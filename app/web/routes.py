"""Роуты. Каждая страница отдаёт HTML, данные для графиков — JSON.

HTMX-партиалы вынесены в /partials/*: страница обновляет только нужный кусок,
а не перезагружается целиком.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from app import config
from app.auth import (authenticate, current_user, note_failure, require_user,
                      require_user_api, reset_attempts, throttled)
from app.core.btc import btc_price, chart_series as btc_chart, summarize as btc_summarize
from app.core.carry import (chart_series as carry_chart, current as carry_current,
                            history as carry_history, history_series as carry_hist_series,
                            monthly as carry_monthly)
from app.core.chains import CHAINS
from app.core.fees import (accrual_series, accrued, annualized, collected,
                           share_of_base)
from app.core.inrange import for_position as inrange_for, for_positions as inrange_many
from app.core.lots import known_symbols, resolve_coin, summarize
from app.core.market import market_rates
from app.core.prices import PriceService
from app.core.portfolio import net_change
from app.core.pricealerts import DIRECTIONS, SYMBOLS
from app.db.base import get_session
from app.db.prefs import (alert_settings, enabled_chain_keys, get_prefs,
                          parse_amount, parse_money, save_prefs)
from app.db.models import (Alert, BtcBuy, Position, PriceAlert, PositionEvent, PositionSnapshot, Snapshot,
                           TempDeposit, TokenLot, User, Wallet)  # noqa: F401 — Alert нужен для статуса доставки
from app.jobs import scheduler
from app.jobs.refresh import add_wallet, get_status
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------------------
# Вход
# --------------------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_session)):
    if current_user(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_session)):
    ip = request.client.host if request.client else "?"
    wait = throttled(ip)
    if wait:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"Слишком много попыток. Подождите {wait} с."}, status_code=429)

    user = authenticate(db, username.strip(), password)
    if user is None:
        note_failure(ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401)

    reset_attempts(ip)
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------------------
# Сводка
# --------------------------------------------------------------------------------------

def _totals(positions: list[Position], initial: float | None = None,
            temp: float = 0.0, hf_threshold: float | None = None) -> dict:
    open_pos = [p for p in positions if p.is_open]
    value = sum(p.value_usd or 0 for p in open_pos)
    debt = sum(p.debt_usd or 0 for p in open_pos)
    fees = sum(p.fees_unclaimed_usd or 0 for p in open_pos)
    claimed = sum(p.fees_claimed_usd or 0 for p in positions)
    deposited = sum(p.deposited_usd or 0 for p in positions)
    withdrawn = sum(p.withdrawn_usd or 0 for p in positions)
    pnl = sum(p.pnl_usd or 0 for p in positions if p.pnl_usd is not None)

    by_proto: dict[str, float] = {}
    by_chain: dict[str, float] = {}
    for p in open_pos:
        by_proto[p.protocol] = by_proto.get(p.protocol, 0.0) + (p.net_usd or 0)
        by_chain[p.chain] = by_chain.get(p.chain, 0.0) + (p.net_usd or 0)

    # средневзвешенная доходность: по мелкой позиции с APR 900% нельзя судить о портфеле
    weighted = [(p.apr, p.net_usd) for p in open_pos
                if p.apr is not None and p.net_usd and p.net_usd > 0]
    base = sum(w for _, w in weighted)
    apr = sum(a * w for a, w in weighted) / base if base else None

    # ── сравнение с исходным вложением
    #
    # Сравнивается ровно «чистая стоимость» — та же цифра, что в соседней плитке,
    # чтобы они не расходились на экране.
    #
    # Заклеймленные комиссии отдельно НЕ прибавляются: они реинвестируются обратно
    # в позиции, а значит уже сидят внутри текущей стоимости. Прибавив их, мы бы
    # посчитали их дважды.
    #
    # Выведенное тело позиций тоже не участвует: закрытая позиция переоткрывается на
    # те же деньги, поэтому «вложено за всё время» и «выведено» — это один и тот же
    # капитал, прокрученный много раз.
    # Временно заведённые деньги физически лежат внутри позиций, но прибылью не
    # являются — вычитаем их, иначе рост завысится ровно на эту сумму.
    net = value - debt
    adjusted = net - (temp or 0.0)
    vs: dict = {"initial": initial, "temp": temp or 0.0}
    if initial and initial > 0:
        vs.update({
            # без временных — чистый результат самих вложений
            "current": adjusted,
            "delta": adjusted - initial,
            "pct": (adjusted / initial - 1) * 100,
            # с временными — как выглядит портфель целиком, вместе с доливками
            "gross": net,
            "delta_gross": net - initial,
            "pct_gross": (net / initial - 1) * 100,
        })

    return {
        "value": value, "debt": debt, "net": net, "fees_unclaimed": fees,
        "fees_claimed": claimed, "deposited": deposited, "withdrawn": withdrawn,
        "net_invested": deposited - withdrawn, "pnl": pnl,
        "total_with_fees": net + fees, "vs": vs,
        "open_count": len(open_pos), "closed_count": len(positions) - len(open_pos),
        "by_protocol": by_proto, "by_chain": by_chain, "apr": apr,
        "out_of_range": sum(1 for p in open_pos if p.in_range is False),
        "at_risk": sum(1 for p in open_pos if p.health_factor is not None
                       and p.health_factor < (hf_threshold or config.ALERT_HEALTH_FACTOR)),
    }


def _with_change(db: Session, totals: dict, wallet_id: int | None) -> dict:
    """Дополняет итоги изменением чистой стоимости за сутки.

    Считается по снапшотам, а не по разнице с чем-либо в памяти: это тот же ряд, что
    рисует график капитала, поэтому плитка и картинка не могут разойтись.
    """
    delta, pct, _ = net_change(db, totals["net"], hours=24, wallet_id=wallet_id)
    totals["change_24h"] = delta
    totals["change_24h_pct"] = pct
    return totals


def _notify_stats(db: Session) -> dict:
    """Состояние доставки уведомлений — для карточки диагностики.

    Только наличие настроек, без обращения к Telegram: дёргать getMe на каждый рендер
    страницы нельзя, это сетевой запрос с секундными таймаутами.
    """
    return {
        "configured": config.telegram_configured(),
        "has_token": bool(config.TELEGRAM_BOT_TOKEN),
        "has_chat": bool(config.TELEGRAM_CHAT_ID),
        "enabled": config.NOTIFY_ENABLED,
        "sent": db.scalar(select(func.count(Alert.id)).where(Alert.notify_state == "sent")) or 0,
        "failed": db.scalar(select(func.count(Alert.id)).where(Alert.notify_state == "failed")) or 0,
        "last": db.scalar(select(Alert).where(Alert.notify_state == "sent")
                          .order_by(Alert.notified_at.desc()).limit(1)),
    }


def _qs_int(v: str | None) -> int | None:
    """Числовой параметр из строки запроса.

    Селект «Все кошельки» отправляет wallet= с пустым значением, и объявленный
    как int параметр на этом падает с 422. Пустое и мусорное трактуем как «не задано»
    — по той же причине не должна ломаться и сохранённая в закладках ссылка.
    """
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _qs_flag(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _form_num(v: float | None) -> str:
    """Число в поле формы: без хвостовых нулей и без экспоненты.

    Иначе количество 8.3009 вернулось бы в поле как 8.30090000, и при каждой
    правке в базе оседал бы всё более длинный мусор.
    """
    if not v:
        return ""
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _temp_total(db: Session) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(TempDeposit.amount_usd), 0.0))) or 0.0)


def _initial_for(db: Session, wallet_id: int | None) -> float | None:
    """Исходное вложение задано на весь портфель, поэтому при фильтре по одному
    кошельку сравнивать не с чем — возвращаем None, и плитка не показывается."""
    if wallet_id:
        return None
    return get_prefs(db).get("initial_deposit_usd")


def _wallet_groups(db: Session, positions: list[Position],
                   wallet_id: int | None) -> list[dict]:
    """Позиции, разложенные по кошелькам, для сворачиваемых групп в таблице.

    Группы нужны только когда кошельков в таблице больше одного: при фильтре по
    одному кошельку или когда он единственный, заголовок группы был бы шумом —
    и там возвращается один безымянный блок, который шаблон рисует как раньше.
    """
    order: list[int] = []
    by_wallet: dict[int, list[Position]] = {}
    for p in positions:
        if p.wallet_id not in by_wallet:
            by_wallet[p.wallet_id] = []
            order.append(p.wallet_id)
        by_wallet[p.wallet_id].append(p)

    if wallet_id or len(order) < 2:
        return [{"wallet": None, "positions": positions}]

    wallets = {w.id: w for w in db.scalars(
        select(Wallet).where(Wallet.id.in_(order))).all()}
    groups = []
    for wid in order:
        items = by_wallet[wid]
        groups.append({
            "wallet": wallets.get(wid),
            "positions": items,
            "net": sum(p.net_usd or 0 for p in items if p.is_open),
            "fees": sum(p.fees_unclaimed_usd or 0 for p in items if p.is_open),
            "open_count": sum(1 for p in items if p.is_open),
            "count": len(items),
        })
    # крупные кошельки сверху — так же, как позиции внутри группы
    groups.sort(key=lambda g: -(g["net"] or 0))
    return groups


def _load(db: Session, wallet_id: int | None, show_closed: bool) -> tuple[list, list]:
    q = select(Position)
    if wallet_id:
        q = q.where(Position.wallet_id == wallet_id)
    positions = list(db.scalars(q).all())
    visible = [p for p in positions if p.is_open or show_closed]
    visible.sort(key=lambda p: (p.is_open is False, -(p.net_usd or 0)))
    return positions, visible


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, wallet: str | None = None, closed: str = "0",
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    wallet_id, show_closed = _qs_int(wallet), _qs_flag(closed)
    wallets = list(db.scalars(select(Wallet).order_by(Wallet.id)).all())
    all_pos, visible = _load(db, wallet_id, show_closed)
    alerts = list(db.scalars(select(Alert).where(Alert.seen.is_(False))
                             .order_by(desc(Alert.ts)).limit(20)).all())
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "wallets": wallets, "wallet_id": wallet_id,
        "positions": visible,
        "groups": _wallet_groups(db, visible, wallet_id),
        "totals": _with_change(db, _totals(all_pos, _initial_for(db, wallet_id),
                                          _temp_total(db),
                                          alert_settings(db)["health_factor"]), wallet_id),
        "show_closed": show_closed, "alerts": alerts,
        "status": get_status(), "chains": CHAINS,
    })


@router.get("/partials/summary", response_class=HTMLResponse)
def partial_summary(request: Request, wallet: str | None = None, closed: str = "0",
                    user: User = Depends(require_user), db: Session = Depends(get_session)):
    """HTMX опрашивает этот кусок раз в минуту — перезагружать страницу не нужно."""
    wallet_id, show_closed = _qs_int(wallet), _qs_flag(closed)
    all_pos, visible = _load(db, wallet_id, show_closed)
    alerts = list(db.scalars(select(Alert).where(Alert.seen.is_(False))
                             .order_by(desc(Alert.ts)).limit(20)).all())
    return templates.TemplateResponse(request, "partials/body.html", {
        "positions": visible,
        "groups": _wallet_groups(db, visible, wallet_id),
        "totals": _with_change(db, _totals(all_pos, _initial_for(db, wallet_id),
                                          _temp_total(db),
                                          alert_settings(db)["health_factor"]), wallet_id),
        "alerts": alerts,
        "wallet_id": wallet_id, "show_closed": show_closed, "status": get_status(),
    })


# --------------------------------------------------------------------------------------
# Позиция
# --------------------------------------------------------------------------------------

@router.get("/position/{pid}", response_class=HTMLResponse)
def position_detail(request: Request, pid: int, user: User = Depends(require_user),
                    db: Session = Depends(get_session)):
    pos = db.get(Position, pid)
    if pos is None:
        return RedirectResponse("/", status_code=303)
    events = list(db.scalars(select(PositionEvent)
                             .where(PositionEvent.position_id == pid)
                             .order_by(PositionEvent.block, PositionEvent.log_index)).all())
    chain = CHAINS.get(pos.chain)
    return templates.TemplateResponse(request, "position.html", {
        "user": user, "pos": pos, "events": events, "chain": chain,
        "d": pos.detail or {},
        # доля времени в диапазоне: за всё наблюдённое время и за последнюю неделю —
        # вместе они показывают, стало ли лучше или хуже
        "inrange_all": inrange_for(db, pid),
        "inrange_week": inrange_for(db, pid, days=7),
    })


@router.get("/api/position/{pid}/history")
def position_history(pid: int, days: int = Query(30, ge=1, le=3650),
                     user: User = Depends(require_user_api),
                     db: Session = Depends(get_session)):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(select(PositionSnapshot)
                      .where(PositionSnapshot.position_id == pid, PositionSnapshot.ts >= since)
                      .order_by(PositionSnapshot.ts)).all()
    return JSONResponse({
        "labels": [r.ts.isoformat() for r in rows],
        "value": [r.value_usd for r in rows],
        "debt": [r.debt_usd for r in rows],
        "net": [r.net_usd for r in rows],
        "fees": [r.fees_unclaimed_usd for r in rows],
        "health": [r.health_factor for r in rows],
    })


# --------------------------------------------------------------------------------------
# График капитала
# --------------------------------------------------------------------------------------

@router.get("/api/chart")
def chart(days: int = Query(30, ge=1, le=3650), wallet: str | None = None,
          user: User = Depends(require_user_api), db: Session = Depends(get_session)):
    wallet_id = _qs_int(wallet)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(Snapshot).where(Snapshot.ts >= since)
    q = (q.where(Snapshot.wallet_id == wallet_id) if wallet_id
         else q.where(Snapshot.wallet_id.is_(None)))
    rows = db.scalars(q.order_by(Snapshot.ts)).all()
    # Текущая стоимость — из ЖИВЫХ позиций, как в плитке, а не из последнего снапшота:
    # снапшоту до 15 минут, и в углу графика оказалась бы цифра, отличная от плитки на
    # соседнем экране. Разница выглядела бы ошибкой, хотя оба значения «правильные».
    # «За 24 ч» при этом не зависит от выбранного масштаба: сутки — это сутки.
    net_q = select(func.coalesce(func.sum(Position.net_usd), 0.0)).where(
        Position.is_open.is_(True))
    if wallet_id:
        net_q = net_q.where(Position.wallet_id == wallet_id)
    open_count = db.scalar(select(func.count()).select_from(Position).where(
        Position.is_open.is_(True),
        *( [Position.wallet_id == wallet_id] if wallet_id else [] )))
    net_now = float(db.scalar(net_q) or 0.0) if open_count else None
    change = change_pct = span_h = None
    if net_now is not None:
        # период — выбранный на графике, а не всегда сутки: подпись строится по
        # фактической длине, поэтому «за 30д» на трёхдневной истории не появится
        change, change_pct, span_h = net_change(db, net_now, hours=days * 24,
                                                wallet_id=wallet_id, allow_shorter=True)
    return JSONResponse({
        "labels": [r.ts.isoformat() for r in rows],
        "net": [r.net_usd for r in rows],
        "value": [r.value_usd for r in rows],
        "debt": [r.debt_usd for r in rows],
        "fees": [r.fees_unclaimed_usd for r in rows],
        "points": len(rows),
        "net_now": net_now,
        "change": change,
        "change_pct": change_pct,
        "change_hours": round(span_h, 2) if span_h is not None else None,
    })


# --------------------------------------------------------------------------------------
# Собранные комиссии
# --------------------------------------------------------------------------------------

def _qs_date(v: str, end_of_day: bool = False) -> datetime | None:
    """Дата из строки запроса. Мусор трактуем как «не задано», чтобы ссылка из
    закладок не отвечала 422."""
    if not v or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip())
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    # «по 30 июня» человек понимает как «включая 30 июня», а не «до его начала»
    if end_of_day and (d.hour, d.minute, d.second) == (0, 0, 0):
        d = d.replace(hour=23, minute=59, second=59)
    return d


def _month_start(dt: datetime, back: int = 0) -> datetime:
    year, month = dt.year, dt.month - back
    while month <= 0:
        year, month = year - 1, month + 12
    return datetime(year, month, 1, tzinfo=timezone.utc)


@router.get("/fees", response_class=HTMLResponse)
def fees_page(request: Request, wallet: str = "",
              date_from: str = Query("", alias="from"),
              date_to: str = Query("", alias="to"),
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Собранные комиссии по месяцам. Страница, на которую ведёт плитка комиссий.

    Данные для графика уходят в шаблон прямо в разметке, без отдельного /api:
    период меняется перезагрузкой страницы, и второй маршрут ради тех же чисел
    только раздваивал бы логику.
    """
    wallet_id = _qs_int(wallet)
    since = _qs_date(date_from)
    until = _qs_date(date_to, end_of_day=True)
    rep = collected(db, since, until, wallet_id)

    now = datetime.now(timezone.utc)
    presets = [
        ("6 месяцев", _month_start(now, 5)),
        ("12 месяцев", _month_start(now, 11)),
        ("этот год", datetime(now.year, 1, 1, tzinfo=timezone.utc)),
        ("всё время", None),
    ]
    current = since.date().isoformat() if since else ""
    ranges = [{"label": label,
               "from": start.date().isoformat() if start else "",
               "active": (start.date().isoformat() if start else "") == current and not until}
              for label, start in presets]

    unclaimed_q = (select(func.coalesce(func.sum(Position.fees_unclaimed_usd), 0.0))
                   .where(Position.is_open.is_(True)))
    if wallet_id:
        unclaimed_q = unclaimed_q.where(Position.wallet_id == wallet_id)

    # Годовые считаем от исходного вложения из настроек. Оно задано на весь портфель
    # одной суммой, поэтому при фильтре по кошельку делить на него нечестно: получилась
    # бы доходность части денег, отнесённая ко всей сумме.
    base = get_prefs(db).get("initial_deposit_usd")
    if wallet_id:
        apr = apr_share = None
        apr_note = "исходное вложение задано на весь портфель, не на отдельный кошелёк"
    elif not base:
        apr = apr_share = None
        apr_note = "задайте исходное вложение в Настройках"
    elif rep.total <= 0:
        apr = apr_share = None
        apr_note = "в этом периоде комиссий не собрано"
    else:
        apr, apr_share = annualized(rep, base), share_of_base(rep, base)
        apr_note = ("период короче двух недель — годовые из него получились бы случайным числом"
                    if apr is None else "")

    # Время в диапазоне — только по активным позициям: у закрытой границы остались,
    # но стоять в них уже нечему, и цифра была бы бессмысленной.
    active_q = select(Position).where(Position.is_open.is_(True),
                                     Position.protocol == "uniswap_v3")
    if wallet_id:
        active_q = active_q.where(Position.wallet_id == wallet_id)
    active = list(db.scalars(active_q).all())
    carry = carry_current(db, wallet_id)
    # факт по замерам: пока их мало, шаблон покажет, что история копится
    carry_hist = carry_history(db, days=90, wallet_id=wallet_id)
    # для графика по месяцам берём всю доступную историю, а не выбранный период:
    # период фильтрует комиссии, а расход на займ есть только с начала записи
    carry_months = carry_monthly(carry_history(db, days=None, wallet_id=wallet_id))

    # начисленные комиссии: сколько накапало по дням, а не сколько забрали
    accrual = accrued(db, days=90, wallet_id=wallet_id)
    # «за прошлые сутки» — последний ПОЛНЫЙ день: сегодняшний ещё не кончился,
    # и показывать его как суточный итог значило бы занижать
    full_days = [d for d in accrual.days if d.hours >= 20]
    coverage = inrange_many(db, [p.id for p in active])
    inrange_rows = sorted(
        [(p, coverage[p.id]) for p in active if p.id in coverage and coverage[p.id].reliable],
        key=lambda pair: pair[1].pct or 0, reverse=True)

    return templates.TemplateResponse(request, "fees.html", {
        "user": user, "status": get_status(), "rep": rep,
        "wallets": list(db.scalars(select(Wallet).order_by(Wallet.id)).all()),
        "wallet_id": wallet_id, "ranges": ranges,
        "base": base, "apr": apr, "apr_share": apr_share, "apr_note": apr_note,
        "inrange_rows": inrange_rows,
        # плата за плечо: приход комиссий без неё отвечает лишь на половину вопроса
        "carry": carry, "carry_chart": carry_chart(carry),
        "carry_hist": carry_hist, "carry_hist_chart": carry_hist_series(carry_hist),
        "accrual": accrual, "accrual_chart": accrual_series(accrual),
        "accrual_last": full_days[-1] if full_days else None,
        "snapshot_minutes": max(round(config.SNAPSHOT_INTERVAL / 60), 1),
        "date_from": since.date().isoformat() if since else "",
        "date_to": until.date().isoformat() if until else "",
        "unclaimed": float(db.scalar(unclaimed_q) or 0.0),
        # На графике комиссий рядом с приходом — фактический расход на займ по тем же
        # месяцам. Где данных ещё нет, ставим null, а не ноль: ноль означал бы
        # «плечо было бесплатным», и месяц до начала записи выглядел бы выгодным.
        "chart": {"labels": [b.label for b in rep.buckets],
                  "usd": [round(b.usd, 2) for b in rep.buckets],
                  "cumulative": [round(b.cumulative, 2) for b in rep.buckets],
                  "events": [b.events for b in rep.buckets],
                  "carry": [(-round(carry_months[b.key].cost, 2)
                             if b.key in carry_months else None) for b in rep.buckets],
                  "carry_cov": [(round(carry_months[b.key].coverage or 0)
                                 if b.key in carry_months else None) for b in rep.buckets]},
    })


# --------------------------------------------------------------------------------------
# Кошельки и обновление
# --------------------------------------------------------------------------------------

@router.get("/wallets", response_class=HTMLResponse)
def wallets_page(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    """Только кошельки. Состояние синхронизации и доставки переехало в Настройки:
    к кошелькам оно не относится, а искали его именно там."""
    wallets = list(db.scalars(select(Wallet).order_by(Wallet.id)).all())
    counts = {w.id: db.scalar(select(func.count(Position.id))
                              .where(Position.wallet_id == w.id)) or 0 for w in wallets}
    return templates.TemplateResponse(request, "wallets.html", {
        "user": user, "wallets": wallets, "counts": counts,
        "status": get_status(), "config": config,
    })


@router.post("/wallets/add")
def wallets_add(address: str = Form(...), label: str = Form(""),
                user: User = Depends(require_user)):
    try:
        w = add_wallet(address, label)
        # новый кошелёк — сразу полный sync, иначе он будет пустым до завтра
        scheduler.run_async("sync", w.id)
    except Exception as e:  # noqa: BLE001 — покажем текст ошибки на странице
        return RedirectResponse(f"/wallets?error={type(e).__name__}: {e}", status_code=303)
    return RedirectResponse("/wallets", status_code=303)


@router.post("/wallets/{wid}/toggle")
def wallets_toggle(wid: int, user: User = Depends(require_user),
                   db: Session = Depends(get_session)):
    w = db.get(Wallet, wid)
    if w:
        w.enabled = not w.enabled
        db.commit()
    return RedirectResponse("/wallets", status_code=303)


@router.post("/wallets/{wid}/delete")
def wallets_delete(wid: int, user: User = Depends(require_user),
                   db: Session = Depends(get_session)):
    w = db.get(Wallet, wid)
    if w:
        db.delete(w)
        db.commit()
    return RedirectResponse("/wallets", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "0", error: str = "",
                  user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Настройки приложения. Только настройки: журналы своих денег переехали в /money,
    диагностика — карточкой ниже на этой же странице."""
    picked = set(enabled_chain_keys(db))
    per_chain = {ch: (total, open_) for ch, total, open_ in db.execute(
        select(Position.chain, func.count(),
               func.coalesce(func.sum(case((Position.is_open.is_(True), 1), else_=0)), 0))
        .group_by(Position.chain)).all()}
    chain_rows = [{"key": key, "name": ch.name, "on": key in picked,
                   "total": per_chain.get(key, (0, 0))[0],
                   "open": per_chain.get(key, (0, 0))[1]}
                  for key, ch in CHAINS.items()]
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "status": get_status(), "config": config,
        "chain_rows": chain_rows, "notify": _notify_stats(db),
        "conf": alert_settings(db),
        "saved": _qs_flag(saved), "error": error,
    })


@router.get("/money", response_class=HTMLResponse)
def money_page(request: Request, saved: str = "0", error: str = "", edit: str = "",
               user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Свои деньги: исходное вложение и временные доливки.

    Журнал, а не настройка — поэтому живёт рядом с партиями и покупками BTC, а не в
    Настройках, где лежал раньше.
    """
    all_pos, _ = _load(db, None, True)
    prefs = get_prefs(db)
    temps = list(db.scalars(select(TempDeposit).order_by(TempDeposit.created_at)).all())
    tid = _qs_int(edit)
    editing_temp = db.get(TempDeposit, tid) if tid else None
    return templates.TemplateResponse(request, "money.html", {
        "user": user, "prefs": prefs, "status": get_status(), "config": config,
        "totals": _totals(all_pos, prefs.get("initial_deposit_usd"), _temp_total(db),
                          alert_settings(db)["health_factor"]),
        "temps": temps, "temp_total": _temp_total(db),
        "editing_temp": editing_temp,
        "temp_prefill": {"amount": _form_num(editing_temp.amount_usd) if editing_temp else "",
                         "note": editing_temp.note if editing_temp else ""},
        "saved": _qs_flag(saved), "error": error,
    })


@router.post("/money/initial")
def money_initial(initial_deposit: str = Form(""), initial_note: str = Form(""),
                  user: User = Depends(require_user), db: Session = Depends(get_session)):
    try:
        amount = parse_money(initial_deposit)
    except ValueError as e:
        return RedirectResponse(f"/money?error={e}", status_code=303)
    save_prefs(db, initial_deposit_usd=amount, initial_note=initial_note.strip()[:200])
    return RedirectResponse("/money?saved=1", status_code=303)


@router.post("/money/temp/add")
def temp_add(amount: str = Form(""), note: str = Form(""),
             user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Временно заведённая сумма — например, долив залога ради health factor."""
    try:
        value = parse_money(amount)
    except ValueError as e:
        return RedirectResponse(f"/money?error={e}", status_code=303)
    if not value:
        return RedirectResponse("/money?error=Укажите сумму", status_code=303)
    db.add(TempDeposit(amount_usd=value, note=note.strip()[:200]))
    db.commit()
    return RedirectResponse("/money?saved=1#temp", status_code=303)


@router.post("/settings/chains")
def settings_chains(chains: list[str] = Form(default=[]),
                    user: User = Depends(require_user),
                    db: Session = Depends(get_session)):
    """Какие сети опрашивать. Действует со следующего цикла, без перезапуска.

    Пустой выбор не принимаем: он означал бы «не опрашивать ничего», и дашборд молча
    перестал бы обновляться — состояние, которое не должно быть достижимым из UI.
    """
    picked = [k for k in chains if k in CHAINS]
    if not picked:
        return RedirectResponse("/settings?error=Оставьте хотя бы одну сеть#chains",
                                status_code=303)
    save_prefs(db, enabled_chains=picked)
    return RedirectResponse("/settings?saved=1#chains", status_code=303)


@router.post("/money/temp/{tid}/edit")
def temp_edit(tid: int, amount: str = Form(""), note: str = Form(""),
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Правка временной суммы.

    created_at не трогаем: это когда запись создана, а не когда деньги завели.
    Переписывать её при исправлении опечатки в сумме означало бы подменять
    историю — а по этой дате видно, что и когда доливалось.
    """
    row = db.get(TempDeposit, tid)
    if row is None:
        return RedirectResponse("/money?error=Запись не найдена", status_code=303)
    try:
        value = parse_money(amount)
    except ValueError as e:
        return RedirectResponse(f"/money?edit={tid}&error={e}#temp", status_code=303)
    if not value:
        return RedirectResponse(f"/money?edit={tid}&error=Укажите сумму#temp",
                                status_code=303)
    row.amount_usd, row.note = value, note.strip()[:200]
    db.commit()
    return RedirectResponse("/money?saved=1#temp", status_code=303)


@router.post("/money/temp/{tid}/delete")
def temp_delete(tid: int, user: User = Depends(require_user),
                db: Session = Depends(get_session)):
    row = db.get(TempDeposit, tid)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/money?saved=1#temp", status_code=303)


# --------------------------------------------------------------------------------------
# Партии: по какой цене на самом деле набран актив
# --------------------------------------------------------------------------------------

@router.get("/lots", response_class=HTMLResponse)
def lots_page(request: Request, symbol: str = "", amount: str = "", price: str = "",
              note: str = "", source: str = "", error: str = "", edit: str = "",
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Список партий. Параметры запроса заполняют форму — по ссылке со страницы позиции.

    edit=ID переводит ту же форму в режим правки: поля у добавления и у изменения
    одни и те же, отдельная страница только размножала бы разметку.
    """
    rows, summary = summarize(db, PriceService(db))
    lid = _qs_int(edit)
    editing = db.get(TokenLot, lid) if lid else None
    if editing is not None:
        prefill = {"symbol": editing.symbol,
                   "amount": _form_num(editing.amount),
                   "price": _form_num(editing.avg_price_usd),
                   "note": editing.note,
                   "source": editing.source_position_id,
                   # <input type="date"> понимает только ISO-формат
                   "acquired": editing.acquired_at.strftime("%Y-%m-%d")
                               if editing.acquired_at else ""}
    else:
        prefill = {"symbol": symbol, "amount": amount, "price": price,
                   "note": note, "source": _qs_int(source), "acquired": ""}
    return templates.TemplateResponse(request, "lots.html", {
        "user": user, "rows": rows, "summary": summary,
        "symbols": known_symbols(db), "status": get_status(), "error": error,
        "editing": editing, "prefill": prefill,
    })


@router.post("/lots/add")
def lots_add(symbol: str = Form(""), amount: str = Form(""), price: str = Form(""),
             acquired: str = Form(""), note: str = Form(""), source: str = Form(""),
             user: User = Depends(require_user), db: Session = Depends(get_session)):
    try:
        qty = parse_amount(amount)      # «1,234» — это 1.234 токена, не 1234
        avg = parse_money(price)
    except ValueError as e:
        return RedirectResponse(f"/lots?error={e}", status_code=303)
    sym = symbol.strip()[:32]
    if not sym or not qty or not avg:
        return RedirectResponse("/lots?error=Нужны актив, количество и средняя цена",
                                status_code=303)
    when = None
    if acquired.strip():
        try:
            when = datetime.fromisoformat(acquired.strip())
        except ValueError:
            when = None
    db.add(TokenLot(symbol=sym, coin=resolve_coin(db, sym), amount=qty,
                    avg_price_usd=avg, acquired_at=when or datetime.now(timezone.utc),
                    note=note.strip()[:200], source_position_id=_qs_int(source)))
    db.commit()
    return RedirectResponse("/lots", status_code=303)


@router.post("/lots/{lid}/edit")
def lots_edit(lid: int, symbol: str = Form(""), amount: str = Form(""),
              price: str = Form(""), acquired: str = Form(""), note: str = Form(""),
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Правка партии.

    source_position_id не трогаем: связь с позицией, из которой партия родилась,
    правкой цифр не меняется, и потерять её тут было бы обидно — по ней открывается
    исходная позиция из таблицы.
    """
    row = db.get(TokenLot, lid)
    if row is None:
        return RedirectResponse("/lots?error=Партия не найдена", status_code=303)
    try:
        qty = parse_amount(amount)      # «1,234» — это 1.234 токена, не 1234
        avg = parse_money(price)
    except ValueError as e:
        return RedirectResponse(f"/lots?edit={lid}&error={e}", status_code=303)
    sym = symbol.strip()[:32]
    if not sym or not qty or not avg:
        return RedirectResponse(
            f"/lots?edit={lid}&error=Нужны актив, количество и средняя цена",
            status_code=303)

    if sym != row.symbol:
        # сменился актив — ключ цены DefiLlama пересчитываем, иначе «Сейчас»
        # осталось бы от прежней монеты
        row.coin = resolve_coin(db, sym)
    row.symbol, row.amount, row.avg_price_usd = sym, qty, avg
    row.note = note.strip()[:200]
    if acquired.strip():
        try:
            row.acquired_at = datetime.fromisoformat(acquired.strip())
        except ValueError:
            pass          # мусор в дате не должен затирать нормальное значение
    else:
        # На добавлении пустая дата означает «сейчас», здесь — «дату не знаю»:
        # человек её осознанно стёр, и подставлять сегодняшнюю было бы враньём.
        row.acquired_at = None
    db.commit()
    return RedirectResponse("/lots", status_code=303)


@router.post("/lots/{lid}/delete")
def lots_delete(lid: int, user: User = Depends(require_user),
                db: Session = Depends(get_session)):
    row = db.get(TokenLot, lid)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/lots", status_code=303)


@router.post("/refresh")
def do_refresh(mode: str = Form("live"), user: User = Depends(require_user)):
    scheduler.run_async("sync" if mode == "sync" else "live")
    return RedirectResponse("/", status_code=303)


@router.get("/partials/market", response_class=HTMLResponse)
def partial_market(request: Request, user: User = Depends(require_user),
                   db: Session = Depends(get_session)):
    """Тикер ETH/BTC в шапке. Отдельным фрагментом, чтобы обновляться самому и не
    тянуть за собой перерисовку страницы."""
    return templates.TemplateResponse(request, "partials/market.html",
                                      {"rates": market_rates(db)})


@router.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request, user: User = Depends(require_user)):
    """Плашка «обновляется…» в шапке. Своим фрагментом, потому что htmx на
    дашборде подменяет только блок позиций, а шапка в него не входит. Опрос чаще
    цикла обновления: индикатор должен загораться и гаснуть заметно, иначе он
    бесполезен."""
    return templates.TemplateResponse(request, "partials/status.html",
                                      {"status": get_status()})


# --------------------------------------------------------------------------------------
# Журнал покупок BTC. Сознательно ни во что не входит — см. модель BtcBuy.
# --------------------------------------------------------------------------------------

@router.get("/btc", response_class=HTMLResponse)
def btc_page(request: Request, error: str = "", edit: str = "",
             user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Журнал покупок BTC. edit=ID переводит форму в режим правки, как на партиях."""
    price_now = btc_price(db)
    summary = btc_summarize(db, price_now)
    bid = _qs_int(edit)
    editing = db.get(BtcBuy, bid) if bid else None
    if editing is not None:
        prefill = {"amount": _form_num(editing.amount_btc),
                   "price": _form_num(editing.price_usd),
                   "note": editing.note,
                   "bought": editing.bought_at.strftime("%Y-%m-%d") if editing.bought_at else ""}
    else:
        prefill = {"amount": "", "price": "", "note": "", "bought": ""}
    return templates.TemplateResponse(request, "btc.html", {
        "user": user, "status": get_status(), "s": summary, "error": error,
        "editing": editing, "prefill": prefill,
        "chart": btc_chart(summary),
    })


def _btc_fields(amount: str, price: str) -> tuple[float, float] | RedirectResponse:
    """Разбор количества и цены, общий для добавления и правки."""
    try:
        qty = parse_amount(amount)      # «0,001» должно остаться тысячной долей
        px = parse_money(price)
    except ValueError as e:
        return RedirectResponse(f"/btc?error={e}", status_code=303)
    if not qty or not px:
        return RedirectResponse("/btc?error=Нужны количество BTC и цена покупки",
                                status_code=303)
    return qty, px


@router.post("/btc/add")
def btc_add(amount: str = Form(""), price: str = Form(""), bought: str = Form(""),
            note: str = Form(""), user: User = Depends(require_user),
            db: Session = Depends(get_session)):
    parsed = _btc_fields(amount, price)
    if isinstance(parsed, RedirectResponse):
        return parsed
    qty, px = parsed
    when = None
    if bought.strip():
        try:
            when = datetime.fromisoformat(bought.strip())
        except ValueError:
            when = None
    db.add(BtcBuy(amount_btc=qty, price_usd=px, note=note.strip()[:200],
                  bought_at=when or datetime.now(timezone.utc)))
    db.commit()
    return RedirectResponse("/btc", status_code=303)


@router.post("/btc/{bid}/edit")
def btc_edit(bid: int, amount: str = Form(""), price: str = Form(""),
             bought: str = Form(""), note: str = Form(""),
             user: User = Depends(require_user), db: Session = Depends(get_session)):
    row = db.get(BtcBuy, bid)
    if row is None:
        return RedirectResponse("/btc?error=Покупка не найдена", status_code=303)
    parsed = _btc_fields(amount, price)
    if isinstance(parsed, RedirectResponse):
        # ошибку возвращаем в ту же форму, а не на пустое добавление
        return RedirectResponse(f"/btc?edit={bid}&error=Проверьте количество и цену",
                                status_code=303)
    row.amount_btc, row.price_usd = parsed
    row.note = note.strip()[:200]
    if bought.strip():
        try:
            row.bought_at = datetime.fromisoformat(bought.strip())
        except ValueError:
            pass          # мусор в дате не должен затирать нормальное значение
    else:
        row.bought_at = None
    db.commit()
    return RedirectResponse("/btc", status_code=303)


@router.post("/btc/{bid}/delete")
def btc_delete(bid: int, user: User = Depends(require_user),
               db: Session = Depends(get_session)):
    row = db.get(BtcBuy, bid)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/btc", status_code=303)


# --------------------------------------------------------------------------------------
# Оповещения о цене
# --------------------------------------------------------------------------------------

@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, error: str = "", saved: str = "0",
                user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Оповещения о пересечении цены. Проверяются в минутном цикле, см. core/pricealerts."""
    rows = list(db.scalars(select(PriceAlert).order_by(desc(PriceAlert.id))).all())
    prices = {r["symbol"]: r["price"] for r in market_rates(db) if r.get("kind") == "crypto"}
    return templates.TemplateResponse(request, "alerts.html", {
        "user": user, "status": get_status(), "rows": rows, "error": error,
        "symbols": SYMBOLS, "directions": DIRECTIONS, "prices": prices,
        "telegram_ok": config.telegram_configured(),
        # пороги по позициям — здесь же: это тоже оповещения, просто не про цену
        "conf": alert_settings(db), "saved": _qs_flag(saved),
    })


@router.post("/alerts/thresholds")
def alerts_thresholds(health: str = Form(""), cooldown: str = Form(""),
                      out_of_range: str = Form(""), user: User = Depends(require_user),
                      db: Session = Depends(get_session)):
    """Пороги оповещений о позициях. Действуют со следующего цикла, без перезапуска."""
    try:
        hf = parse_money(health)
        cd = parse_money(cooldown)
    except ValueError as e:
        return RedirectResponse(f"/alerts?error={e}#thresholds", status_code=303)
    if not hf or not (1.0 <= hf <= 10.0):
        return RedirectResponse(
            "/alerts?error=Порог health factor задаётся между 1.0 и 10.0#thresholds",
            status_code=303)
    if cd is None or not (0 <= cd <= 24 * 60):
        return RedirectResponse("/alerts?error=Пауза задаётся в минутах, до 1440#thresholds",
                                status_code=303)
    save_prefs(db, alert_health_factor=hf, alert_cooldown=int(cd * 60),
               alert_out_of_range=_qs_flag(out_of_range))
    return RedirectResponse("/alerts?saved=1#thresholds", status_code=303)


@router.post("/alerts/add")
def alerts_add(symbol: str = Form(""), price: str = Form(""), direction: str = Form(""),
               note: str = Form(""), user: User = Depends(require_user),
               db: Session = Depends(get_session)):
    if symbol not in SYMBOLS or direction not in DIRECTIONS:
        return RedirectResponse("/alerts?error=Выберите монету и направление", status_code=303)
    try:
        target = parse_money(price)
    except ValueError as e:
        return RedirectResponse(f"/alerts?error={e}", status_code=303)
    if not target:
        return RedirectResponse("/alerts?error=Укажите цену", status_code=303)

    # last_price ставим сразу: иначе первая проверка ушла бы на «заряжание», и переход,
    # случившийся в ту же минуту, потерялся бы
    now = {r["symbol"]: r["price"] for r in market_rates(db) if r.get("kind") == "crypto"}
    db.add(PriceAlert(symbol=symbol, price=target, direction=direction,
                      note=note.strip()[:200], last_price=now.get(symbol)))
    db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{aid}/arm")
def alerts_arm(aid: int, user: User = Depends(require_user),
               db: Session = Depends(get_session)):
    """Взвести заново после срабатывания или выключить."""
    row = db.get(PriceAlert, aid)
    if row is not None:
        row.enabled = not row.enabled
        if row.enabled:
            # сбрасываем отметку срабатывания и заряжаем текущей ценой
            row.triggered_at = row.triggered_price = None
            now = {r["symbol"]: r["price"] for r in market_rates(db) if r.get("kind") == "crypto"}
            row.last_price = now.get(row.symbol)
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{aid}/delete")
def alerts_delete(aid: int, user: User = Depends(require_user),
                  db: Session = Depends(get_session)):
    row = db.get(PriceAlert, aid)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.get("/calc", response_class=HTMLResponse)
def calc_page(request: Request, user: User = Depends(require_user)):
    """Калькулятор пула. Вся арифметика в браузере — состояния и запросов не нужно."""
    return templates.TemplateResponse(request, "calc.html", {"user": user})


@router.get("/api/market")
def api_market(user: User = Depends(require_user_api), db: Session = Depends(get_session)):
    return JSONResponse({"rates": market_rates(db)})


@router.get("/api/status")
def api_status(user: User = Depends(require_user_api)):
    return JSONResponse(get_status())


@router.post("/alerts/seen")
def alerts_seen(user: User = Depends(require_user), db: Session = Depends(get_session)):
    for a in db.scalars(select(Alert).where(Alert.seen.is_(False))).all():
        a.seen = True
    db.commit()
    return RedirectResponse("/", status_code=303)
