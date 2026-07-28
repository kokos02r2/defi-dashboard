"""Провайдер Uniswap V3.

Логика чтения позиций и расчётов перенесена из uniswap_positions.py. Существенно
изменено одно: история больше не пересканируется целиком при каждом запуске.

  * события лежат в БД и накапливаются;
  * у каждой позиции есть last_scanned_block — сканируем только новые блоки;
  * у закрытой позиции с полной историей сканировать нечего вообще, она иммутабельна.

Именно это превращает четырёхминутный прогон по сети в пару запросов.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from sqlalchemy import select
from web3 import Web3

from app.core.abi import abi_decode, call_data, hx, topic
from app.core.chains import FEE_TIERS, MAX_TICK, MIN_TICK, Chain
from app.core.prices import coin_key
from app.core.rpc import Rpc, multicall
from app.db.models import PROTO_UNIV3, PositionEvent
from app.providers.base import Ctx, EventData, KnownPosition, Provider, RawPosition, jsonable
from app.providers.univ3_math import (Q96, amounts_from_liquidity, is_stable, price_at_tick,
                                      solve_breakeven, sqrt_ratio_at_tick, uncollected_fees)

log = logging.getLogger(__name__)

EV_TRANSFER = topic("Transfer(address,address,uint256)")
EV_INCREASE = topic("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
EV_DECREASE = topic("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
EV_COLLECT = topic("Collect(uint256,address,uint256,uint256)")
ZERO_TOPIC = "0x" + "0" * 64

IDS_PER_LOG_QUERY = 40      # сколько tokenId кладём в один фильтр логов

POS_TYPES = ["uint96", "address", "address", "address", "uint24", "int24", "int24",
             "uint128", "uint256", "uint256", "uint128", "uint128"]
TICK_TYPES = ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]
SLOT0_TYPES = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]


# --------------------------------------------------------------------------------------
# Чтение состояния позиций (дёшево — только eth_call через Multicall3)
# --------------------------------------------------------------------------------------

class _Pos:
    """Промежуточное состояние позиции при сборке. В БД уезжает уже RawPosition."""

    __slots__ = ("token_id", "token0", "token1", "fee", "tick_lower", "tick_upper", "liquidity",
                 "fg_inside0", "fg_inside1", "owed0", "owed1", "pool", "sym0", "sym1",
                 "dec0", "dec1", "tick_cur", "sqrt_price_x96", "amount0", "amount1",
                 "fees0", "fees1")

    def __init__(self, token_id: int, d: tuple):
        self.token_id = token_id
        self.token0 = Web3.to_checksum_address(d[2])
        self.token1 = Web3.to_checksum_address(d[3])
        self.fee = d[4]
        self.tick_lower, self.tick_upper = d[5], d[6]
        self.liquidity = d[7]
        self.fg_inside0, self.fg_inside1 = d[8], d[9]
        self.owed0, self.owed1 = d[10], d[11]
        self.pool = ""
        self.sym0 = self.sym1 = "?"
        self.dec0 = self.dec1 = 18
        self.tick_cur: int | None = None
        self.sqrt_price_x96: int | None = None
        self.amount0 = self.amount1 = Decimal(0)
        self.fees0 = self.fees1 = 0

    @property
    def closed(self) -> bool:
        return self.liquidity == 0 and self.owed0 == 0 and self.owed1 == 0

    @property
    def in_range(self) -> bool | None:
        if self.tick_cur is None:
            return None
        return self.tick_lower <= self.tick_cur < self.tick_upper


def _read_positions(ctx: Ctx, wallet: str) -> list[_Pos]:
    chain, rpc = ctx.chain, ctx.rpc
    npm = chain.npm

    balance = abi_decode(["uint256"], rpc.eth_call(
        npm, call_data("balanceOf(address)", ["address"], [wallet])))[0]
    log.debug("[%s] NFT-позиций у кошелька: %s", chain.name, balance)
    if balance == 0:
        return []

    ids_res = multicall(rpc, [
        (npm, call_data("tokenOfOwnerByIndex(address,uint256)", ["address", "uint256"], [wallet, i]))
        for i in range(balance)
    ])
    token_ids = [abi_decode(["uint256"], r)[0] for ok, r in ids_res if ok and r]

    pos_res = multicall(rpc, [(npm, call_data("positions(uint256)", ["uint256"], [tid]))
                              for tid in token_ids])
    positions: list[_Pos] = []
    for tid, (ok, raw) in zip(token_ids, pos_res):
        if not ok or not raw:
            continue
        positions.append(_Pos(tid, abi_decode(POS_TYPES, raw)))
    if not positions:
        return []

    # адреса пулов
    pool_res = multicall(rpc, [
        (chain.factory, call_data("getPool(address,address,uint24)",
                                  ["address", "address", "uint24"], [p.token0, p.token1, p.fee]))
        for p in positions])
    for p, (ok, raw) in zip(positions, pool_res):
        p.pool = Web3.to_checksum_address(abi_decode(["address"], raw)[0]) if ok and raw else ""

    # метаданные токенов — из вечного кэша, в сеть ходим только за новыми
    meta = ctx.tokens.resolve([t for p in positions for t in (p.token0, p.token1)])
    for p in positions:
        p.sym0, p.dec0 = meta.get(p.token0, ("?", 18))
        p.sym1, p.dec1 = meta.get(p.token1, ("?", 18))

    # состояние пулов + тики границ (для несобранных комиссий)
    calls: list[tuple[str, bytes]] = []
    live = [p for p in positions if p.pool and int(p.pool, 16) != 0]
    for p in live:
        calls.append((p.pool, call_data("slot0()", [], [])))
        calls.append((p.pool, call_data("feeGrowthGlobal0X128()", [], [])))
        calls.append((p.pool, call_data("feeGrowthGlobal1X128()", [], [])))
        calls.append((p.pool, call_data("ticks(int24)", ["int24"], [p.tick_lower])))
        calls.append((p.pool, call_data("ticks(int24)", ["int24"], [p.tick_upper])))
    res = multicall(rpc, calls)

    for i, p in enumerate(live):
        chunk = res[i * 5:i * 5 + 5]
        try:
            slot0 = abi_decode(SLOT0_TYPES, chunk[0][1])
            fg0 = abi_decode(["uint256"], chunk[1][1])[0]
            fg1 = abi_decode(["uint256"], chunk[2][1])[0]
            tl = abi_decode(TICK_TYPES, chunk[3][1])
            tu = abi_decode(TICK_TYPES, chunk[4][1])
        except Exception:  # noqa: BLE001 — пул может быть не инициализирован
            continue
        p.sqrt_price_x96, p.tick_cur = slot0[0], slot0[1]
        p.amount0, p.amount1 = amounts_from_liquidity(
            p.liquidity, p.sqrt_price_x96, p.tick_lower, p.tick_upper)
        p.fees0 = p.owed0 + uncollected_fees(
            p.liquidity, fg0, tl[2], tu[2], p.fg_inside0, p.tick_cur, p.tick_lower, p.tick_upper)
        p.fees1 = p.owed1 + uncollected_fees(
            p.liquidity, fg1, tl[3], tu[3], p.fg_inside1, p.tick_cur, p.tick_lower, p.tick_upper)

    return positions


# --------------------------------------------------------------------------------------
# История: инкрементальное сканирование логов
# --------------------------------------------------------------------------------------

def _decode_liq_log(lg: dict) -> EventData:
    t0 = hx(lg["topics"][0].hex())
    data = bytes(lg["data"])
    if t0 in (hx(EV_INCREASE), hx(EV_DECREASE)):
        liq, a0, a1 = abi_decode(["uint128", "uint256", "uint256"], data)
        kind = "increase" if t0 == hx(EV_INCREASE) else "decrease"
    else:
        _recipient, a0, a1 = abi_decode(["address", "uint256", "uint256"], data)
        liq, kind = 0, "collect"
    return EventData(kind=kind, block=lg["blockNumber"], log_index=lg["logIndex"],
                     tx=hx(lg["transactionHash"].hex()), liquidity=liq, amount0=a0, amount1=a1)


def _decode_xfer_log(lg: dict) -> EventData:
    frm = "0x" + lg["topics"][1].hex()[-40:]
    to = "0x" + lg["topics"][2].hex()[-40:]
    return EventData(kind="transfer", block=lg["blockNumber"], log_index=lg["logIndex"],
                     tx=hx(lg["transactionHash"].hex()),
                     extra={"from": Web3.to_checksum_address(frm),
                            "to": Web3.to_checksum_address(to)})


def find_mint_block(rpc: Rpc, npm: str, token_id: int, lo: int, hi: int,
                    deadline: float) -> int | None:
    """
    Блок минта NFT позиции — бинарным поиском по событиям mint.

    Работает на любой публичной ноде, потому что нужны только КОРОТКИЕ диапазоны блоков.
    Опорный факт: tokenId выдаются строго по возрастанию, значит номер токена монотонен
    по номеру блока. Смотрим в окно-зонд, какие tokenId минтились рядом, и сдвигаем границы.
    """
    window, max_window = 5_000, 400_000
    probe = {"address": npm, "topics": [hx(EV_TRANSFER), ZERO_TOPIC]}

    while hi - lo > window:
        if time.time() > deadline:
            return None
        mid = (lo + hi) // 2
        a, w, found = mid, window, None
        while found is None and w <= max_window:
            b = min(a + w - 1, hi)
            try:
                logs = rpc.get_logs(probe, a, b)
            except Exception:  # noqa: BLE001 — нода не потянула окно, пробуем меньше
                w = max(w // 4, 500)
                if w < 500:
                    return None
                continue
            if logs:
                ids = [int(hx(lg["topics"][3].hex()), 16) for lg in logs]
                found = (min(ids), max(ids), a, b)
            elif b >= hi:
                hi = a - 1          # правее минтов нет — искомый левее
                break
            else:
                w *= 4              # тихий участок: расширяем зонд вправо
        if found is None:
            continue
        mn, mx, a, b = found
        if token_id < mn:
            hi = a - 1
        elif token_id > mx:
            lo = b + 1
        else:
            lo, hi = a, b
            break

    try:
        exact = rpc.get_logs({"address": npm,
                              "topics": [hx(EV_TRANSFER), ZERO_TOPIC, None,
                                         "0x" + f"{token_id:064x}"]}, lo, hi)
    except Exception:  # noqa: BLE001
        return None
    return exact[0]["blockNumber"] if exact else None


def _scan_events(ctx: Ctx, positions: list[_Pos], known: dict[str, KnownPosition],
                 latest: int) -> tuple[dict[int, list[EventData]], set[int], set[int]]:
    """
    Логи по всем позициям. Возвращает (новые события, полностью покрытые, частичные).

    Позиции делятся на две группы с разной нижней границей сканирования:
      * известные  -> от сохранённого watermark (обычно это «вчера»);
      * новые      -> от блока деплоя NPM.
    Внутри группы фильтр eth_getLogs принимает СПИСОК tokenId, поэтому события сразу
    десятков позиций забираются пачками, а не по запросу на позицию.
    """
    chain, rpc = ctx.chain, ctx.rpc
    npm = Web3.to_checksum_address(chain.npm)
    deadline = time.time() + ctx.history_budget

    out: dict[int, list[EventData]] = {p.token_id: [] for p in positions}
    covered: set[int] = set()
    partial: set[int] = set()

    groups: dict[int, list[int]] = {}
    for p in positions:
        k = known.get(str(p.token_id))
        start = (k.last_scanned_block + 1) if (k and k.last_scanned_block) else chain.deploy_block
        groups.setdefault(start, []).append(p.token_id)

    for start, ids in sorted(groups.items()):
        if start > latest:
            covered.update(ids)          # новых блоков с прошлого раза не появилось
            continue
        for i in range(0, len(ids), IDS_PER_LOG_QUERY):
            if time.time() > deadline:
                break
            part = ids[i:i + IDS_PER_LOG_QUERY]
            tid_topics = ["0x" + f"{t:064x}" for t in part]
            liq = rpc.get_logs_ranged(
                {"address": npm,
                 "topics": [[hx(EV_INCREASE), hx(EV_DECREASE), hx(EV_COLLECT)], tid_topics]},
                start, latest, deadline)
            if liq is None:
                continue
            xfer = rpc.get_logs_ranged(
                {"address": npm, "topics": [hx(EV_TRANSFER), None, None, tid_topics]},
                start, latest, deadline)
            if xfer is None:
                continue
            for lg in liq:
                out[int(hx(lg["topics"][1].hex()), 16)].append(_decode_liq_log(lg))
            for lg in xfer:
                out[int(hx(lg["topics"][3].hex()), 16)].append(_decode_xfer_log(lg))
            covered.update(part)
            log.debug("[%s] пачка %s позиций с блока %s: %s событий",
                      chain.name, len(part), start, len(liq) + len(xfer))

    # запасной путь: нода режет диапазон — ищем блок минта и сканируем только «хвост»
    lo_bound = chain.deploy_block
    for p in sorted(positions, key=lambda x: x.token_id):
        if p.token_id in covered or time.time() > deadline:
            continue
        tid_topic = "0x" + f"{p.token_id:064x}"
        liq_filter = {"address": npm,
                      "topics": [[hx(EV_INCREASE), hx(EV_DECREASE), hx(EV_COLLECT)], tid_topic]}
        xfer_filter = {"address": npm, "topics": [hx(EV_TRANSFER), None, None, tid_topic]}

        mint_block = find_mint_block(rpc, npm, p.token_id, lo_bound, latest, deadline)
        if mint_block is None:
            continue
        lo_bound = mint_block
        liq_logs = rpc.get_logs_ranged(liq_filter, mint_block, latest, deadline)
        if liq_logs is None:
            # хотя бы событие открытия — это даёт дату и первый взнос
            try:
                liq_logs = rpc.get_logs(liq_filter, mint_block, mint_block)
                partial.add(p.token_id)
            except Exception:  # noqa: BLE001
                continue
        xfer_logs = rpc.get_logs_ranged(xfer_filter, mint_block, latest,
                                        min(deadline, time.time() + 10)) or []
        out[p.token_id] = [_decode_liq_log(lg) for lg in liq_logs] + \
                          [_decode_xfer_log(lg) for lg in xfer_logs]
        covered.add(p.token_id)

    # таймстемпы всех новых блоков разом — параллельно и с общим кэшем
    blocks = {e.block for tid in covered for e in out[tid]}
    rpc.fetch_block_times(blocks, deadline)
    for tid in covered:
        for e in out[tid]:
            e.timestamp = rpc.ts_cache.get(e.block)

    return out, covered, partial


def _load_stored_events(ctx: Ctx, db_id: int) -> list[EventData]:
    rows = ctx.db.scalars(select(PositionEvent).where(PositionEvent.position_id == db_id)
                          .order_by(PositionEvent.block, PositionEvent.log_index)).all()
    return [EventData(kind=r.kind, block=r.block, log_index=r.log_index, tx=r.tx,
                      timestamp=r.timestamp, liquidity=int(r.liquidity or 0),
                      amount0=int(r.amount0 or 0), amount1=int(r.amount1 or 0),
                      fee0=int(r.fee0 or 0), fee1=int(r.fee1 or 0),
                      usd_at_time=r.usd_at_time, fee_usd_at_time=r.fee_usd_at_time,
                      price0_usd=r.price0_usd, price1_usd=r.price1_usd, extra=r.extra or {})
            for r in rows]


def _split_collects(events: list[EventData]) -> None:
    """
    Разделяет Collect на выведенное тело и заработанные комиссии.

    decrease кладёт тело в «долг» контракта перед владельцем; collect сначала гасит этот
    долг, а всё сверх него — комиссии. Так корректно и когда decrease с collect в одной
    транзакции, и когда клейм сделан отдельно и позже. Порядок (блок, индекс лога) —
    это точный порядок в цепочке, и он здесь принципиален.
    """
    events.sort(key=lambda e: (e.block, e.log_index))
    pending0 = pending1 = 0
    for e in events:
        if e.kind == "decrease":
            pending0 += e.amount0
            pending1 += e.amount1
        elif e.kind == "collect":
            body0, body1 = min(e.amount0, pending0), min(e.amount1, pending1)
            pending0 -= body0
            pending1 -= body1
            e.fee0, e.fee1 = e.amount0 - body0, e.amount1 - body1


# --------------------------------------------------------------------------------------
# Долларовая оценка
# --------------------------------------------------------------------------------------

def _acquisition(p: _Pos, hist: dict, has_history: bool) -> dict:
    """
    По какой средней цене позиция набирала волатильный актив.

    Считаются две величины, и они отвечают на разные вопросы.

    1) По диапазону. Пока цена идёт вниз от s₁ к s₂ (в корнях цены), позиция набирает
       L·(1/s₂ − 1/s₁) базового актива и тратит L·(s₁ − s₂) котируемого. Отношение
       этих величин сокращается до s₁·s₂, то есть до √(P₁·P₂) — среднего
       геометрического цен на концах пройденного участка. Пройдя диапазон целиком,
       позиция наберёт актив ровно по √(нижняя·верхняя), и это известно заранее.

    2) По фактическим движениям токенов: сколько базового актива на руках оказалось
       сверх внесённого и сколько котируемого на это ушло. Комиссии сюда не входят —
       они отделены от тела при разборе Collect, и нам нужна цена конверсии, а не доход.

    Первая цифра — ориентир, вторая — то, что произошло на самом деле.
    """
    if p.tick_cur is None:
        return {}

    # котируемым считаем стейбл; если стейблов нет или оба — берём token1 как котировку
    quote_is_0 = is_stable(p.sym0) and not is_stable(p.sym1)
    if quote_is_0:
        base_sym, quote_sym = p.sym1, p.sym0
        base_dec, quote_dec = p.dec1, p.dec0
        lo = 1 / price_at_tick(p.tick_upper, p.dec0, p.dec1)
        hi = 1 / price_at_tick(p.tick_lower, p.dec0, p.dec1)
        cur = 1 / price_at_tick(p.tick_cur, p.dec0, p.dec1)
        base_now, quote_now = p.amount1, p.amount0
    else:
        base_sym, quote_sym = p.sym0, p.sym1
        base_dec, quote_dec = p.dec0, p.dec1
        lo = price_at_tick(p.tick_lower, p.dec0, p.dec1)
        hi = price_at_tick(p.tick_upper, p.dec0, p.dec1)
        cur = price_at_tick(p.tick_cur, p.dec0, p.dec1)
        base_now, quote_now = p.amount0, p.amount1

    out: dict = {
        "base_symbol": base_sym, "quote_symbol": quote_sym,
        "base_decimals": base_dec, "quote_decimals": quote_dec,
        "range_low": lo, "range_high": hi, "current_price": cur,
        "range_avg": (lo * hi).sqrt() if lo > 0 and hi > 0 else None,
        "unit": f"{quote_sym} за 1 {base_sym}",
        "fully_converted": p.tick_cur <= p.tick_lower if not quote_is_0
                           else p.tick_cur >= p.tick_upper,
    }

    # без разобранной истории фактическую цену считать не из чего; нули приняли бы
    # весь остаток за «набранное» и дали бы заведомо неверную среднюю
    if not has_history or "deposited0" not in hist:
        return out

    def raw(key: str) -> int:
        return int(hist.get(key) or 0)

    if quote_is_0:
        dep_base, dep_quote = raw("deposited1"), raw("deposited0")
        wit_base, wit_quote = raw("withdrawn1"), raw("withdrawn0")
    else:
        dep_base, dep_quote = raw("deposited0"), raw("deposited1")
        wit_base, wit_quote = raw("withdrawn0"), raw("withdrawn1")

    # «оказалось на руках» = осталось в пуле + уже выведено телом
    base_final = Decimal(int(base_now)) + Decimal(wit_base)
    quote_final = Decimal(int(quote_now)) + Decimal(wit_quote)
    base_gained = base_final - Decimal(dep_base)
    quote_spent = Decimal(dep_quote) - quote_final

    base_h = base_gained / (Decimal(10) ** base_dec)
    quote_h = quote_spent / (Decimal(10) ** quote_dec)
    out["base_gained"] = base_h
    out["quote_spent"] = quote_h

    # Формула симметрична: диапазон можно пройти и вниз, и вверх.
    #   вниз — позиция набрала базовый актив, потратив котируемый  (покупка)
    #   вверх — распродала базовый, получив котируемый             (продажа)
    # В обоих случаях средняя цена это |котируемый| / |базовый|; знаки лишь говорят,
    # что произошло. Расходятся знаки — значит движения не сводятся к одной конверсии
    # (доливали обе стороны), и средняя тут смысла не имеет.
    if base_gained != 0 and quote_spent != 0 and (base_gained > 0) == (quote_spent > 0):
        out["actual_avg"] = abs(quote_h) / abs(base_h)
        out["direction"] = "buy" if base_gained > 0 else "sell"
        out["base_abs"] = abs(base_h)
        out["quote_abs"] = abs(quote_h)
    return out


def _enrich(ctx: Ctx, p: _Pos, events: list[EventData], partial: bool,
            has_history: bool) -> RawPosition:
    chain = ctx.chain
    c0, c1 = coin_key(chain, p.token0), coin_key(chain, p.token1)
    now = ctx.prices.current([c0, c1])
    pr0, pr1 = now.get(c0), now.get(c1)

    def usd(raw0, raw1) -> Decimal | None:
        if pr0 is None or pr1 is None:
            return None
        return ((Decimal(raw0) / Decimal(10) ** p.dec0) * pr0
                + (Decimal(raw1) / Decimal(10) ** p.dec1) * pr1)

    u: dict = {"price0_usd": pr0, "price1_usd": pr1}
    position_value = usd(p.amount0, p.amount1)
    fees_unc = usd(p.fees0, p.fees1)

    # ── потолок стоимости: на верхней границе диапазона позиция полностью
    # распродана в котирующий токен, и дальше рост цены на неё не влияет
    if p.liquidity:
        quote0 = is_stable(p.sym0) and not is_stable(p.sym1)
        sa_raw, sb_raw = sqrt_ratio_at_tick(p.tick_lower), sqrt_ratio_at_tick(p.tick_upper)
        if quote0 and pr0 is not None:
            amt = Decimal(p.liquidity) * (1 / sa_raw - 1 / sb_raw)
            u["max_amount"] = amt / Decimal(10) ** p.dec0
            u["max_value_usd"] = u["max_amount"] * pr0
            u["max_in_token"] = 0
        elif not quote0 and pr1 is not None:
            amt = Decimal(p.liquidity) * (sb_raw - sa_raw)
            u["max_amount"] = amt / Decimal(10) ** p.dec1
            u["max_value_usd"] = u["max_amount"] * pr1
            u["max_in_token"] = 1
        if "max_in_token" in u:
            u["max_quote_is_stable"] = is_stable(p.sym0 if u["max_in_token"] == 0 else p.sym1)

    hist: dict = {"available": has_history, "partial": partial}
    opened_at = closed_at = None
    fees_total = None

    if has_history and events:
        incs = [e for e in events if e.kind == "increase"]
        decs = [e for e in events if e.kind == "decrease"]
        cols = [e for e in events if e.kind == "collect"]
        mint = next((e for e in events if e.kind == "transfer"
                     and int(e.extra.get("from", "0x0"), 16) == 0), None)
        opened = incs[0] if incs else mint
        opened_at = opened.timestamp if opened else None

        def tot(evs, attr) -> int:
            return sum(getattr(e, attr) for e in evs)

        dep0, dep1 = tot(incs, "amount0"), tot(incs, "amount1")
        wit0, wit1 = tot(decs, "amount0"), tot(decs, "amount1")
        fee0c, fee1c = tot(cols, "fee0"), tot(cols, "fee1")

        # исторические цены — одним пакетом на все события позиции
        need = {(c, e.timestamp) for e in events if e.kind != "transfer" and e.timestamp
                for c in (c0, c1)}
        ctx.prices.prefetch(need)

        def value_at_own_time(evs, k0="amount0", k1="amount1",
                              store="usd_at_time") -> Decimal | None:
            """Сумма событий, каждое — по цене токенов на момент этого события."""
            total = Decimal(0)
            for e in evs:
                if not e.timestamp:
                    return None
                h0, h1 = ctx.prices.at(c0, e.timestamp), ctx.prices.at(c1, e.timestamp)
                if h0 is None or h1 is None:
                    return None
                v = ((Decimal(getattr(e, k0, 0)) / Decimal(10) ** p.dec0) * h0
                     + (Decimal(getattr(e, k1, 0)) / Decimal(10) ** p.dec1) * h1)
                setattr(e, store, float(v))
                e.price0_usd, e.price1_usd = float(h0), float(h1)
                total += v
            return total

        # вклад по ценам входа, изъятия по ценам выхода: так PnL получается
        # реализованным, а не «если бы всё дожило до сегодня»
        deposited = value_at_own_time(incs)
        u["deposited_first"] = value_at_own_time(incs[:1])
        u["deposits_count"] = len(incs)
        if incs and incs[0].price0_usd is not None:
            u["entry_price0_usd"] = incs[0].price0_usd
            u["entry_price1_usd"] = incs[0].price1_usd
        deposited_now = usd(dep0, dep1)      # HODL-оценка по текущей цене

        withdrawn = fees_claimed = None
        if not partial:
            withdrawn = value_at_own_time(decs) or usd(wit0, wit1)
            value_at_own_time(cols)          # полная сумма клейма — для строки истории
            # комиссии по курсу дня клейма: заклеймленное обычно сразу меняют в доллары
            fees_claimed = value_at_own_time(cols, "fee0", "fee1", store="fee_usd_at_time")
            if fees_claimed is None:
                fees_claimed = usd(fee0c, fee1c)
            if deposited is not None and withdrawn is not None:
                u["net_invested"] = deposited - withdrawn

        closed_at = decs[-1].timestamp if (p.closed and decs) else None

        if partial:
            fees_total = fees_unc
            u["fees_are_lower_bound"] = True
        elif fees_claimed is not None and fees_unc is not None:
            fees_total = fees_claimed + fees_unc

        base = deposited or deposited_now
        apr = pnl = pnl_pct = None
        # закрытая позиция перестала зарабатывать в момент вывода — годовые
        # считаем за срок работы, а не «по сегодняшний день»
        end_ts = closed_at if (p.closed and closed_at) else time.time()
        if fees_total is not None and base and base > 0 and opened_at:
            days = max((end_ts - opened_at) / 86400, 0.5)
            u["days"] = days
            roi = fees_total / base
            apr = roi * Decimal(365) / Decimal(str(days)) * 100
            daily = roi / Decimal(str(days))
            try:
                u["fee_apy"] = float(((1 + daily) ** 365 - 1) * 100)
            except Exception:  # noqa: BLE001 — при огромном ROI степень переполняется
                u["fee_apy"] = None

        # PnL и IL требуют знать изъятия — при неполной истории не считаем, чтобы не врать
        if not partial and position_value is not None and base:
            final = position_value + (withdrawn or 0) + (fees_total or 0)
            pnl = final - base
            pnl_pct = (final / base - 1) * 100 if base > 0 else None
        if not partial and deposited_now is not None and position_value is not None:
            hodl = deposited_now
            actual = position_value + (usd(wit0, wit1) or 0)
            u["hodl_value"] = hodl
            u["il"] = actual - hodl
            u["il_pct"] = (actual / hodl - 1) * 100 if hodl > 0 else None

        # ── при какой цене позиция снова будет стоить вложенное
        # (комиссии намеренно не учитываем — с ними безубыток был бы ниже)
        quote_is_token0 = is_stable(p.sym0) and not is_stable(p.sym1)
        sa_raw, sb_raw = sqrt_ratio_at_tick(p.tick_lower), sqrt_ratio_at_tick(p.tick_upper)

        def breakeven_for(target_usd) -> dict | None:
            if p.liquidity == 0 or target_usd is None or target_usd <= 0:
                return None
            if quote_is_token0:
                if pr0 is None:
                    return None
                # считаем в token0: та же формула с подстановкой r=1/s, sa'=1/sb, sb'=1/sa
                target_raw = (target_usd / pr0) * (Decimal(10) ** p.dec0)
                r, status, v_max = solve_breakeven(p.liquidity, 1 / sb_raw, 1 / sa_raw, target_raw)
                price_raw = 1 / (r * r) if r is not None else None
                max_usd = v_max / (Decimal(10) ** p.dec0) * pr0
            else:
                if pr1 is None:
                    return None
                target_raw = (target_usd / pr1) * (Decimal(10) ** p.dec1)
                s, status, v_max = solve_breakeven(p.liquidity, sa_raw, sb_raw, target_raw)
                price_raw = s * s if s is not None else None
                max_usd = v_max / (Decimal(10) ** p.dec1) * pr1
            return {"status": status,
                    "price": price_raw * Decimal(10) ** (p.dec0 - p.dec1)
                             if price_raw is not None else None,
                    "max_value_usd": max_usd, "target_usd": target_usd,
                    "quote_is_token0": quote_is_token0}

        u["breakeven_deposited"] = breakeven_for(deposited)
        if withdrawn:
            u["breakeven_net"] = breakeven_for(u.get("net_invested"))

        hist.update({
            "deposited0": str(dep0), "deposited1": str(dep1),
            "withdrawn0": str(wit0), "withdrawn1": str(wit1),
            "fees_collected0": str(fee0c), "fees_collected1": str(fee1c),
            "opened_at": opened_at, "closed_at": closed_at,
            "events_count": len(events),
        })
    else:
        deposited = withdrawn = fees_claimed = deposited_now = None
        apr = pnl = pnl_pct = None

    # ── описание диапазона для UI
    rng: dict = {}
    if p.tick_cur is not None:
        pl = price_at_tick(p.tick_lower, p.dec0, p.dec1)
        pu = price_at_tick(p.tick_upper, p.dec0, p.dec1)
        pc = price_at_tick(p.tick_cur, p.dec0, p.dec1)
        if p.tick_lower <= MIN_TICK + 10 and p.tick_upper >= MAX_TICK - 10:
            width = "весь диапазон цен (как в V2)"
        elif pu / pl > 100:
            width = "очень широкий"
        else:
            width = f"±{(pu / pl - 1) * 50:.1f}%"
        rng = {"lower": pl, "upper": pu, "current": pc,
               "lower_inv": 1 / pu, "upper_inv": 1 / pl, "current_inv": 1 / pc,
               "width": width, "tick_lower": p.tick_lower, "tick_upper": p.tick_upper,
               "tick_current": p.tick_cur,
               # положение цены внутри диапазона, 0..100 — для полоски в интерфейсе
               "position_pct": max(0.0, min(100.0, float(
                   (p.tick_cur - p.tick_lower) / (p.tick_upper - p.tick_lower) * 100)))
               if p.tick_upper > p.tick_lower else None}
        if not p.in_range and not p.closed:
            rng["side"] = p.sym1 if p.tick_cur >= p.tick_upper else p.sym0

    # ── средняя цена, по которой позиция набирала волатильный актив
    acq = _acquisition(p, hist, has_history)

    fee_label = FEE_TIERS.get(p.fee, f"{p.fee / 10000:g}%")
    detail = {
        "kind": "univ3",
        "pool": p.pool,
        "fee": p.fee, "fee_label": fee_label,
        "token0": {"address": p.token0, "symbol": p.sym0, "decimals": p.dec0},
        "token1": {"address": p.token1, "symbol": p.sym1, "decimals": p.dec1},
        "liquidity": str(p.liquidity),
        "amount0": str(int(p.amount0)), "amount1": str(int(p.amount1)),
        "fees0": str(p.fees0), "fees1": str(p.fees1),
        "range": jsonable(rng),
        "usd": jsonable(u),
        "history": jsonable(hist),
        "acquisition": jsonable(acq),
        "closed": p.closed,
        "explorer_position": f"{chain.explorer}/token/{chain.npm}?a={p.token_id}",
        "explorer_pool": f"{chain.explorer}/address/{p.pool}" if p.pool else "",
    }

    return RawPosition(
        protocol=PROTO_UNIV3, chain=chain.key, external_id=str(p.token_id),
        title=f"{p.sym0}/{p.sym1} {fee_label}",
        subtitle="закрыта" if p.closed else ("в диапазоне" if p.in_range else "вне диапазона"),
        is_open=not p.closed,
        value_usd=float(position_value) if position_value is not None else None,
        debt_usd=None,
        net_usd=float(position_value) if position_value is not None else None,
        fees_unclaimed_usd=float(fees_unc) if fees_unc is not None else None,
        fees_claimed_usd=float(fees_claimed) if fees_claimed is not None else None,
        deposited_usd=float(deposited) if deposited is not None else None,
        withdrawn_usd=float(withdrawn) if withdrawn is not None else None,
        pnl_usd=float(pnl) if pnl is not None else None,
        pnl_pct=float(pnl_pct) if pnl_pct is not None else None,
        apr=float(apr) if apr is not None else None,
        in_range=p.in_range,
        opened_at=opened_at, closed_at=closed_at,
        detail=detail,
    )


# --------------------------------------------------------------------------------------

class UniswapV3Provider(Provider):
    key = PROTO_UNIV3
    title = "Uniswap V3"

    def supports(self, chain: Chain) -> bool:
        return bool(chain.npm)

    def fetch(self, ctx: Ctx, wallet: str, known: dict[str, KnownPosition],
              with_history: bool) -> list[RawPosition]:
        positions = _read_positions(ctx, wallet)
        if not positions:
            return []

        new_events: dict[int, list[EventData]] = {}
        covered: set[int] = set()
        partial: set[int] = set()
        latest = 0

        if with_history:
            # закрытую позицию с уже собранной историей трогать незачем — она иммутабельна
            todo = [p for p in positions
                    if not (known.get(str(p.token_id))
                            and known[str(p.token_id)].history_complete
                            and not known[str(p.token_id)].is_open)]
            if todo:
                latest = ctx.rpc.block_number()
                new_events, covered, partial = _scan_events(ctx, todo, known, latest)

        out: list[RawPosition] = []
        for p in positions:
            k = known.get(str(p.token_id))
            stored = _load_stored_events(ctx, k.db_id) if (k and k.db_id) else []
            fresh = new_events.get(p.token_id, [])

            # объединяем сохранённое и новое, отбрасывая пересечения по (блок, индекс)
            seen = {(e.block, e.log_index) for e in stored}
            merged = stored + [e for e in fresh if (e.block, e.log_index) not in seen]
            _split_collects(merged)

            is_partial = p.token_id in partial
            has_history = bool(merged) and (p.token_id in covered or bool(stored))

            rp = _enrich(ctx, p, merged, is_partial, has_history)
            # отдаём весь набор: _enrich проставил событиям долларовые оценки, и у
            # старых записей они тоже должны осесть в БД (запись идёт через upsert)
            rp.events = merged or None
            # у позиции без ликвидности и без несобранных комиссий история закончена
            rp.history_complete = (p.token_id in covered and not is_partial) or \
                                  (k.history_complete if k else False)
            rp.last_scanned_block = latest if p.token_id in covered else \
                (k.last_scanned_block if k else 0)
            out.append(rp)

        return out
