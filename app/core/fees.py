"""Собранные комиссии во времени: по месяцам и по пулам.

Источник — события `collect` в базе. Это единственное место, где комиссия
зафиксирована как факт: сколько токенов забрали и по какой цене они стоили
**в тот момент**. Пересчитывать старые сборы по сегодняшней цене нельзя — в мае
собранный эфир стоил не столько, сколько в декабре, и такой «доход» ходил бы
вверх-вниз вместе с рынком, хотя деньги давно на руках.

Отсюда же и главное ограничение: у части событий цены на момент сбора в базе нет
(DefiLlama не отдал историческую котировку). Такие события в суммы не попадают и
считаются отдельно — страница показывает их числом, чтобы итог не выглядел точнее,
чем он есть.

Агрегация делается в Python, а не в SQL: событий сотни, экономить тут нечего, зато
нет ни диалектных функций дат SQLite, ни возни с часовыми поясами внутри запроса.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Position, PositionEvent

MONTHS_RU = ("янв", "фев", "мар", "апр", "май", "июн",
             "июл", "авг", "сен", "окт", "ноя", "дек")


@dataclass
class Bucket:
    """Месяц на графике."""
    key: str            # 2026-06 — им же сортируем
    label: str          # июн 2026
    usd: float = 0.0
    events: int = 0
    cumulative: float = 0.0


@dataclass
class PoolRow:
    position_id: int
    title: str
    subtitle: str
    chain: str
    protocol: str
    # В одном пуле бывает несколько позиций подряд — с одинаковым названием и сетью.
    # Без tokenId строки в таблице выглядели бы дублями одной и той же записи.
    external_id: str = ""
    usd: float = 0.0
    events: int = 0
    first: datetime | None = None
    last: datetime | None = None


@dataclass
class Report:
    buckets: list[Bucket]
    pools: list[PoolRow]
    total: float = 0.0
    events: int = 0
    best: Bucket | None = None
    per_month: float = 0.0
    skipped: int = 0            # с комиссиями, но без цены на момент сбора
    skipped_usd_unknown: bool = True
    first: datetime | None = None
    last: datetime | None = None
    span_days: float = 0.0      # длина периода, по которой считаются годовые


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{MONTHS_RU[int(month) - 1]} {year}"


def _month_range(first: str, last: str) -> list[str]:
    """Все месяцы от first до last включительно.

    Пустые месяцы нужны на графике обязательно: без них два сбора с разницей в
    полгода встанут рядом, и картинка соврёт про темп — покажет ровный доход там,
    где его полгода не было.
    """
    y1, m1 = (int(x) for x in first.split("-"))
    y2, m2 = (int(x) for x in last.split("-"))
    out: list[str] = []
    while (y1, m1) <= (y2, m2):
        out.append(f"{y1:04d}-{m1:02d}")
        m1 += 1
        if m1 == 13:
            y1, m1 = y1 + 1, 1
    return out


def collected(db: Session, since: datetime | None = None, until: datetime | None = None,
              wallet_id: int | None = None) -> Report:
    """Сводка по собранным комиссиям за период.

    Границы включительные по дате: until — конец дня, иначе выбранное «по 30 июня»
    отрезало бы всё, что собрано 30-го числа после полуночи.
    """
    q = (select(PositionEvent, Position)
         .join(Position, PositionEvent.position_id == Position.id)
         .where(PositionEvent.kind == "collect"))
    if wallet_id:
        q = q.where(Position.wallet_id == wallet_id)
    if since is not None:
        q = q.where(PositionEvent.timestamp >= int(since.timestamp()))
    if until is not None:
        q = q.where(PositionEvent.timestamp <= int(until.timestamp()))

    rep = Report(buckets=[], pools=[])
    by_month: dict[str, Bucket] = {}
    by_pool: dict[int, PoolRow] = {}

    for event, pos in db.execute(q).all():
        fee = event.fee_usd_at_time
        has_fee_tokens = event.fee0 not in ("0", "", None) or event.fee1 not in ("0", "", None)
        if fee is None or not event.timestamp:
            # Сбор без комиссий вообще (забирали только тело) не потеря, а норма —
            # в пропущенные попадает только то, где комиссия была, а цены нет.
            if has_fee_tokens:
                rep.skipped += 1
            continue

        when = datetime.fromtimestamp(event.timestamp, timezone.utc)
        key = _month_key(when)
        bucket = by_month.setdefault(key, Bucket(key=key, label=_month_label(key)))
        bucket.usd += fee
        bucket.events += 1

        pool = by_pool.setdefault(pos.id, PoolRow(
            position_id=pos.id, title=pos.title or f"#{pos.external_id}",
            subtitle=pos.subtitle, chain=pos.chain, protocol=pos.protocol,
            external_id=pos.external_id))
        pool.usd += fee
        pool.events += 1
        pool.first = when if pool.first is None or when < pool.first else pool.first
        pool.last = when if pool.last is None or when > pool.last else pool.last

        rep.total += fee
        rep.events += 1
        rep.first = when if rep.first is None or when < rep.first else rep.first
        rep.last = when if rep.last is None or when > rep.last else rep.last

    if by_month:
        keys = sorted(by_month)
        # диапазон тянем по фактическим данным, но если человек задал границы —
        # уважаем их: пустой хвост месяца тоже факт, его видно на графике
        lo = _month_key(since) if since else keys[0]
        hi = _month_key(until) if until else keys[-1]
        lo, hi = min(lo, keys[0]), max(hi, keys[-1])
        running = 0.0
        for key in _month_range(lo, hi):
            bucket = by_month.get(key) or Bucket(key=key, label=_month_label(key))
            running += bucket.usd
            bucket.cumulative = running
            rep.buckets.append(bucket)

        filled = [b for b in rep.buckets if b.events]
        rep.best = max(filled, key=lambda b: b.usd) if filled else None
        # среднее по всем месяцам периода, включая пустые: месяц без сборов —
        # это тоже месяц, и делить только на «удачные» значило бы завышать темп
        rep.per_month = rep.total / len(rep.buckets) if rep.buckets else 0.0

    rep.pools = sorted(by_pool.values(), key=lambda p: p.usd, reverse=True)

    # Длина периода для годовых. Начало — заданная граница, а если её нет, первый
    # сбор: до него позиций не было, и растягивать доходность на пустоту нельзя.
    # Конец — «сейчас», а не последний сбор: месяц без комиссий доходность снижает,
    # и прятать это, обрезая период по крайнему сбору, было бы приятной неправдой.
    if rep.first is not None:
        start = since if since and since > rep.first else rep.first
        now = datetime.now(timezone.utc)
        end = min(until, now) if until else now
        rep.span_days = max((end - start).total_seconds() / 86400.0, 0.0)
    return rep


# Годовые из двух недель данных — это не оценка, а случайное число: один удачный
# день, умноженный на 26. Ниже этого порога честнее не показывать ничего.
MIN_SPAN_DAYS = 14.0


def annualized(rep: Report, base: float | None) -> float | None:
    """Годовые на комиссиях от заданной базы — суммы исходного вложения.

    Простая, не сложная ставка: комиссии из этой цифры не реинвестируются сами
    собой, а базой служит одна введённая руками сумма.
    """
    if not base or base <= 0 or rep.total <= 0 or rep.span_days < MIN_SPAN_DAYS:
        return None
    return rep.total / base * (365.0 / rep.span_days) * 100.0


def share_of_base(rep: Report, base: float | None) -> float | None:
    """Сколько собранные комиссии составляют от вложенного, без приведения к году."""
    if not base or base <= 0:
        return None
    return rep.total / base * 100.0
