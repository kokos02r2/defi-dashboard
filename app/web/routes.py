"""Роуты. Каждая страница отдаёт HTML, данные для графиков — JSON.

HTMX-партиалы вынесены в /partials/*: страница обновляет только нужный кусок,
а не перезагружается целиком.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import config
from app.auth import (authenticate, current_user, note_failure, require_user,
                      require_user_api, reset_attempts, throttled)
from app.core.chains import CHAINS
from app.core.lots import known_symbols, resolve_coin, summarize
from app.core.market import market_rates
from app.core.prices import PriceService
from app.db.base import get_session
from app.db.prefs import get_prefs, parse_money, save_prefs
from app.db.models import (Alert, Position, PositionEvent, PositionSnapshot, Snapshot,
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
            temp: float = 0.0) -> dict:
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
                       and p.health_factor < config.ALERT_HEALTH_FACTOR),
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


def _temp_total(db: Session) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(TempDeposit.amount_usd), 0.0))) or 0.0)


def _initial_for(db: Session, wallet_id: int | None) -> float | None:
    """Исходное вложение задано на весь портфель, поэтому при фильтре по одному
    кошельку сравнивать не с чем — возвращаем None, и плитка не показывается."""
    if wallet_id:
        return None
    return get_prefs(db).get("initial_deposit_usd")


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
        "totals": _totals(all_pos, _initial_for(db, wallet_id), _temp_total(db)),
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
        "totals": _totals(all_pos, _initial_for(db, wallet_id), _temp_total(db)),
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
    return JSONResponse({
        "labels": [r.ts.isoformat() for r in rows],
        "net": [r.net_usd for r in rows],
        "value": [r.value_usd for r in rows],
        "debt": [r.debt_usd for r in rows],
        "fees": [r.fees_unclaimed_usd for r in rows],
        "points": len(rows),
    })


# --------------------------------------------------------------------------------------
# Кошельки и обновление
# --------------------------------------------------------------------------------------

@router.get("/wallets", response_class=HTMLResponse)
def wallets_page(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    wallets = list(db.scalars(select(Wallet).order_by(Wallet.id)).all())
    counts = {w.id: db.scalar(select(func.count(Position.id))
                              .where(Position.wallet_id == w.id)) or 0 for w in wallets}
    # только наличие настроек: дёргать getMe на каждый рендер страницы нельзя,
    # это сетевой запрос с секундными таймаутами
    notify_info = {
        "configured": config.telegram_configured(),
        "has_token": bool(config.TELEGRAM_BOT_TOKEN),
        "has_chat": bool(config.TELEGRAM_CHAT_ID),
        "enabled": config.NOTIFY_ENABLED,
        "sent": db.scalar(select(func.count(Alert.id)).where(Alert.notify_state == "sent")) or 0,
        "failed": db.scalar(select(func.count(Alert.id)).where(Alert.notify_state == "failed")) or 0,
        "last": db.scalar(select(Alert).where(Alert.notify_state == "sent")
                          .order_by(Alert.notified_at.desc()).limit(1)),
    }
    return templates.TemplateResponse(request, "wallets.html", {
        "user": user, "wallets": wallets, "counts": counts, "notify": notify_info,
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
    all_pos, _ = _load(db, None, True)
    prefs = get_prefs(db)
    temps = list(db.scalars(select(TempDeposit).order_by(TempDeposit.created_at)).all())
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "prefs": prefs, "status": get_status(), "config": config,
        "totals": _totals(all_pos, prefs.get("initial_deposit_usd"), _temp_total(db)),
        "temps": temps, "temp_total": _temp_total(db),
        "saved": _qs_flag(saved), "error": error,
    })


@router.post("/settings")
def settings_save(initial_deposit: str = Form(""), initial_note: str = Form(""),
                  user: User = Depends(require_user), db: Session = Depends(get_session)):
    try:
        amount = parse_money(initial_deposit)
    except ValueError as e:
        return RedirectResponse(f"/settings?error={e}", status_code=303)
    save_prefs(db, initial_deposit_usd=amount, initial_note=initial_note.strip()[:200])
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/temp/add")
def temp_add(amount: str = Form(""), note: str = Form(""),
             user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Временно заведённая сумма — например, долив залога ради health factor."""
    try:
        value = parse_money(amount)
    except ValueError as e:
        return RedirectResponse(f"/settings?error={e}", status_code=303)
    if not value:
        return RedirectResponse("/settings?error=Укажите сумму", status_code=303)
    db.add(TempDeposit(amount_usd=value, note=note.strip()[:200]))
    db.commit()
    return RedirectResponse("/settings?saved=1#temp", status_code=303)


@router.post("/settings/temp/{tid}/delete")
def temp_delete(tid: int, user: User = Depends(require_user),
                db: Session = Depends(get_session)):
    row = db.get(TempDeposit, tid)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/settings?saved=1#temp", status_code=303)


# --------------------------------------------------------------------------------------
# Партии: по какой цене на самом деле набран актив
# --------------------------------------------------------------------------------------

@router.get("/lots", response_class=HTMLResponse)
def lots_page(request: Request, symbol: str = "", amount: str = "", price: str = "",
              note: str = "", source: str = "", error: str = "",
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Список партий. Параметры запроса заполняют форму — по ссылке со страницы позиции."""
    rows, summary = summarize(db, PriceService(db))
    return templates.TemplateResponse(request, "lots.html", {
        "user": user, "rows": rows, "summary": summary,
        "symbols": known_symbols(db), "status": get_status(), "error": error,
        "prefill": {"symbol": symbol, "amount": amount, "price": price,
                    "note": note, "source": _qs_int(source)},
    })


@router.post("/lots/add")
def lots_add(symbol: str = Form(""), amount: str = Form(""), price: str = Form(""),
             acquired: str = Form(""), note: str = Form(""), source: str = Form(""),
             user: User = Depends(require_user), db: Session = Depends(get_session)):
    try:
        qty = parse_money(amount)
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
