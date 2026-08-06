"""Личные финансы: запись операций и отчёты по ним.

Все итоги считаются по amount_base — сумме, пересчитанной в валюту отчётов на дату
операции. Операции с флагом excluded не участвуют нигде: это переводы между своими
счетами, которые в выписке выглядят как расход, но расходом не являются.

Строка без курса (amount_base пустой — не было связи с ЦБ в момент записи) не
считается нулём молча: её количество возвращается отдельным полем, чтобы в интерфейсе
было видно, что часть сумм не учтена, а не «в этом месяце потрачено меньше».
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import fx
from app.db.models import FinAccount, FinCategory, FinRule, FinTx
from app.db.prefs import base_currency, get_prefs, save_prefs

log = logging.getLogger(__name__)

KINDS = ("expense", "income")

# Стартовый набор категорий. Не «правильный», а обычный: список, который человек
# всё равно перепишет под себя, но с которым можно начать раскладывать выписку в
# первый же день, а не сочинять справочник с нуля.
SEED_EXPENSE = [
    "Продукты", "Кафе и рестораны", "Жильё", "Коммунальные платежи", "Транспорт",
    "Автомобиль", "Здоровье", "Одежда и обувь", "Развлечения", "Подписки и связь",
    "Образование", "Дети", "Путешествия", "Подарки", "Налоги и сборы",
    "Хозяйство и ремонт", "Прочее",
]
SEED_INCOME = [
    "Зарплата", "Фриланс", "Проценты и дивиденды", "Аренда", "Возвраты",
    "Подарки", "Прочее",
]

# Образцы переводов между своими счетами из выписок Revolut и Райффайзена. Без них
# первая же загрузка выписки завышает расходы в разы: перекладывание денег со счёта
# на счёт, обмен валюты и вывод на свою же карту выглядят в файле как обычные траты,
# а их там сотни. Правила заводятся с флагом «не учитывать», а не категорией: такие
# строки не расход и не доход, им нечего делать в отчётах.
SEED_SKIP_RULES = [
    "рублевый перевод между счетами",     # Райффайзен: между своими счетами
    "перевели между своими счетами",
    "пополнение своего счета",
    "пополнение брокерского счета",
    "пополнение счета",                   # Revolut: с собственной карты
    "обменено на",                        # Revolut: обмен валюты внутри счёта
    # Копилка Revolut. Образцы длиннее, чем просто «сбережения с мгновенным доступом»,
    # намеренно: тем же словом называются начисленные по ней проценты, а это
    # настоящий доход, и отсеивать его нельзя.
    "получатель: eur сбережения",
    "с карты eur сбережения",
    "transfer from revolut digital assets",
]


# --------------------------------------------------------------------------------------
# Справочники
# --------------------------------------------------------------------------------------

def seed_categories(db: Session) -> int:
    """Заводит стартовые категории один раз за жизнь базы.

    Флаг в настройках, а не проверка «таблица пуста»: иначе удалённые категории
    возвращались бы при первом же заходе в раздел с пустым списком.
    """
    if get_prefs(db).get("fin_seeded"):
        return 0
    added = 0
    for kind, names in (("expense", SEED_EXPENSE), ("income", SEED_INCOME)):
        for i, name in enumerate(names):
            exists = db.scalar(select(FinCategory.id).where(
                FinCategory.name == name, FinCategory.kind == kind))
            if exists:
                continue
            db.add(FinCategory(name=name, kind=kind, sort=(i + 1) * 10))
            added += 1
    db.commit()
    save_prefs(db, fin_seeded=True)
    return added


def seed_rules(db: Session) -> int:
    """Заводит правила «не учитывать» для переводов между своими счетами.

    Отдельный флаг от категорий: базы, где категории уже посеяны, тоже должны получить
    правила — иначе первая выписка снова разъедется. Удалённое правило не вернётся:
    флаг взводится один раз, независимо от того, сколько строк реально добавилось.
    """
    if get_prefs(db).get("fin_rules_seeded"):
        return 0
    added = 0
    for pattern in SEED_SKIP_RULES:
        exists = db.scalar(select(FinRule.id).where(FinRule.pattern == pattern))
        if exists:
            continue
        db.add(FinRule(pattern=pattern, category_id=None, skip=True))
        added += 1
    db.commit()
    save_prefs(db, fin_rules_seeded=True)
    return added


def categories(db: Session, kind: str | None = None,
               with_archived: bool = False) -> list[FinCategory]:
    q = select(FinCategory)
    if kind:
        q = q.where(FinCategory.kind == kind)
    if not with_archived:
        q = q.where(FinCategory.archived.is_(False))
    return list(db.scalars(q.order_by(FinCategory.kind, FinCategory.sort, FinCategory.name)))


def accounts(db: Session, with_archived: bool = False) -> list[FinAccount]:
    q = select(FinAccount)
    if not with_archived:
        q = q.where(FinAccount.archived.is_(False))
    return list(db.scalars(q.order_by(FinAccount.archived, FinAccount.name)))


# --------------------------------------------------------------------------------------
# Запись операций
# --------------------------------------------------------------------------------------

_WORD = re.compile(r"[^\w]+", re.UNICODE)


def norm_note(s: str | None) -> str:
    """Описание к сравнимому виду: регистр и пунктуация в выписках гуляют."""
    return _WORD.sub(" ", (s or "").lower()).strip()


def _base_key(account_id: int, day: date, amount: float, currency: str, note: str) -> str:
    return f"{account_id}|{day.isoformat()}|{amount:.2f}|{currency}|{norm_note(note)}"


def fingerprint(db: Session, account_id: int, day: date, amount: float,
                currency: str, note: str, seen: dict[str, int] | None = None) -> str:
    """Отпечаток операции для защиты от повторной загрузки выписки.

    Тонкость, из-за которой нельзя взять просто хэш от полей: два одинаковых кофе в
    один день по одной цене — это две настоящие операции, и вторую нельзя терять как
    «дубль». Поэтому в ключ входит порядковый номер среди уже существующих таких же:
    первая получает #1, вторая #2. При повторной загрузке того же файла нумерация
    повторяется, и обе строки честно опознаются как уже загруженные.

    Два режима, и разница между ними принципиальная:

    * загрузка файла (`seen` передан) — номер считается только в пределах файла. Тогда
      повторная загрузка того же файла даёт те же отпечатки, и все строки опознаются
      как уже загруженные. Это и есть защита от дублей.
    * запись руками (`seen` не передан) — ищется первый свободный номер. Человек,
      добавляющий второй такой же кофе, хочет именно вторую запись, а не сообщение
      «такая операция уже есть».
    """
    key = _base_key(account_id, day, amount, currency, note)

    def digest(n: int) -> str:
        return hashlib.sha1(f"{key}#{n}".encode()).hexdigest()

    if seen is not None:
        n = seen.get(key, 0) + 1
        seen[key] = n
        return digest(n)

    n = 1
    while db.scalar(select(FinTx.id).where(FinTx.fingerprint == digest(n))):
        n += 1
    return digest(n)


def apply_rules(db: Session, note: str, kind: str,
                rules: list[FinRule] | None = None) -> tuple[int | None, bool]:
    """Первое подошедшее правило решает: (категория, отсеять).

    Порядок — по длине образца: более длинный образец описывает случай точнее, и
    правило «lidl gasolinera → Автомобиль» должно побеждать общее «lidl → Продукты».
    """
    if rules is None:
        rules = list(db.scalars(select(FinRule)))
    text = norm_note(note)
    if not text:
        return None, False
    for r in sorted(rules, key=lambda r: -len(r.pattern or "")):
        if r.kind and r.kind != kind:
            continue
        pat = norm_note(r.pattern)
        if pat and pat in text:
            r.hits = (r.hits or 0) + 1
            return r.category_id, bool(r.skip)
    return None, False


def convert_for(db: Session, amount: float, currency: str,
                day: date) -> tuple[float | None, float | None, str]:
    """Пересчёт в валюту отчётов. При недоступном курсе — (None, None, текст ошибки).

    Операция сохраняется и без курса: потерять запись из-за недоступного сайта ЦБ
    было бы хуже, чем показать её без пересчёта. Досчитать потом можно кнопкой.
    """
    base = base_currency(db)
    try:
        value, rate = fx.convert(db, amount, currency, base, day)
        return value, rate, ""
    except fx.FxUnavailable as e:
        log.warning("[fin] нет курса %s→%s на %s: %s", currency, base, day, e)
        return None, None, str(e)


def add_tx(db: Session, *, account: FinAccount, day: date, kind: str, amount: float,
           currency: str | None = None, category_id: int | None = None, note: str = "",
           source: str = "manual", batch_id: int | None = None,
           excluded: bool = False, seen: dict[str, int] | None = None,
           commit: bool = True) -> FinTx | None:
    """Добавляет операцию. Возвращает None, если такая уже есть (совпал отпечаток)."""
    currency = (currency or account.currency or "EUR").upper()
    fp = fingerprint(db, account.id, day, amount, currency, note, seen)
    if db.scalar(select(FinTx.id).where(FinTx.fingerprint == fp)):
        return None
    value, rate, _ = convert_for(db, amount, currency, day)
    tx = FinTx(account_id=account.id, day=day, kind=kind, amount=amount,
               currency=currency, amount_base=value, base_code=base_currency(db),
               rate=rate, category_id=category_id, note=note[:300],
               excluded=excluded, source=source, batch_id=batch_id, fingerprint=fp)
    db.add(tx)
    if commit:
        db.commit()
    return tx


def recompute_base(db: Session, only_missing: bool = False) -> tuple[int, int]:
    """Пересчитывает amount_base у операций. Возвращает (пересчитано, не удалось).

    Нужно в двух случаях: сменилась валюта отчётов (иначе в одной таблице сложились
    бы суммы, пересчитанные в разные валюты) и досчитать строки, записанные без связи
    с ЦБ.
    """
    base = base_currency(db)
    q = select(FinTx)
    if only_missing:
        q = q.where(FinTx.amount_base.is_(None))
    else:
        q = q.where((FinTx.base_code != base) | (FinTx.amount_base.is_(None)))
    rows = list(db.scalars(q))
    # Тот же приём, что при загрузке выписки: курсы за весь период сразу, одним
    # запросом на валюту. Пересчёт тысячи операций иначе означает тысячу походов в ЦБ.
    days = [t.day for t in rows if t.day is not None]
    if days:
        try:
            fx.prefetch(db, {t.currency for t in rows if t.currency} | {base},
                        min(days), max(days))
        except Exception as e:  # noqa: BLE001 — ускорение не должно ронять пересчёт
            log.warning("[fin] курсы за период не загружены: %s", e)

    done = failed = 0
    for tx in rows:
        value, rate, _ = convert_for(db, tx.amount, tx.currency, tx.day)
        if value is None:
            failed += 1
            continue
        tx.amount_base, tx.rate, tx.base_code = value, rate, base
        done += 1
    db.commit()
    return done, failed


# --------------------------------------------------------------------------------------
# Отчёты
# --------------------------------------------------------------------------------------

@dataclass
class Summary:
    income: float = 0.0
    expense: float = 0.0
    count: int = 0
    no_rate: int = 0
    uncategorized: int = 0
    excluded: int = 0

    @property
    def net(self) -> float:
        return self.income - self.expense

    @property
    def savings_rate(self) -> float | None:
        """Доля дохода, которая не потрачена. Без дохода величина не определена —
        показывать 0% или −100% было бы выдумкой."""
        return (self.net / self.income * 100) if self.income else None


@dataclass
class Bucket:
    """Строка разбивки: категория (или счёт) с суммой и сравнением с прошлым периодом."""
    key: int | None
    name: str
    amount: float = 0.0
    count: int = 0
    prev: float = 0.0

    @property
    def delta(self) -> float:
        return self.amount - self.prev

    @property
    def delta_pct(self) -> float | None:
        return (self.amount / self.prev - 1) * 100 if self.prev else None

    def share(self, total: float) -> float:
        return (self.amount / total * 100) if total else 0.0


def _base_q(start: date | None, end: date | None, account_ids: list[int] | None = None):
    q = select(FinTx).where(FinTx.excluded.is_(False))
    if start:
        q = q.where(FinTx.day >= start)
    if end:
        q = q.where(FinTx.day <= end)
    if account_ids:
        q = q.where(FinTx.account_id.in_(account_ids))
    return q


def summary(db: Session, start: date | None, end: date | None,
            account_ids: list[int] | None = None) -> Summary:
    s = Summary()
    rows = db.execute(
        select(FinTx.kind, func.sum(FinTx.amount_base), func.count(),
               func.sum(func.iif(FinTx.amount_base.is_(None), 1, 0)),
               func.sum(func.iif(FinTx.category_id.is_(None), 1, 0)))
        .where(FinTx.excluded.is_(False))
        .where(*( [FinTx.day >= start] if start else []))
        .where(*( [FinTx.day <= end] if end else []))
        .where(*( [FinTx.account_id.in_(account_ids)] if account_ids else []))
        .group_by(FinTx.kind)).all()
    for kind, total, cnt, no_rate, uncat in rows:
        if kind == "income":
            s.income = float(total or 0)
        else:
            s.expense = float(total or 0)
        s.count += int(cnt or 0)
        s.no_rate += int(no_rate or 0)
        s.uncategorized += int(uncat or 0)
    s.excluded = int(db.scalar(
        select(func.count()).select_from(FinTx).where(FinTx.excluded.is_(True))
        .where(*([FinTx.day >= start] if start else []))
        .where(*([FinTx.day <= end] if end else []))) or 0)
    return s


def by_category(db: Session, start: date, end: date, kind: str = "expense",
                account_ids: list[int] | None = None) -> list[Bucket]:
    """Разбивка по категориям с сравнением с предыдущим периодом такой же длины.

    Сравнение важнее самой суммы: «на продукты 600 €» само по себе ни о чём не
    говорит, а «600 € против 420 € в прошлом месяце» — уже повод посмотреть.
    """
    span = (end - start).days + 1
    prev_start, prev_end = start - timedelta(days=span), start - timedelta(days=1)

    def fetch(a: date, b: date) -> dict[int | None, tuple[float, int]]:
        rows = db.execute(
            select(FinTx.category_id, func.sum(FinTx.amount_base), func.count())
            .where(FinTx.excluded.is_(False), FinTx.kind == kind,
                   FinTx.day >= a, FinTx.day <= b)
            .where(*([FinTx.account_id.in_(account_ids)] if account_ids else []))
            .group_by(FinTx.category_id)).all()
        return {cid: (float(total or 0), int(cnt or 0)) for cid, total, cnt in rows}

    now, before = fetch(start, end), fetch(prev_start, prev_end)
    names = {c.id: c.name for c in categories(db, with_archived=True)}
    out = [Bucket(key=cid, name=names.get(cid) or "Без категории",
                  amount=amount, count=cnt, prev=before.get(cid, (0.0, 0))[0])
           for cid, (amount, cnt) in now.items()]
    out.sort(key=lambda b: -b.amount)
    return out


def by_account(db: Session, start: date, end: date, kind: str = "expense") -> list[Bucket]:
    rows = db.execute(
        select(FinTx.account_id, func.sum(FinTx.amount_base), func.count())
        .where(FinTx.excluded.is_(False), FinTx.kind == kind,
               FinTx.day >= start, FinTx.day <= end)
        .group_by(FinTx.account_id)).all()
    names = {a.id: a.name for a in accounts(db, with_archived=True)}
    out = [Bucket(key=aid, name=names.get(aid) or "—",
                  amount=float(total or 0), count=int(cnt or 0))
           for aid, total, cnt in rows]
    out.sort(key=lambda b: -b.amount)
    return out


@dataclass
class MonthRow:
    month: str                    # «2025-07»
    income: float = 0.0
    expense: float = 0.0

    @property
    def net(self) -> float:
        return self.income - self.expense


def monthly_series(db: Session, months: int = 24,
                   account_ids: list[int] | None = None) -> list[MonthRow]:
    """Доходы и расходы по месяцам, включая пустые.

    Пропущенные месяцы заполняются нулями намеренно: без них график сжимает паузу
    в учёте и показывает ровную линию там, где данных просто нет.
    """
    ym = func.strftime("%Y-%m", FinTx.day)
    rows = db.execute(
        select(ym, FinTx.kind, func.sum(FinTx.amount_base))
        .where(FinTx.excluded.is_(False))
        .where(*([FinTx.account_id.in_(account_ids)] if account_ids else []))
        .group_by(ym, FinTx.kind).order_by(ym)).all()
    if not rows:
        return []
    data: dict[str, MonthRow] = {}
    for m, kind, total in rows:
        row = data.setdefault(m, MonthRow(month=m))
        if kind == "income":
            row.income = float(total or 0)
        else:
            row.expense = float(total or 0)

    first = min(data)
    y, mo = (int(x) for x in first.split("-"))
    today = date.today()
    out: list[MonthRow] = []
    while (y, mo) <= (today.year, today.month):
        key = f"{y:04d}-{mo:02d}"
        out.append(data.get(key) or MonthRow(month=key))
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return out[-months:]


def top_expenses(db: Session, start: date, end: date, limit: int = 10,
                 account_ids: list[int] | None = None) -> list[FinTx]:
    return list(db.scalars(
        _base_q(start, end, account_ids).where(FinTx.kind == "expense")
        .order_by(FinTx.amount_base.desc().nullslast()).limit(limit)))


@dataclass
class Recurring:
    """Похоже на регулярный платёж: одно и то же описание из месяца в месяц."""
    name: str
    months: int
    total: float
    avg: float
    last_day: date | None = None
    examples: list[str] = field(default_factory=list)


def recurring(db: Session, months: int = 6, min_months: int = 3) -> list[Recurring]:
    """Повторяющиеся платежи — обычно это подписки, о части которых уже забыли.

    Критерий — не одинаковая сумма, а появление в разные месяцы: у связи и хостинга
    сумма гуляет, но платёж всё равно регулярный. Цифры из описания выбрасываются:
    в выписках туда попадают номера квитанций, и один и тот же платёж иначе выглядит
    каждый месяц по-новому.
    """
    since = date.today() - timedelta(days=31 * months)
    groups: dict[str, dict] = {}
    for tx in db.scalars(_base_q(since, None).where(FinTx.kind == "expense")):
        name = re.sub(r"\d+", " ", norm_note(tx.note))
        name = re.sub(r"\s+", " ", name).strip()[:40]
        if len(name) < 4:
            continue
        g = groups.setdefault(name, {"months": set(), "total": 0.0, "n": 0,
                                     "last": None, "examples": []})
        g["months"].add((tx.day.year, tx.day.month))
        g["total"] += tx.amount_base or 0.0
        g["n"] += 1
        if g["last"] is None or tx.day > g["last"]:
            g["last"] = tx.day
        if tx.note and len(g["examples"]) < 2 and tx.note not in g["examples"]:
            g["examples"].append(tx.note)

    out = [Recurring(name=name, months=len(g["months"]), total=g["total"],
                     avg=g["total"] / g["n"] if g["n"] else 0.0,
                     last_day=g["last"], examples=g["examples"])
           for name, g in groups.items() if len(g["months"]) >= min_months]
    out.sort(key=lambda r: -r.total)
    return out


def month_bounds(anchor: date | None = None, back: int = 0) -> tuple[date, date]:
    """Первое и последнее число месяца, отстоящего на `back` месяцев назад."""
    d = anchor or date.today()
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    start = date(y, m, 1)
    end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return start, end


def available_months(db: Session) -> list[str]:
    """Месяцы, за которые есть хоть одна операция — для выбора периода."""
    ym = func.strftime("%Y-%m", FinTx.day)
    return [m for (m,) in db.execute(
        select(ym).select_from(FinTx).group_by(ym).order_by(ym.desc())).all() if m]
