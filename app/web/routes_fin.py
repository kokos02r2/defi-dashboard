"""Роуты личных финансов — второе пространство дашборда, префикс /fin.

Отдельный модуль, а не продолжение routes.py: у этих страниц нет ничего общего с
позициями, кошельками и нодами, и держать их в одном файле означало бы, что правка
формы расходов способна уронить дашборд портфеля.

С крипто-частью пересекаются только вход и вёрстка. Ни один расчёт отсюда не попадает
в чистую стоимость портфеля.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.auth import require_user
from app.core import finance as fin
from app.core import finimport as imp
from app.core import fx
from app.core.fmt import plural
from app.db.base import get_session
from app.db.models import (KV, FinAccount, FinBalance, FinCategory, FinDebt,
                           FinImportBatch, FinRule, FinTx, User, utcnow)
from app.db.prefs import base_currency, get_prefs, save_prefs
from app.web.templating import templates

log = logging.getLogger(__name__)


def _ready(db: Session = Depends(get_session)) -> None:
    """Стартовый список категорий заводится при первом обращении к разделу.

    Зависимостью роутера, а не вызовом в каждой странице: иначе набор категорий
    появлялся бы только после захода на «правильный» экран, и форма добавления
    операции могла открыться с пустым списком. Повторные вызовы ничего не делают —
    флаг в настройках проверяется одним чтением.
    """
    fin.seed_categories(db)
    fin.seed_rules(db)


router = APIRouter(prefix="/fin", dependencies=[Depends(_ready)])

UPLOAD_DIR = config.DATA_DIR / "imports"
UPLOAD_TTL = 7 * 24 * 3600     # незавершённые загрузки не должны лежать вечно
PAGE_SIZE = 100


def _ctx(db: Session, **extra) -> dict:
    """Общее для всех страниц раздела: валюта отчётов и признак пространства.

    space читает base.html: от него зависят меню и то, показывать ли кнопки
    обновления портфеля — в личных финансах обновлять нечего.
    """
    return {"space": "fin", "base": base_currency(db), **extra}


def _redirect(path: str, **params) -> RedirectResponse:
    """Переход с добавлением параметров к тем, что уже есть в адресе.

    Склеивать через «?» нельзя: путь возврата почти всегда уже несёт фильтры
    («/fin/tx?m=2026-07&account=3»), и второй знак вопроса превращал месяц в
    «2026-07?saved=1» — фильтр молча слетал после каждого сохранения.
    """
    path, _, query = path.partition("?")
    items = [(k, v) for k, v in parse_qsl(query) if k not in params]
    items += [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    return RedirectResponse(f"{path}?{urlencode(items)}" if items else path,
                            status_code=303)


def _import_from(db: Session) -> date | None:
    """С какой даты брать операции из выписок. Пусто — брать все."""
    raw = str(get_prefs(db).get("fin_import_from") or "").strip()
    return imp.parse_day(raw) if raw else None


def _month(m: str | None, db: Session) -> tuple[date, date, str]:
    """Границы выбранного месяца. Без выбора — последний месяц, где есть операции.

    Именно последний с данными, а не текущий: выписку заливают за прошедший месяц, и
    первого числа пустой экран «в этом месяце ничего» выглядел бы как потеря данных.
    """
    if m and re.fullmatch(r"\d{4}-\d{2}", m):
        y, mo = int(m[:4]), int(m[5:7])
        if 1 <= mo <= 12:
            start, end = fin.month_bounds(date(y, mo, 1))
            return start, end, m
    have = fin.available_months(db)
    key = have[0] if have else date.today().strftime("%Y-%m")
    y, mo = int(key[:4]), int(key[5:7])
    start, end = fin.month_bounds(date(y, mo, 1))
    return start, end, key


# --------------------------------------------------------------------------------------
# Обзор
# --------------------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def overview(request: Request, m: str | None = None, user: User = Depends(require_user),
             db: Session = Depends(get_session)):
    start, end, key = _month(m, db)
    prev_start, prev_end = fin.month_bounds(start, back=1)

    now = fin.summary(db, start, end)
    before = fin.summary(db, prev_start, prev_end)
    months = fin.monthly_series(db, months=24)

    expense_rows = fin.by_category(db, start, end, "expense")
    income_rows = fin.by_category(db, start, end, "income")
    # Записанные руками остатки — к выбранному месяцу не привязаны: показываем
    # последнее, что известно, иначе плитка пропадала бы при листании месяцев
    money = fin.snapshots(db, limit=2)

    return templates.TemplateResponse(request, "fin/overview.html", _ctx(
        db, user=user, month=key, months_have=fin.available_months(db),
        s=now, prev=before,
        expense_rows=expense_rows, income_rows=income_rows,
        by_account=fin.by_account(db, start, end, "expense"),
        top=fin.top_expenses(db, start, end, limit=10),
        recurring=fin.recurring(db, months=6)[:12],
        money=money[0] if money else None, money_prev=money[1] if len(money) > 1 else None,
        series=[{"month": r.month, "income": round(r.income, 2),
                 "expense": round(r.expense, 2), "net": round(r.net, 2)} for r in months],
    ))


# --------------------------------------------------------------------------------------
# Операции
# --------------------------------------------------------------------------------------

TX_FLASH = ("saved", "error", "applied", "hint")   # не фильтры, в путь возврата не идут


def _tx_view(db: Session, user: User, p: dict) -> dict:
    """Список операций по фильтрам p — тому, что стоит в адресной строке.

    Вынесено из страницы отдельно, потому что этот же кусок отдаётся в ответ на
    каждое действие со списком: htmx подменяет только его, страница целиком не
    перезагружается и выставленные фильтры остаются на месте.
    """
    account, category = p.get("account", ""), p.get("category", "")
    kind, q, uncat = p.get("kind", ""), p.get("q", ""), p.get("uncat", "")
    start, end, key = _month(p.get("m"), db)
    every = p.get("all_time") == "1"

    conds = []
    if not every:
        conds += [FinTx.day >= start, FinTx.day <= end]
    if account.isdigit():
        conds.append(FinTx.account_id == int(account))
    if category == "none":
        conds.append(FinTx.category_id.is_(None))
    elif category.isdigit():
        conds.append(FinTx.category_id == int(category))
    if kind in fin.KINDS:
        conds.append(FinTx.kind == kind)
    if q.strip():
        conds.append(FinTx.note.ilike(f"%{q.strip()}%"))
    if uncat == "1":
        conds += [FinTx.category_id.is_(None), FinTx.excluded.is_(False)]

    total = int(db.scalar(select(func.count()).select_from(FinTx).where(*conds)) or 0)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(int(p["page"]) if str(p.get("page", "")).isdigit() else 1, pages)
    page = max(page, 1)
    rows = list(db.scalars(
        select(FinTx).where(*conds)
        .order_by(FinTx.day.desc(), FinTx.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))

    # Итог по отфильтрованному, а не по странице: иначе цифра меняется от листания
    sums = dict(db.execute(
        select(FinTx.kind, func.sum(FinTx.amount_base))
        .where(*conds).where(FinTx.excluded.is_(False))
        .group_by(FinTx.kind)).all())

    edit = p.get("edit", "")
    editing = db.get(FinTx, int(edit)) if edit.isdigit() else None

    # Ссылки листания должны сохранять фильтры, иначе вторая страница «всех расходов
    # за год» молча превращается в первую страницу без фильтров
    keep = [(k, v) for k, v in p.items() if k not in TX_FLASH and k != "edit" and v]
    qs = urlencode([(k, v) for k, v in keep if k != "page"])
    back = f"/fin/tx?{urlencode(keep)}" if keep else "/fin/tx"

    return dict(
        user=user, rows=rows, month=key, months_have=fin.available_months(db),
        all_time=every, page=page, pages=pages, total=total,
        income_sum=float(sums.get("income") or 0), expense_sum=float(sums.get("expense") or 0),
        accounts=fin.accounts(db, with_archived=True), cats=fin.categories(db),
        f={"account": account, "category": category, "kind": kind, "q": q, "uncat": uncat},
        editing=editing, qs=qs, back=back,
        saved=p.get("saved", ""), error=p.get("error", ""),
        applied=p.get("applied", ""), hint=p.get("hint", ""),
        today=date.today().isoformat(), currencies=fx.CURRENCIES,
    )


@router.get("/tx", response_class=HTMLResponse)
def tx_page(request: Request, user: User = Depends(require_user),
            db: Session = Depends(get_session)):
    ctx = _tx_view(db, user, dict(request.query_params))
    return templates.TemplateResponse(request, "fin/tx.html", _ctx(db, **ctx))


def _after(request: Request, db: Session, user: User, back: str, show: int | None = None,
           **flash):
    """Ответ на действие со списком: правку, удаление, категорию.

    Обычному браузеру — переход на тот же адрес с теми же фильтрами. htmx-запросу —
    один перерисованный кусок страницы: фильтры, поиск и выбранный месяц человек
    выставил руками, и терять их после каждой галочки нельзя.
    """
    if request.headers.get("HX-Request") != "true":
        return _redirect(back, **flash)
    p = dict(parse_qsl(back.partition("?")[2]))
    p.update({k: str(v) for k, v in flash.items() if v not in (None, "")})
    ctx = _tx_view(db, user, p)
    if show is not None and all(r.id != show for r in ctx["rows"]):
        ctx["hint"] = "операция не подходит под фильтр — в списке её не видно"
    resp = templates.TemplateResponse(request, "fin/_tx_live.html", _ctx(db, **ctx))
    # Адресная строка должна остаться честной: обновление страницы по F5 покажет то
    # же, что и сейчас на экране, вместе с фильтрами
    resp.headers["HX-Push-Url"] = ctx["back"]
    return resp


def _tx_form(db: Session, account: str, day: str, amount: str, currency: str,
             kind: str) -> tuple[FinAccount, date, float, str, str] | str:
    """Проверка полей формы операции. Возвращает разобранное или текст ошибки."""
    acc = db.get(FinAccount, int(account)) if account.isdigit() else None
    if acc is None:
        return "не выбран счёт"
    d = imp.parse_day(day) or date.today()
    if d > date.today():
        return "дата в будущем"
    value = imp.parse_number(amount)
    if value is None or value == 0:
        return f"не похоже на сумму: {amount!r}"
    cur = (currency or acc.currency or "EUR").upper()
    return acc, d, abs(value), cur, ("income" if kind == "income" else "expense")


@router.post("/tx/add")
def tx_add(request: Request, account: str = Form(""), day: str = Form(""),
           amount: str = Form(""), currency: str = Form(""), kind: str = Form("expense"),
           category: str = Form(""), note: str = Form(""), back: str = Form("/fin/tx"),
           user: User = Depends(require_user), db: Session = Depends(get_session)):
    parsed = _tx_form(db, account, day, amount, currency, kind)
    if isinstance(parsed, str):
        return _after(request, db, user, back, error=parsed)
    acc, d, value, cur, k = parsed
    tx = fin.add_tx(db, account=acc, day=d, kind=k, amount=value, currency=cur,
                    category_id=int(category) if category.isdigit() else None,
                    note=note.strip(), source="manual")
    if tx is None:
        return _after(request, db, user, back, error="такая операция уже записана")
    if tx.amount_base is None:
        return _after(request, db, user, back,
                      error="записано, но курс ЦБ недоступен — пересчитайте позже")
    # Месяц переключается на месяц операции: иначе запись «за прошлый раз» уходит
    # в невидимый период и выглядит как несохранённая
    return _after(request, db, user, back, show=tx.id, saved="1", m=d.strftime("%Y-%m"))


@router.post("/tx/{tid}/edit")
def tx_edit(request: Request, tid: int, account: str = Form(""), day: str = Form(""),
            amount: str = Form(""), currency: str = Form(""), kind: str = Form("expense"),
            category: str = Form(""), note: str = Form(""), back: str = Form("/fin/tx"),
            user: User = Depends(require_user), db: Session = Depends(get_session)):
    tx = db.get(FinTx, tid)
    if tx is None:
        return _after(request, db, user, back, error="операция не найдена")
    parsed = _tx_form(db, account, day, amount, currency, kind)
    if isinstance(parsed, str):
        return _after(request, db, user, back, error=parsed, edit=tid)
    acc, d, value, cur, k = parsed
    tx.account_id, tx.day, tx.kind, tx.amount, tx.currency = acc.id, d, k, value, cur
    tx.category_id = int(category) if category.isdigit() else None
    tx.note = note.strip()[:300]
    # Пересчёт обязателен: изменили сумму, валюту или дату — прежний amount_base стал
    # неверным, а именно он попадает во все отчёты.
    tx.amount_base, tx.rate, _ = fin.convert_for(db, value, cur, d)
    tx.base_code = base_currency(db)
    db.commit()
    return _after(request, db, user, back, show=tx.id, saved="1")


@router.post("/tx/{tid}/delete")
def tx_delete(request: Request, tid: int, back: str = Form("/fin/tx"),
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    tx = db.get(FinTx, tid)
    if tx is not None:
        db.delete(tx)
        db.commit()
    return _after(request, db, user, back, saved="1")


@router.post("/tx/delete")
def tx_delete_many(request: Request, ids: list[int] = Form(default=[]),
                   back: str = Form("/fin/tx"), user: User = Depends(require_user),
                   db: Session = Depends(get_session)):
    """Удалить отмеченные операции разом.

    Разбирая залитую выписку, лишнее видишь пачкой: десяток строк одного и того же
    вида. Удаление по одной превращает это в десяток подтверждений, и человек
    бросает на середине.

    Удаляются ровно отмеченные — не «всё по фильтру»: пропажа того, чего не видел
    на экране, страшнее любого удобства. Целую загрузку отменяют в её разделе.
    """
    if not ids:
        return _after(request, db, user, back, error="не отмечено ни одной операции")
    n = 0
    for tx in db.scalars(select(FinTx).where(FinTx.id.in_(ids))):
        db.delete(tx)
        n += 1
    db.commit()
    return _after(request, db, user, back, saved="1",
                  hint=f"удалено {n} {plural(n, 'операция', 'операции', 'операций')}")


@router.post("/tx/{tid}/exclude")
def tx_exclude(request: Request, tid: int, back: str = Form("/fin/tx"),
               user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Убрать операцию из итогов или вернуть обратно.

    Ровно для переводов между своими счетами: в выписке они выглядят как расход, но
    расходом не являются. Запись остаётся в базе — чтобы при повторной загрузке той же
    выписки не появилась заново.
    """
    tx = db.get(FinTx, tid)
    if tx is not None:
        tx.excluded = not tx.excluded
        db.commit()
    return _after(request, db, user, back, saved="1")


@router.post("/tx/{tid}/categorize")
def tx_categorize(request: Request, tid: int, category: str = Form(""),
                  pattern: str = Form(""), back: str = Form("/fin/tx?uncat=1"),
                  user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Назначить категорию и заодно, если задан образец, создать из этого правило.

    Смысл в одном действии: разбирая очередь, человек всё равно видит закономерность
    («опять Mercadona»), и правило надо создавать в этот момент, а не отдельным
    заходом на другую страницу. Созданное правило сразу применяется ко всем прочим
    неразобранным операциям.
    """
    tx = db.get(FinTx, tid)
    if tx is None:
        return _after(request, db, user, back, error="операция не найдена")
    cid = int(category) if category.isdigit() else None
    tx.category_id = cid
    applied = 0
    pat = pattern.strip()
    if pat and cid:
        exists = db.scalar(select(FinRule.id).where(
            func.lower(FinRule.pattern) == pat.lower(), FinRule.category_id == cid))
        if not exists:
            db.add(FinRule(pattern=pat[:200], category_id=cid, kind=tx.kind))
        db.commit()
        applied = _apply_rules_to_uncategorized(db)
    db.commit()
    return _after(request, db, user, back, saved="1", applied=applied or "")


def _apply_rules_to_uncategorized(db: Session) -> int:
    """Прогоняет правила по операциям без категории. Уже разобранные не трогает:
    правило не должно переписывать то, что человек поставил руками."""
    rules = list(db.scalars(select(FinRule)))
    if not rules:
        return 0
    n = 0
    for tx in db.scalars(select(FinTx).where(FinTx.category_id.is_(None),
                                             FinTx.excluded.is_(False))):
        cid, skip = fin.apply_rules(db, tx.note, tx.kind, rules)
        if skip:
            tx.excluded = True
            n += 1
        elif cid:
            tx.category_id = cid
            n += 1
    db.commit()
    return n


@router.post("/rules/apply")
def rules_apply(back: str = Form("/fin/categories"), user: User = Depends(require_user),
                db: Session = Depends(get_session)):
    return _redirect(back, applied=_apply_rules_to_uncategorized(db), saved="1")


# --------------------------------------------------------------------------------------
# Загрузка выписки
# --------------------------------------------------------------------------------------

def _upload_key(token: str) -> str:
    return f"fin_upload_{token}"


def _cleanup_uploads(db: Session) -> None:
    """Брошенные загрузки удаляются: файл выписки — не тот документ, который стоит
    держать на диске дольше необходимого."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for row in db.scalars(select(KV).where(KV.key.like("fin_upload_%"))):
        if now - float((row.value or {}).get("at", 0)) < UPLOAD_TTL:
            continue
        path = UPLOAD_DIR / str((row.value or {}).get("stored", ""))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        db.delete(row)
    db.commit()


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, error: str = "", saved: str = "",
                user: User = Depends(require_user), db: Session = Depends(get_session)):
    batches = list(db.scalars(select(FinImportBatch)
                              .order_by(FinImportBatch.created_at.desc()).limit(20)))
    return templates.TemplateResponse(request, "fin/import.html", _ctx(
        db, user=user, accounts=fin.accounts(db), batches=batches,
        error=error, saved=saved))


@router.post("/import/upload")
async def import_upload(account: str = Form(""), file: UploadFile = File(...),
                        user: User = Depends(require_user),
                        db: Session = Depends(get_session)):
    acc = db.get(FinAccount, int(account)) if account.isdigit() else None
    if acc is None:
        return _redirect("/fin/import", error="сначала выберите счёт")
    name = (file.filename or "").strip()
    if not name.lower().endswith(imp.SUPPORTED):
        return _redirect("/fin/import",
                         error="нужен файл .xlsx или .csv — .pdf и .xls не читаются")
    data = await file.read()
    if not data:
        return _redirect("/fin/import", error="файл пустой")
    try:
        rows = imp.read_table(name, data)
    except ValueError as e:
        return _redirect("/fin/import", error=str(e))
    if not rows:
        return _redirect("/fin/import", error="в файле нет строк")

    _cleanup_uploads(db)
    token = uuid.uuid4().hex
    ext = "." + name.rsplit(".", 1)[-1].lower()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"{token}{ext}").write_bytes(data)
    db.add(KV(key=_upload_key(token), value={
        "at": time.time(), "stored": f"{token}{ext}", "filename": name,
        "account_id": acc.id}))
    db.commit()
    return RedirectResponse(f"/fin/import/{token}", status_code=303)


def _load_upload(db: Session, token: str) -> tuple[list[list], dict] | None:
    row = db.get(KV, _upload_key(token))
    if row is None:
        return None
    meta = row.value or {}
    path = UPLOAD_DIR / str(meta.get("stored", ""))
    if not path.exists():
        return None
    try:
        return imp.read_table(str(meta.get("filename") or path.name),
                              path.read_bytes()), meta
    except ValueError:
        return None


@router.get("/import/{token}", response_class=HTMLResponse)
def import_map(request: Request, token: str, error: str = "",
               user: User = Depends(require_user), db: Session = Depends(get_session)):
    loaded = _load_upload(db, token)
    if loaded is None:
        return _redirect("/fin/import", error="файл больше не найден, загрузите заново")
    rows, meta = loaded
    acc = db.get(FinAccount, int(meta.get("account_id") or 0))
    # Сопоставление прошлой удачной загрузки этого счёта важнее угадывания: у банка
    # формат не меняется, а человек уже один раз указал колонки правильно.
    m = imp.Mapping.from_dict(acc.import_map) if acc and acc.import_map else imp.guess(rows)
    if not m.ok:
        m = imp.guess(rows)
    since = _import_from(db)
    preview = imp.parse_rows(rows, m, acc.currency if acc else "EUR",
                             limit=imp.PREVIEW_ROWS, not_before=since)
    total = len(imp.data_rows(rows, m))
    return templates.TemplateResponse(request, "fin/import_map.html", _ctx(
        db, user=user, token=token, meta=meta, acc=acc, m=m,
        labels=imp.header_labels(rows, m), preview=preview, total=total,
        head=rows[:imp.PREVIEW_ROWS + 3], error=error,
        bad=sum(1 for r in preview if r.error),
        skipped=sum(1 for r in preview if r.skip)))


@router.post("/import/{token}/remap")
def import_remap(token: str, header_row: str = Form("0"), day: str = Form(""),
                 amount: str = Form(""), expense: str = Form(""), income: str = Form(""),
                 note: str = Form(""), currency: str = Form(""), fee: str = Form(""),
                 state: str = Form(""),
                 expense_negative: str = Form(""), user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    """Сохраняет исправленное сопоставление на счёте и возвращает к предпросмотру.

    Сохраняем ДО применения: если человек поправил колонки, а потом закрыл страницу,
    в следующий раз он не будет делать это заново.
    """
    loaded = _load_upload(db, token)
    if loaded is None:
        return _redirect("/fin/import", error="файл больше не найден, загрузите заново")
    _, meta = loaded
    acc = db.get(FinAccount, int(meta.get("account_id") or 0))
    if acc is not None:
        acc.import_map = imp.Mapping.from_dict({
            "header_row": header_row, "day": day, "amount": amount, "expense": expense,
            "income": income, "note": note, "currency": currency,
            "fee": fee, "state": state,
            "expense_negative": expense_negative == "1"}).to_dict()
        db.commit()
    return RedirectResponse(f"/fin/import/{token}", status_code=303)


@router.post("/import/{token}/apply")
def import_apply(token: str, user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    loaded = _load_upload(db, token)
    if loaded is None:
        return _redirect("/fin/import", error="файл больше не найден, загрузите заново")
    rows, meta = loaded
    acc = db.get(FinAccount, int(meta.get("account_id") or 0))
    if acc is None:
        return _redirect("/fin/import", error="счёт удалён, загрузите заново")
    m = imp.Mapping.from_dict(acc.import_map) if acc.import_map else imp.guess(rows)
    if not m.ok:
        return RedirectResponse(f"/fin/import/{token}?error="
                                "укажите хотя бы дату и сумму", status_code=303)

    parsed = imp.parse_rows(rows, m, acc.currency, not_before=_import_from(db))

    # Курсы за весь период выписки — заранее и одним запросом на валюту. Иначе каждая
    # новая дата тянет отдельный поход в ЦБ, и выписка за несколько лет загружается
    # десятками минут, всё это время держа базу занятой.
    days = [r.day for r in parsed if r.day is not None]
    if days:
        try:
            fx.prefetch(db, {r.currency for r in parsed if r.currency} |
                        {acc.currency, base_currency(db)}, min(days), max(days))
        except Exception as e:  # noqa: BLE001 — ускорение не должно ронять загрузку
            log.warning("[fin] курсы за период не загружены: %s", e)

    batch = FinImportBatch(account_id=acc.id, filename=str(meta.get("filename") or ""),
                           total=len(parsed), mapping=m.to_dict())
    db.add(batch)
    db.flush()

    rules = list(db.scalars(select(FinRule)))
    seen: dict[str, int] = {}
    problems: list[str] = []
    for r in parsed:
        if r.skip:
            # Отменённая банком операция и строка с нулевой суммой — не ошибка разбора,
            # а нормальный ход дела: их незачем показывать как «не прочиталось».
            batch.ignored += 1
            continue
        if r.error:
            batch.failed += 1
            if len(problems) < 3:
                problems.append(f"строка {r.index + 1}: {r.error}")
            continue
        cid, skip = fin.apply_rules(db, r.note, r.kind, rules)
        if skip:
            batch.ignored += 1
            continue
        tx = fin.add_tx(db, account=acc, day=r.day, kind=r.kind, amount=r.amount,
                        currency=r.currency, category_id=cid, note=r.note,
                        source="import", batch_id=batch.id, seen=seen, commit=False)
        if tx is None:
            batch.duplicates += 1
        else:
            batch.added += 1
    batch.note = "; ".join(problems)
    db.commit()

    # Файл больше не нужен: разобранные строки лежат в базе, а выписка на диске —
    # лишняя копия персональных данных.
    try:
        (UPLOAD_DIR / str(meta.get("stored", ""))).unlink(missing_ok=True)
    except OSError:
        pass
    row = db.get(KV, _upload_key(token))
    if row is not None:
        db.delete(row)
        db.commit()
    return _redirect("/fin/import", saved=str(batch.id))


@router.post("/import/batch/{bid}/revert")
def import_revert(bid: int, user: User = Depends(require_user),
                  db: Session = Depends(get_session)):
    """Отменяет загрузку целиком — вместе со всеми добавленными ею операциями."""
    batch = db.get(FinImportBatch, bid)
    if batch is None:
        return _redirect("/fin/import", error="загрузка не найдена")
    n = 0
    for tx in db.scalars(select(FinTx).where(FinTx.batch_id == bid)):
        db.delete(tx)
        n += 1
    batch.reverted_at = utcnow()
    batch.note = f"отменено, удалено операций: {n}"
    db.commit()
    return _redirect("/fin/import", saved="revert")


# --------------------------------------------------------------------------------------
# Категории и правила
# --------------------------------------------------------------------------------------

@router.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request, saved: str = "", error: str = "", edit: str = "",
                    applied: str = "", user: User = Depends(require_user),
                    db: Session = Depends(get_session)):
    used = dict(db.execute(
        select(FinTx.category_id, func.count()).group_by(FinTx.category_id)).all())
    editing = db.get(FinCategory, int(edit)) if edit.isdigit() else None
    return templates.TemplateResponse(request, "fin/categories.html", _ctx(
        db, user=user, cats=fin.categories(db, with_archived=True), used=used,
        rules=list(db.scalars(select(FinRule).order_by(FinRule.pattern))),
        uncategorized=int(db.scalar(select(func.count()).select_from(FinTx).where(
            FinTx.category_id.is_(None), FinTx.excluded.is_(False))) or 0),
        editing=editing, saved=saved, error=error, applied=applied))


@router.post("/categories/add")
def categories_add(name: str = Form(""), kind: str = Form("expense"),
                   user: User = Depends(require_user), db: Session = Depends(get_session)):
    name = name.strip()
    if not name:
        return _redirect("/fin/categories", error="пустое название")
    kind = kind if kind in fin.KINDS else "expense"
    if db.scalar(select(FinCategory.id).where(func.lower(FinCategory.name) == name.lower(),
                                             FinCategory.kind == kind)):
        return _redirect("/fin/categories", error=f"категория «{name}» уже есть")
    db.add(FinCategory(name=name[:80], kind=kind))
    db.commit()
    return _redirect("/fin/categories", saved="1")


@router.post("/categories/{cid}/edit")
def categories_edit(cid: int, name: str = Form(""), user: User = Depends(require_user),
                    db: Session = Depends(get_session)):
    cat = db.get(FinCategory, cid)
    if cat is None:
        return _redirect("/fin/categories", error="категория не найдена")
    if not name.strip():
        return _redirect("/fin/categories", error="пустое название", edit=cid)
    cat.name = name.strip()[:80]
    db.commit()
    return _redirect("/fin/categories", saved="1")


@router.post("/categories/{cid}/archive")
def categories_archive(cid: int, user: User = Depends(require_user),
                       db: Session = Depends(get_session)):
    cat = db.get(FinCategory, cid)
    if cat is not None:
        cat.archived = not cat.archived
        db.commit()
    return _redirect("/fin/categories", saved="1")


@router.post("/categories/{cid}/delete")
def categories_delete(cid: int, user: User = Depends(require_user),
                      db: Session = Depends(get_session)):
    """Удаление разрешено и с операциями: они не пропадут, а станут «без категории».

    Так устроено в модели (SET NULL), и это правильнее запрета: категория —
    классификация, а не сама операция, и передумать в классификации нормально.
    """
    cat = db.get(FinCategory, cid)
    if cat is None:
        return _redirect("/fin/categories", error="категория не найдена")
    n = int(db.scalar(select(func.count()).select_from(FinTx)
                      .where(FinTx.category_id == cid)) or 0)
    db.delete(cat)
    db.commit()
    return _redirect("/fin/categories", saved="1",
                     applied=f"-{n}" if n else "")


@router.post("/rules/add")
def rules_add(pattern: str = Form(""), category: str = Form(""), skip: str = Form(""),
              user: User = Depends(require_user), db: Session = Depends(get_session)):
    pat = pattern.strip()
    if len(pat) < 2:
        return _redirect("/fin/categories", error="образец слишком короткий: "
                                                 "он подойдёт почти к любой строке")
    is_skip = skip == "1"
    cid = int(category) if category.isdigit() else None
    if not is_skip and cid is None:
        return _redirect("/fin/categories", error="выберите категорию или отметьте "
                                                 "«не учитывать»")
    db.add(FinRule(pattern=pat[:200], category_id=None if is_skip else cid, skip=is_skip,
                   kind=""))
    db.commit()
    return _redirect("/fin/categories", saved="1", applied=_apply_rules_to_uncategorized(db))


@router.post("/rules/{rid}/delete")
def rules_delete(rid: int, user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    r = db.get(FinRule, rid)
    if r is not None:
        db.delete(r)
        db.commit()
    return _redirect("/fin/categories", saved="1")


# --------------------------------------------------------------------------------------
# Долги
# --------------------------------------------------------------------------------------

def _debt_form(person: str, amount: str, day: str, due: str,
               ) -> tuple[str, float, date, date | None] | str:
    """Проверка полей формы долга. Возвращает разобранное или текст ошибки."""
    who = person.strip()[:80]
    if not who:
        return "не указано, кто должен"
    value = imp.parse_number(amount)
    if value is None or value == 0:
        return f"не похоже на сумму: {amount!r}"
    d = imp.parse_day(day) or date.today()
    when_due = imp.parse_day(due) if due.strip() else None
    return who, abs(round(value, 2)), d, when_due


@router.get("/debts", response_class=HTMLResponse)
def debts_page(request: Request, saved: str = "", error: str = "", edit: str = "",
               closed: str = "", user: User = Depends(require_user),
               db: Session = Depends(get_session)):
    """Кто и сколько должен на сегодня — и сколько должен я."""
    data = fin.debts(db, include_settled=True)
    editing = db.get(FinDebt, int(edit)) if edit.isdigit() else None
    return templates.TemplateResponse(request, "fin/debts.html", _ctx(
        db, user=user, d=data, editing=editing, show_closed=closed == "1",
        settled=[x for x in data.rows if x.settled_at is not None],
        today=date.today().isoformat(), today_date=date.today(),
        currencies=fx.CURRENCIES, side_names=fin.SIDE_NAMES, saved=saved, error=error))


@router.post("/debts/add")
def debts_add(side: str = Form("to_me"), person: str = Form(""), amount: str = Form(""),
              currency: str = Form(""), day: str = Form(""), due: str = Form(""),
              note: str = Form(""), user: User = Depends(require_user),
              db: Session = Depends(get_session)):
    parsed = _debt_form(person, amount, day, due)
    if isinstance(parsed, str):
        return _redirect("/fin/debts", error=parsed)
    who, value, d, when_due = parsed
    cur = (currency or base_currency(db)).upper()
    db.add(FinDebt(side=side if side in fin.SIDES else "to_me", person=who, amount=value,
                   currency=cur if len(cur) == 3 else "EUR", day=d, due=when_due,
                   note=note.strip()[:300]))
    db.commit()
    return _redirect("/fin/debts", saved="1")


@router.post("/debts/{did}/edit")
def debts_edit(did: int, side: str = Form("to_me"), person: str = Form(""),
               amount: str = Form(""), currency: str = Form(""), day: str = Form(""),
               due: str = Form(""), note: str = Form(""),
               user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Правка долга. Ею же оформляется частичный возврат: сумма — это остаток."""
    debt = db.get(FinDebt, did)
    if debt is None:
        return _redirect("/fin/debts", error="долг не найден")
    parsed = _debt_form(person, amount, day, due)
    if isinstance(parsed, str):
        return _redirect("/fin/debts", error=parsed, edit=did)
    who, value, d, when_due = parsed
    cur = (currency or debt.currency).upper()
    debt.side = side if side in fin.SIDES else debt.side
    debt.person, debt.amount, debt.day, debt.due = who, value, d, when_due
    debt.currency = cur if len(cur) == 3 else debt.currency
    debt.note = note.strip()[:300]
    db.commit()
    return _redirect("/fin/debts", saved="1")


@router.post("/debts/{did}/settle")
def debts_settle(did: int, user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    """Рассчитались — или наоборот, закрыли по ошибке.

    Запись остаётся: «мне вернули» — это то, что хочется видеть потом, а не то, что
    надо стирать. Закрытый долг в итоги не входит.
    """
    debt = db.get(FinDebt, did)
    if debt is None:
        return _redirect("/fin/debts", error="долг не найден")
    debt.settled_at = None if debt.settled_at else utcnow()
    db.commit()
    return _redirect("/fin/debts", saved="1", closed="1" if debt.settled_at else "")


@router.post("/debts/{did}/delete")
def debts_delete(did: int, user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    debt = db.get(FinDebt, did)
    if debt is not None:
        db.delete(debt)
        db.commit()
    return _redirect("/fin/debts", saved="1")


# --------------------------------------------------------------------------------------
# Сколько денег есть сейчас
# --------------------------------------------------------------------------------------

@router.get("/money", response_class=HTMLResponse)
def money_page(request: Request, day: str = "", saved: str = "", error: str = "",
               user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Остатки по счетам и наличными на дату.

    Отдельный экран, а не колонка в счетах: тут своя история и свой смысл —
    расходы отвечают, куда ушло, остатки отвечают, сколько осталось.
    """
    d = imp.parse_day(day) or date.today()
    rows = fin.snapshots(db, limit=36)
    last = fin.latest_balances(db)
    on_day = {b.account_id: b for b in db.scalars(
        select(FinBalance).where(FinBalance.day == d))}
    accs = fin.accounts(db)
    # Поле заполняется тем, что уже записано на эту дату, иначе — прошлым значением:
    # менять приходится один счёт из пяти, остальные достаточно подтвердить
    values = {a.id: (on_day[a.id].amount if a.id in on_day
                     else (last[a.id].amount if a.id in last else None)) for a in accs}
    prefilled = {a.id for a in accs if a.id not in on_day and a.id in last}
    return templates.TemplateResponse(request, "fin/money.html", _ctx(
        db, user=user, accounts=accs, day=d.isoformat(), today=date.today().isoformat(),
        values=values, prefilled=prefilled, rows=rows,
        now=rows[0] if rows else None, prev=rows[1] if len(rows) > 1 else None,
        series=[{"day": s.day.isoformat(), "total": s.total} for s in reversed(rows)],
        has_cash=any("налич" in a.name.lower() for a in accs),
        saved=saved, error=error))


@router.post("/money/save")
async def money_save(request: Request, day: str = Form(""),
                     user: User = Depends(require_user),
                     db: Session = Depends(get_session)):
    """Записать суммы со всей формы разом.

    Поля читаются из сырой формы, а не объявлены по одному: счетов сколько угодно,
    и каждый приходит полем «amount_<id>». Пустое поле стирает запись за эту дату —
    иначе от опечатки нельзя было бы избавиться.
    """
    d = imp.parse_day(day) or date.today()
    if d > date.today():
        return _redirect("/fin/money", error="дата в будущем", day=d.isoformat())
    form = await request.form()
    amounts: dict[int, float | None] = {}
    bad: list[str] = []
    for key, raw in form.items():
        if not key.startswith("amount_") or not key[7:].isdigit():
            continue
        aid = int(key[7:])
        text = str(raw).strip()
        if not text:
            amounts[aid] = None
            continue
        value = imp.parse_number(text)
        if value is None:
            bad.append(text)
            continue
        amounts[aid] = abs(value)
    if bad:
        return _redirect("/fin/money", day=d.isoformat(),
                         error=f"не похоже на сумму: {', '.join(bad[:3])}")
    n = fin.save_balances(db, d, amounts)
    return _redirect("/fin/money", saved="1" if n else "", day=d.isoformat())


@router.post("/money/delete")
def money_delete(day: str = Form(""), user: User = Depends(require_user),
                 db: Session = Depends(get_session)):
    """Убрать всю запись за дату — вписал не тот день, и он мешает в истории."""
    d = imp.parse_day(day)
    if d is None:
        return _redirect("/fin/money", error="не разобрана дата")
    for row in db.scalars(select(FinBalance).where(FinBalance.day == d)):
        db.delete(row)
    db.commit()
    return _redirect("/fin/money", saved="1")


# --------------------------------------------------------------------------------------
# Счета и настройки раздела
# --------------------------------------------------------------------------------------

@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, saved: str = "", error: str = "", edit: str = "",
                  user: User = Depends(require_user), db: Session = Depends(get_session)):
    counts = dict(db.execute(
        select(FinTx.account_id, func.count()).group_by(FinTx.account_id)).all())
    spent = dict(db.execute(
        select(FinTx.account_id, func.sum(FinTx.amount_base))
        .where(FinTx.kind == "expense", FinTx.excluded.is_(False))
        .group_by(FinTx.account_id)).all())
    no_rate = int(db.scalar(select(func.count()).select_from(FinTx)
                            .where(FinTx.amount_base.is_(None))) or 0)
    since = _import_from(db)
    return templates.TemplateResponse(request, "fin/accounts.html", _ctx(
        db, user=user, rows=fin.accounts(db, with_archived=True), counts=counts,
        spent=spent, currencies=fx.CURRENCIES, no_rate=no_rate,
        import_from=since.isoformat() if since else "", import_from_day=since,
        editing=db.get(FinAccount, int(edit)) if edit.isdigit() else None,
        saved=saved, error=error))


@router.post("/accounts/add")
def accounts_add(name: str = Form(""), currency: str = Form("EUR"), note: str = Form(""),
                 user: User = Depends(require_user), db: Session = Depends(get_session)):
    if not name.strip():
        return _redirect("/fin/accounts", error="пустое название")
    cur = (currency or "EUR").upper()
    db.add(FinAccount(name=name.strip()[:80], currency=cur if len(cur) == 3 else "EUR",
                      note=note.strip()[:200]))
    db.commit()
    return _redirect("/fin/accounts", saved="1")


@router.post("/accounts/{aid}/edit")
def accounts_edit(aid: int, name: str = Form(""), currency: str = Form("EUR"),
                  note: str = Form(""), user: User = Depends(require_user),
                  db: Session = Depends(get_session)):
    acc = db.get(FinAccount, aid)
    if acc is None:
        return _redirect("/fin/accounts", error="счёт не найден")
    if not name.strip():
        return _redirect("/fin/accounts", error="пустое название", edit=aid)
    acc.name = name.strip()[:80]
    cur = (currency or "EUR").upper()
    acc.currency = cur if len(cur) == 3 else acc.currency
    acc.note = note.strip()[:200]
    db.commit()
    return _redirect("/fin/accounts", saved="1")


@router.post("/accounts/{aid}/archive")
def accounts_archive(aid: int, user: User = Depends(require_user),
                     db: Session = Depends(get_session)):
    acc = db.get(FinAccount, aid)
    if acc is not None:
        acc.archived = not acc.archived
        db.commit()
    return _redirect("/fin/accounts", saved="1")


@router.post("/accounts/{aid}/delete")
def accounts_delete(aid: int, user: User = Depends(require_user),
                    db: Session = Depends(get_session)):
    """Счёт с операциями не удаляется — только в архив.

    Удаление унесло бы с собой всю историю по этой карте (в схеме стоит CASCADE), и
    восстановить её можно было бы только повторной загрузкой всех выписок. Архив
    убирает счёт из выбора, ничего не теряя.
    """
    acc = db.get(FinAccount, aid)
    if acc is None:
        return _redirect("/fin/accounts", error="счёт не найден")
    n = int(db.scalar(select(func.count()).select_from(FinTx)
                      .where(FinTx.account_id == aid)) or 0)
    if n:
        return _redirect("/fin/accounts",
                         error=f"на счёте {n} операций — удаление унесло бы их с собой. "
                               "Уберите счёт в архив.")
    db.delete(acc)
    db.commit()
    return _redirect("/fin/accounts", saved="1")


@router.post("/settings/currency")
def settings_currency(currency: str = Form("EUR"), user: User = Depends(require_user),
                      db: Session = Depends(get_session)):
    """Смена валюты отчётов пересчитывает все операции.

    Без пересчёта в одной таблице сложились бы суммы, пересчитанные в разные валюты, —
    итог получился бы бессмысленным числом, и заметить это было бы почти невозможно.
    """
    cur = (currency or "EUR").upper()
    if cur not in fx.CURRENCIES:
        return _redirect("/fin/accounts", error="неизвестная валюта")
    if cur == base_currency(db):
        return _redirect("/fin/accounts")
    save_prefs(db, fin_base_currency=cur)
    done, failed = fin.recompute_base(db)
    msg = f"пересчитано записей: {done}"
    if failed:
        msg += f", без курса осталось: {failed}"
    return _redirect("/fin/accounts", saved="1", error="" if not failed else msg)


@router.post("/settings/import-from")
def settings_import_from(day: str = Form(""), user: User = Depends(require_user),
                         db: Session = Depends(get_session)):
    """С какой даты брать операции из выписок.

    Банк отдаёт выписку за всю историю счёта. Без отсечки удалённые вручную старые
    годы возвращались бы при следующей же загрузке того же файла — и человек чистил
    бы их каждый месяц заново.
    """
    raw = (day or "").strip()
    if not raw:
        save_prefs(db, fin_import_from="")
        return _redirect("/fin/accounts", saved="1")
    d = imp.parse_day(raw)
    if d is None:
        return _redirect("/fin/accounts", error=f"не разобрана дата: {raw}")
    save_prefs(db, fin_import_from=d.isoformat())
    return _redirect("/fin/accounts", saved="1")


@router.post("/settings/recompute")
def settings_recompute(user: User = Depends(require_user),
                       db: Session = Depends(get_session)):
    done, failed = fin.recompute_base(db, only_missing=True)
    return _redirect("/fin/accounts", saved="1",
                     error=f"досчитано: {done}, без курса: {failed}" if failed else "")


# --------------------------------------------------------------------------------------
# Данные для графиков
# --------------------------------------------------------------------------------------

@router.get("/api/monthly")
def api_monthly(months: int = Query(24, ge=1, le=120), user: User = Depends(require_user),
                db: Session = Depends(get_session)):
    rows = fin.monthly_series(db, months=months)
    return JSONResponse({"base": base_currency(db), "months": [
        {"month": r.month, "income": round(r.income, 2), "expense": round(r.expense, 2),
         "net": round(r.net, 2)} for r in rows]})


@router.get("/api/category-trend")
def api_category_trend(category: int = Query(...), months: int = Query(12, ge=1, le=60),
                       user: User = Depends(require_user),
                       db: Session = Depends(get_session)):
    """Одна категория по месяцам — чтобы увидеть, растёт ли она, а не только итог."""
    ym = func.strftime("%Y-%m", FinTx.day)
    since = date.today() - timedelta(days=31 * months)
    rows = db.execute(
        select(ym, func.sum(FinTx.amount_base))
        .where(FinTx.category_id == category, FinTx.excluded.is_(False),
               FinTx.day >= since)
        .group_by(ym).order_by(ym)).all()
    cat = db.get(FinCategory, category)
    return JSONResponse({"base": base_currency(db),
                         "name": cat.name if cat else "",
                         "months": [{"month": m, "value": round(float(v or 0), 2)}
                                    for m, v in rows]})
