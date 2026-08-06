"""Обновление данных: три контура с разной ценой и частотой.

  live  — состояние активных позиций. Только eth_call через Multicall3, без логов.
          Дёшево, поэтому раз в минуту.
  sync  — поиск новых позиций и досканирование событий с сохранённого watermark.
          Дорого (eth_getLogs), поэтому раз в сутки.
  backfill — разовая добивка истории там, где она осталась неполной.

Ключевая экономия: у закрытой позиции с полной историей сканировать нечего — она
иммутабельна, и sync её просто пропускает.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.core import notify, pricealerts
from app.core.chains import CHAINS, Chain, enabled_chains
from app.core.inrange import portfolio as inrange_portfolio
from app.core.market import market_rates
from app.core.portfolio import net_change
from app.core.prices import PriceService
from app.core.rpc import Rpc
from app.core.tokens import TokenService
from app.db.base import session_scope
from app.db.prefs import alert_settings, enabled_chain_keys
from app.db.models import (Alert, BlockTime, KV, Position, PositionEvent, PositionSnapshot,
                           Snapshot, Wallet, utcnow)
from app.providers.base import Ctx, KnownPosition, Provider, RawPosition
from app.providers.fluid import FluidLendingProvider, FluidVaultProvider
from app.providers.uniswap_v3 import UniswapV3Provider

log = logging.getLogger(__name__)

PROVIDERS: list[Provider] = [UniswapV3Provider(), FluidLendingProvider(), FluidVaultProvider()]

# live и sync не должны идти одновременно: оба пишут в одни и те же строки
_refresh_lock = threading.Lock()


# --------------------------------------------------------------------------------------
# Кэш времени блоков поверх SQLite
# --------------------------------------------------------------------------------------

class BlockTimeCache(dict):
    """dict-подобный кэш, который сам подтягивает и сохраняет строки в БД.

    Rpc работает с ts_cache как с обычным словарём, поэтому весь обмен с базой
    прячется здесь: грузить время всех блоков сети заранее было бы бессмысленно.
    """

    def __init__(self, db: Session, chain_key: str):
        super().__init__()
        self.db = db
        self.chain_key = chain_key
        self._pending: dict[int, int] = {}

    def __contains__(self, key: object) -> bool:
        if dict.__contains__(self, key):
            return True
        row = self.db.scalar(select(BlockTime).where(BlockTime.chain == self.chain_key,
                                                     BlockTime.number == key))
        if row is None:
            return False
        dict.__setitem__(self, key, row.timestamp)
        return True

    def __setitem__(self, key: int, value: int) -> None:
        dict.__setitem__(self, key, value)
        self._pending[key] = value

    def get(self, key, default=None):  # noqa: A003 — совместимость с dict
        if key in self:
            return dict.__getitem__(self, key)
        return default

    def flush(self) -> None:
        if not self._pending:
            return
        existing = {r.number for r in self.db.scalars(
            select(BlockTime).where(BlockTime.chain == self.chain_key,
                                    BlockTime.number.in_(list(self._pending)))).all()}
        self.db.add_all([BlockTime(chain=self.chain_key, number=n, timestamp=t)
                         for n, t in self._pending.items() if n not in existing])
        self.db.flush()
        self._pending.clear()


# --------------------------------------------------------------------------------------
# Запись результатов
# --------------------------------------------------------------------------------------

def _known_for(db: Session, wallet_id: int, protocol: str, chain: str) -> dict[str, KnownPosition]:
    rows = db.scalars(select(Position).where(Position.wallet_id == wallet_id,
                                             Position.protocol == protocol,
                                             Position.chain == chain)).all()
    return {r.external_id: KnownPosition(external_id=r.external_id,
                                         last_scanned_block=r.last_scanned_block,
                                         history_complete=r.history_complete,
                                         is_open=r.is_open, db_id=r.id,
                                         opened_at=r.opened_at) for r in rows}


def _upsert_position(db: Session, wallet: Wallet, rp: RawPosition, with_history: bool) -> Position:
    pos = db.scalar(select(Position).where(Position.wallet_id == wallet.id,
                                           Position.protocol == rp.protocol,
                                           Position.chain == rp.chain,
                                           Position.external_id == rp.external_id))
    created = pos is None
    if created:
        pos = Position(wallet_id=wallet.id, protocol=rp.protocol, chain=rp.chain,
                       external_id=rp.external_id)
        db.add(pos)

    prev_in_range = pos.in_range
    prev_health = pos.health_factor

    pos.title = rp.title or pos.title
    pos.subtitle = rp.subtitle
    pos.is_open = rp.is_open
    pos.value_usd = rp.value_usd
    pos.debt_usd = rp.debt_usd
    pos.net_usd = rp.net_usd
    pos.fees_unclaimed_usd = rp.fees_unclaimed_usd
    pos.in_range = rp.in_range
    pos.health_factor = rp.health_factor
    pos.ltv = rp.ltv
    pos.detail = rp.detail
    pos.live_updated_at = utcnow()

    # Поля, производные от истории. В live-режиме события всё равно читаются из БД,
    # поэтому PnL и годовые пересчитываются по свежим ценам каждую минуту — но
    # записываются только непустые значения, иначе неудачный прогон обнулил бы данные.
    hist_fields = ("fees_claimed_usd", "deposited_usd", "withdrawn_usd",
                   "pnl_usd", "pnl_pct", "apr")
    if with_history or created:
        for f in hist_fields:
            setattr(pos, f, getattr(rp, f))
        pos.history_complete = rp.history_complete
        if rp.last_scanned_block:
            pos.last_scanned_block = rp.last_scanned_block
        pos.synced_at = utcnow()
    else:
        for f in hist_fields:
            v = getattr(rp, f)
            if v is not None:
                setattr(pos, f, v)
    pos.opened_at = rp.opened_at or pos.opened_at
    pos.closed_at = rp.closed_at or pos.closed_at

    db.flush()

    if rp.events:
        _upsert_events(db, pos, rp)
    _check_alerts(db, pos, prev_in_range, prev_health, created)
    return pos


def _upsert_events(db: Session, pos: Position, rp: RawPosition) -> None:
    existing = {(e.block, e.log_index): e for e in db.scalars(
        select(PositionEvent).where(PositionEvent.position_id == pos.id)).all()}
    for e in rp.events or []:
        row = existing.get((e.block, e.log_index))
        if row is None:
            row = PositionEvent(position_id=pos.id, block=e.block, log_index=e.log_index)
            db.add(row)
        row.kind = e.kind
        row.tx = e.tx
        row.timestamp = e.timestamp
        row.liquidity = str(e.liquidity)
        row.amount0, row.amount1 = str(e.amount0), str(e.amount1)
        row.fee0, row.fee1 = str(e.fee0), str(e.fee1)
        row.usd_at_time = e.usd_at_time
        row.fee_usd_at_time = e.fee_usd_at_time
        row.price0_usd, row.price1_usd = e.price0_usd, e.price1_usd
        row.extra = e.extra or {}
    db.flush()


RANGE_KINDS = ("out_of_range", "back_in_range")


def _recent_range_alert(db: Session, position_id: int) -> Alert | None:
    """Последний алерт про диапазон по этой позиции внутри паузы против дребезга."""
    since = utcnow() - timedelta(seconds=alert_settings(db)["cooldown"])
    row = db.scalar(select(Alert).where(Alert.position_id == position_id,
                                        Alert.kind.in_(RANGE_KINDS))
                    .order_by(Alert.ts.desc()).limit(1))
    if row is None:
        return None
    ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
    return row if ts >= since else None


def _add_alert(db: Session, pos: Position, kind: str, severity: str, message: str,
               cooldown: bool = False) -> None:
    """Создаёт алерт. cooldown=True — придавить отправку, если недавно уже писали.

    Алерт всё равно появится в дашборде: придавливается доставка, а не сам факт.
    """
    state = "pending"
    if cooldown and _recent_range_alert(db, pos.id) is not None:
        state = "suppressed"
    elif not config.telegram_configured():
        # помечаем сразу, иначе после настройки бота прилетит поток старых событий
        state = "disabled"
    db.add(Alert(position_id=pos.id, kind=kind, severity=severity, message=message,
                 notify_state=state))


def _check_alerts(db: Session, pos: Position, prev_in_range, prev_health, created: bool) -> None:
    """Алерты только на ПЕРЕХОД состояния — иначе они бы сыпались каждую минуту."""
    if created:
        return

    # По закрытой позиции алертов быть не должно.
    #
    # in_range у Uniswap считается из тиков и текущей цены и НЕ смотрит на
    # ликвидность (см. _Pos.in_range): у позиции с нулевой ликвидностью границы
    # диапазона остаются на месте, цена продолжает через них ходить, и дашборд
    # исправно сообщает «вышла из диапазона» про то, из чего деньги давно выведены.
    #
    # Ликвидация — единственное исключение ниже: она сама закрывает позицию, и
    # промолчать про неё из-за этой же проверки было бы худшим из возможных исходов.
    active = bool(pos.is_open)

    conf = alert_settings(db)
    if (active and conf["out_of_range"]
            and prev_in_range is not None and pos.in_range is not None):
        if prev_in_range and not pos.in_range:
            _add_alert(db, pos, "out_of_range", "warning",
                       f"{pos.title} ({pos.chain}) вышла из диапазона — "
                       f"комиссии больше не начисляются", cooldown=True)
        elif not prev_in_range and pos.in_range:
            _add_alert(db, pos, "back_in_range", "info",
                       f"{pos.title} ({pos.chain}) вернулась в диапазон", cooldown=True)

    thr = conf["health_factor"]
    if active and pos.health_factor is not None:
        crossed_down = (prev_health is None or prev_health >= thr) and pos.health_factor < thr
        if crossed_down:
            msg = (f"{pos.title} ({pos.chain}): health factor "
                   f"{pos.health_factor:.2f} — ниже порога {thr}")
            liq = (pos.detail.get("risk") or {}).get("liquidation_price")
            unit = (pos.detail.get("risk") or {}).get("price_unit") or ""
            if liq:
                msg += f". Цена ликвидации {liq:.6g} {unit}".rstrip()
            _add_alert(db, pos, "health", "danger", msg)
    if pos.detail.get("is_liquidated"):
        already = db.scalar(select(Alert).where(Alert.position_id == pos.id,
                                                Alert.kind == "liquidated"))
        if already is None:
            _add_alert(db, pos, "liquidated", "danger",
                       f"{pos.title} ({pos.chain}): позиция ликвидирована")


def dispatch_alerts(db: Session) -> int:
    """Отправляет накопившиеся алерты одним сообщением. Возвращает число отправленных."""
    pending = list(db.scalars(select(Alert).where(Alert.notify_state == "pending")
                              .order_by(Alert.ts)).all())
    if not pending:
        return 0
    if not config.telegram_configured():
        for a in pending:
            a.notify_state = "disabled"
        db.flush()
        return 0

    ok = notify.send(notify.format_alerts(pending))
    now = utcnow()
    for a in pending:
        a.notify_state = "sent" if ok else "failed"
        a.notified_at = now if ok else None
    db.flush()
    if ok:
        log.info("[notify] отправлено алертов: %s", len(pending))
    return len(pending) if ok else 0


# --------------------------------------------------------------------------------------
# Основной проход
# --------------------------------------------------------------------------------------

def _run_chain(db: Session, wallet: Wallet, chain: Chain, with_history: bool,
               only_protocols: set[str] | None = None) -> tuple[int, list[str]]:
    """Опрашивает одну сеть по одному кошельку. Возвращает (сколько позиций, ошибки)."""
    errors: list[str] = []
    ts_cache = BlockTimeCache(db, chain.key)
    rpc = Rpc(chain, extra_rpc=config.RPC_URL, ts_cache=ts_cache)
    prices = PriceService(db)
    tokens = TokenService(db, chain, rpc)
    ctx = Ctx(db=db, chain=chain, rpc=rpc, prices=prices, tokens=tokens,
              history_budget=config.HISTORY_BUDGET)

    total = 0
    for prov in PROVIDERS:
        if not prov.supports(chain):
            continue
        if only_protocols and prov.key not in only_protocols:
            continue
        try:
            known = _known_for(db, wallet.id, prov.key, chain.key)
            found = prov.fetch(ctx, wallet.address, known, with_history)
        except Exception as e:  # noqa: BLE001 — одна сеть/протокол не валит весь прогон
            msg = f"{chain.name}/{prov.title}: {type(e).__name__}: {str(e)[:160]}"
            log.warning("  ! %s", msg)
            errors.append(msg)
            continue
        for rp in found:
            _upsert_position(db, wallet, rp, with_history)
            total += 1

    ts_cache.flush()
    db.flush()
    return total, errors


def refresh(mode: str = "live", wallet_id: int | None = None) -> dict:
    """
    Прогон обновления.

    mode='live' — только текущее состояние (быстро).
    mode='sync' — плюс сканирование логов и разбор истории.
    """
    with_history = mode in ("sync", "backfill")
    started = time.time()

    if not _refresh_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "обновление уже идёт"}

    stats = {"mode": mode, "positions": 0, "wallets": 0, "errors": [],
             "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        with session_scope() as db:
            # курсы ETH/BTC для тикера греем здесь же: тогда открытая страница
            # берёт их из базы мгновенно, не дожидаясь похода в DefiLlama
            rates = market_rates(db)
            # оповещения о цене проверяем на тех же ценах — своих запросов не делаем
            stats["price_alerts"] = pricealerts.check(db, rates)

            wallets = db.scalars(
                select(Wallet).where(Wallet.enabled.is_(True))
                .where(Wallet.id == wallet_id if wallet_id else Wallet.id.isnot(None))).all()
            # список читаем из настроек на каждом прогоне: галочка в интерфейсе
            # должна действовать со следующего цикла, без перезапуска
            chains = enabled_chains(enabled_chain_keys(db))

            for w in wallets:
                stats["wallets"] += 1
                for chain in chains:
                    n, errs = _run_chain(db, w, chain, with_history)
                    stats["positions"] += n
                    stats["errors"].extend(errs)

            _write_snapshots(db)
            # алерты сложились в базу выше — отправляем их одним сообщением
            stats["notified"] = dispatch_alerts(db)
            stats["elapsed"] = round(time.time() - started, 1)
            _set_kv(db, f"last_{mode}", stats)
    finally:
        _refresh_lock.release()

    log.info("[refresh:%s] позиций %s, кошельков %s, %.1f c, ошибок %s",
             mode, stats["positions"], stats["wallets"], time.time() - started,
             len(stats["errors"]))
    return stats


def _token_amount(detail: dict | None, side: str) -> float | None:
    """Количество токенов залога или долга из detail — если протокол его сообщает."""
    part = (detail or {}).get(side) or {}
    value = part.get("human")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fee_tokens(detail: dict | None) -> dict:
    """Несобранные комиссии в токенах и цены на этот момент — для расчёта начислений.

    Сырые значения приходят строками (в базе они не влезают в целое), поэтому делим
    на десятичность здесь: дальше нужны уже человеческие количества.
    """
    d = detail or {}
    if d.get("kind") != "univ3":
        return {}
    usd = d.get("usd") or {}
    out: dict[str, float | None] = {"price0_usd": usd.get("price0_usd"),
                                    "price1_usd": usd.get("price1_usd")}
    for i in (0, 1):
        raw = d.get(f"fees{i}")
        dec = ((d.get(f"token{i}") or {}).get("decimals"))
        try:
            out[f"fees{i}_tokens"] = int(raw) / (10 ** int(dec)) if raw is not None else None
        except (TypeError, ValueError):
            out[f"fees{i}_tokens"] = None
    return out


def _write_snapshots(db: Session, force: bool = False) -> None:
    """Точка на графике капитала: по каждому кошельку и суммарно.

    Пишется не на каждый прогон: при минутном live-обновлении это давало бы 1440
    точек в сутки на кошелёк, что бесполезно для графика и раздувает базу.
    """
    now = utcnow()
    if not force:
        last = db.scalar(select(Snapshot).where(Snapshot.wallet_id.is_(None))
                         .order_by(Snapshot.ts.desc()).limit(1))
        if last is not None:
            # SQLite отдаёт datetime без таймзоны — приводим к UTC перед вычитанием
            prev = last.ts if last.ts.tzinfo else last.ts.replace(tzinfo=timezone.utc)
            if (now - prev).total_seconds() < config.SNAPSHOT_INTERVAL:
                return

    positions = db.scalars(select(Position).where(Position.is_open.is_(True))).all()

    # Нечего снимать — не пишем точку вовсе.
    #
    # Ноль на графике означал бы «капитал был нулевым», а на самом деле это «мы ещё
    # не знали»: так появлялась точка при самом первом запуске, до первой удачной
    # синхронизации, и график начинался отвесной чертой от нуля, портя весь масштаб.
    # Цена решения: если однажды закроете все позиции разом, график не поставит
    # честный ноль, а остановится на последней точке. Это меньшее из двух зол —
    # закрыть всё сразу можно раз в жизни, а первый запуск бывает у каждого.
    if not positions:
        return

    per_wallet: dict[int, list[Position]] = {}
    for p in positions:
        per_wallet.setdefault(p.wallet_id, []).append(p)

    def agg(items: list[Position]) -> dict:
        value = sum(p.value_usd or 0 for p in items)
        debt = sum(p.debt_usd or 0 for p in items)
        fees = sum(p.fees_unclaimed_usd or 0 for p in items)
        by_proto: dict[str, float] = {}
        by_chain: dict[str, float] = {}
        for p in items:
            by_proto[p.protocol] = by_proto.get(p.protocol, 0.0) + (p.net_usd or 0)
            by_chain[p.chain] = by_chain.get(p.chain, 0.0) + (p.net_usd or 0)
        return {"value_usd": value, "debt_usd": debt, "net_usd": value - debt,
                "fees_unclaimed_usd": fees, "positions_open": len(items),
                "breakdown": {"protocol": by_proto, "chain": by_chain}}

    for wid, items in per_wallet.items():
        a = agg(items)
        db.add(Snapshot(ts=now, wallet_id=wid, **a))

    total = agg(positions)
    db.add(Snapshot(ts=now, wallet_id=None, **total))

    for p in positions:
        # ставки и балансы в токенах — только у Fluid, у Uniswap их нет и не нужно
        rates = (p.detail or {}).get("rates") or {}
        db.add(PositionSnapshot(position_id=p.id, ts=now, value_usd=p.value_usd,
                                debt_usd=p.debt_usd, net_usd=p.net_usd,
                                fees_unclaimed_usd=p.fees_unclaimed_usd,
                                health_factor=p.health_factor, in_range=p.in_range,
                                borrow_rate=rates.get("borrow"),
                                # у депозита Fluid ставка лежит не в rates, а в apr —
                                # иначе его доход выпал бы из истории целиком
                                supply_rate=(rates.get("supply") if rates
                                             else (p.apr if p.protocol.startswith("fluid") else None)),
                                debt_tokens=_token_amount(p.detail, "debt"),
                                collateral_tokens=_token_amount(p.detail, "collateral"),
                                **_fee_tokens(p.detail)))
    db.flush()


# --------------------------------------------------------------------------------------
# Ежедневная сводка
# --------------------------------------------------------------------------------------

def digest_payload(db: Session) -> dict:
    """Цифры для ежедневной сводки. Собираются прямо здесь, без веб-слоя.

    Изменение за сутки берётся из снапшота портфеля суточной давности: это тот же
    ряд, что рисует график капитала, поэтому сводка и дашборд не могут разойтись.
    """
    positions = list(db.scalars(select(Position).where(Position.is_open.is_(True))).all())
    net = sum(p.net_usd or 0 for p in positions)
    now = utcnow()

    # тот же расчёт, что в плитке «Чистая стоимость» на дашборде: одна функция,
    # чтобы сообщение и экран не разошлись в цифрах
    delta, delta_pct, _ = net_change(db, net, hours=24)

    collected_24h = db.scalar(
        select(func.coalesce(func.sum(PositionEvent.fee_usd_at_time), 0.0))
        .where(PositionEvent.kind == "collect",
               PositionEvent.timestamp >= int((now - timedelta(hours=24)).timestamp())))

    with_hf = [p for p in positions if p.health_factor is not None]
    worst = min(with_hf, key=lambda p: p.health_factor) if with_hf else None
    ir = inrange_portfolio(db, days=1)

    return {
        "net": net,
        "net_delta": delta,
        "net_delta_pct": delta_pct,
        "fees_unclaimed": sum(p.fees_unclaimed_usd or 0 for p in positions),
        "fees_collected": float(collected_24h or 0.0),
        "open_count": len(positions),
        "out_of_range": sum(1 for p in positions if p.in_range is False),
        "worst_hf": worst.health_factor if worst else None,
        "worst_hf_title": f"{worst.title} ({worst.chain})" if worst else None,
        # порог берём действующий, а не из .env: он настраивается в Оповещениях
        "hf_below_threshold": bool(worst and worst.health_factor
                                   < alert_settings(db)["health_factor"]),
        # долю времени показываем только когда наблюдений хватает, иначе она врёт
        "inrange_pct": ir.pct if ir.reliable else None,
    }


def send_digest() -> bool:
    """Собирает и отправляет сводку. Возвращает False, если отправить не удалось."""
    if not config.telegram_configured():
        log.info("[digest] Telegram не настроен — сводка не отправлена")
        return False
    with session_scope() as db:
        text = notify.format_digest(digest_payload(db))
    ok = notify.send(text)
    log.info("[digest] сводка %s", "отправлена" if ok else "НЕ отправлена")
    return ok


def _set_kv(db: Session, key: str, value: dict) -> None:
    row = db.get(KV, key)
    if row is None:
        db.add(KV(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def get_status() -> dict:
    with session_scope() as db:
        out = {}
        for k in ("last_live", "last_sync", "last_backfill"):
            row = db.get(KV, k)
            out[k] = row.value if row else None
        out["running"] = _refresh_lock.locked()
        return out


def add_wallet(address: str, label: str = "") -> Wallet:
    """Добавляет кошелёк. ENS резолвится через публичную ноду Ethereum."""
    from web3 import HTTPProvider, Web3

    raw = address.strip()
    if raw.startswith("0x") and len(raw) == 42:
        addr = Web3.to_checksum_address(raw)
    else:
        addr = None
        for url in CHAINS["ethereum"].rpcs:
            try:
                w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 20}))
                got = w3.ens.address(raw)
                if got:
                    addr = Web3.to_checksum_address(got)
                    break
            except Exception:  # noqa: BLE001
                continue
        if addr is None:
            raise ValueError(f"не удалось разобрать адрес или ENS-имя: {raw}")

    with session_scope() as db:
        existing = db.scalar(select(Wallet).where(Wallet.address == addr))
        if existing:
            if label:
                existing.label = label
            return existing
        w = Wallet(address=addr, label=label or (raw if raw != addr else ""))
        db.add(w)
        db.flush()
        db.refresh(w)
        return w
