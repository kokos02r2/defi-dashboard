"""Стоимость плеча: сколько процентов уходит за долг и сколько приходит с залога.

Зачем считать отдельно от комиссий. Комиссии Uniswap — это приход, и он виден на
странице «Комиссии». Плечо во Fluid — это расход, который нигде не показывался: долг
растёт сам, каждый день, и заметить это можно было только по медленно уползающей
цифре долга. При этом залог одновременно что-то приносит. Один без другого не
отвечает на главный вопрос: конструкция в целом зарабатывает или проедает.

Здесь два разных расчёта, и путать их нельзя:

  current() — по НЫНЕШНИМ ставкам и балансам. Это прогноз «если так и останется»:
              ставки во Fluid плавают вместе с загрузкой пула. Работает сразу.
  history() — ФАКТ по замерам: ставка и баланс, записанные в снапшот, умножаются на
              реально прошедшее время. Наполняется по мере накопления снапшотов, зато
              отвечает на вопрос «сколько уже потрачено», а не «сколько будет».

Задним числом историю не восстановить: ставок за прошлое нет ни в базе, ни в дешёвом
виде в блокчейне, а рост долга от процентов неотличим от новых займов, пока события
Fluid не собираются. Поэтому запись начата с того дня, когда появились эти поля.

Долларовые ставки: доход = залог × supply, расход = долг × borrow. Ключевое, чего
не видно в самих ставках, — расход считается от СУММЫ ДОЛГА, а она больше вашего
капитала при LTV выше 50%. Отсюда «−8% на капитал» при ставке долга 6.75%.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Position, PositionSnapshot

DAYS_IN_YEAR = 365.0
MONTHS_IN_YEAR = 12.0


@dataclass
class Row:
    """Одна позиция, приносящая или стоящая процентов."""
    position_id: int
    title: str
    subtitle: str
    chain: str
    protocol: str
    collateral: float = 0.0
    debt: float = 0.0
    equity: float = 0.0
    supply_rate: float = 0.0        # % годовых на залог
    borrow_rate: float = 0.0        # % годовых на долг
    income_year: float = 0.0
    cost_year: float = 0.0

    @property
    def net_year(self) -> float:
        return self.income_year - self.cost_year

    @property
    def net_on_equity(self) -> float | None:
        """Годовые на собственный капитал — та же цифра, что на странице позиции."""
        return (self.net_year / self.equity * 100) if self.equity else None


@dataclass
class Carry:
    rows: list[Row] = field(default_factory=list)
    income_year: float = 0.0
    cost_year: float = 0.0
    equity: float = 0.0
    debt: float = 0.0

    @property
    def net_year(self) -> float:
        return self.income_year - self.cost_year

    @property
    def net_month(self) -> float:
        return self.net_year / MONTHS_IN_YEAR

    @property
    def net_day(self) -> float:
        return self.net_year / DAYS_IN_YEAR

    @property
    def cost_day(self) -> float:
        return self.cost_year / DAYS_IN_YEAR

    @property
    def income_day(self) -> float:
        return self.income_year / DAYS_IN_YEAR

    @property
    def cost_month(self) -> float:
        return self.cost_year / MONTHS_IN_YEAR

    @property
    def income_month(self) -> float:
        return self.income_year / MONTHS_IN_YEAR

    @property
    def net_on_equity(self) -> float | None:
        return (self.net_year / self.equity * 100) if self.equity else None


def current(db: Session, wallet_id: int | None = None) -> Carry:
    """Плата за плечо по всем открытым позициям с процентами.

    Uniswap здесь нет умышленно: там доход не начисляется ставкой, а капает
    комиссиями с оборота, и его место — на графике комиссий.
    """
    q = select(Position).where(Position.is_open.is_(True),
                              Position.protocol.in_(("fluid_vault", "fluid_lending")))
    if wallet_id:
        q = q.where(Position.wallet_id == wallet_id)

    out = Carry()
    for p in db.scalars(q).all():
        rates = (p.detail or {}).get("rates") or {}
        collateral = float(p.value_usd or 0)
        debt = float(p.debt_usd or 0)
        # у депозита ставки лежат не в rates, а в apr — это и есть доход на всю сумму
        supply = float(rates.get("supply") if rates.get("supply") is not None
                       else (p.apr or 0.0))
        borrow = float(rates.get("borrow") or 0.0)

        row = Row(position_id=p.id, title=p.title, subtitle=p.subtitle, chain=p.chain,
                  protocol=p.protocol, collateral=collateral, debt=debt,
                  equity=float(p.net_usd or 0), supply_rate=supply, borrow_rate=borrow,
                  income_year=collateral * supply / 100.0,
                  cost_year=debt * borrow / 100.0)
        # позиция без ставок и без долга ничего не говорит — не показываем её
        if not row.income_year and not row.cost_year:
            continue
        out.rows.append(row)
        out.income_year += row.income_year
        out.cost_year += row.cost_year
        out.equity += row.equity
        out.debt += row.debt

    # дороже сверху: первым делом смотрят, что съедает больше всего
    out.rows.sort(key=lambda r: r.net_year)
    return out


@dataclass
class Day:
    """Сутки: сколько плечо стоило по факту."""
    key: str                       # 2026-07-30
    income: float = 0.0
    cost: float = 0.0
    hours: float = 0.0             # сколько времени реально наблюдали

    @property
    def net(self) -> float:
        return self.income - self.cost


@dataclass
class History:
    days: list[Day] = field(default_factory=list)
    income: float = 0.0
    cost: float = 0.0
    hours: float = 0.0

    @property
    def net(self) -> float:
        return self.income - self.cost

    @property
    def per_day(self) -> float | None:
        """Средняя стоимость суток по наблюдённому времени, а не по числу дней:
        неполные сутки на краях иначе занижали бы среднее."""
        return (self.net / (self.hours / 24)) if self.hours >= 1 else None

    @property
    def full_days(self) -> int:
        return sum(1 for d in self.days if d.hours >= 20)


# Разрыв больше часа — это не «интервал между замерами», а простой приложения:
# мак спал, сеть падала, шёл перезапуск. Начислять проценты за это время нельзя:
# мы не знаем, что там было со ставкой и балансом.
MAX_GAP_HOURS = 1.0


def history(db: Session, days: int | None = 90, wallet_id: int | None = None) -> History:
    """Фактическая стоимость плеча по замерам.

    Считается интегрированием: между двумя соседними замерами берём записанные тогда
    ставку и баланс и умножаем на реально прошедшее время. Это не прогноз «если ставка
    останется» — это то, что уже начислилось, с точностью до шага замера.

    Почему не по росту долга, хотя так было бы совсем точно: долг растёт и от процентов,
    и от новых займов, а событий Fluid в базе нет — различить одно от другого нечем.
    Проинтегрированная ставка от этой двусмысленности свободна.
    """
    q = (select(PositionSnapshot, Position)
         .join(Position, PositionSnapshot.position_id == Position.id)
         .where(Position.protocol.in_(("fluid_vault", "fluid_lending")))
         .order_by(PositionSnapshot.position_id, PositionSnapshot.ts))
    if wallet_id:
        q = q.where(Position.wallet_id == wallet_id)
    if days:
        q = q.where(PositionSnapshot.ts >= datetime.now(timezone.utc) - timedelta(days=days))

    per_position: dict[int, list[PositionSnapshot]] = {}
    for snap, _pos in db.execute(q).all():
        per_position.setdefault(snap.position_id, []).append(snap)

    buckets: dict[str, Day] = {}
    rep = History()

    for snaps in per_position.values():
        for prev, cur in zip(snaps, snaps[1:]):
            t0 = prev.ts if prev.ts.tzinfo else prev.ts.replace(tzinfo=timezone.utc)
            t1 = cur.ts if cur.ts.tzinfo else cur.ts.replace(tzinfo=timezone.utc)
            gap_h = (t1 - t0).total_seconds() / 3600.0
            if gap_h <= 0 or gap_h > MAX_GAP_HOURS:
                continue
            # Замеры, снятые до появления этих полей, ставок не содержат. Считать их
            # за «наблюдали, стоило ноль» нельзя: получилось бы, что плечо было
            # бесплатным, хотя мы про этот интервал просто ничего не знаем.
            if prev.borrow_rate is None and prev.supply_rate is None:
                continue

            years = gap_h / 24.0 / DAYS_IN_YEAR
            # берём состояние на НАЧАЛО интервала: именно оно действовало всё это время
            cost = float(prev.debt_usd or 0) * float(prev.borrow_rate or 0) / 100.0 * years
            income = float(prev.value_usd or 0) * float(prev.supply_rate or 0) / 100.0 * years

            key = t1.strftime("%Y-%m-%d")
            day = buckets.setdefault(key, Day(key=key))
            day.income += income
            day.cost += cost
            rep.income += income
            rep.cost += cost
            day.hours += gap_h

    # часы наблюдения: делим на число позиций, чтобы получить календарное время
    n_pos = max(len(per_position), 1)
    for day in buckets.values():
        day.hours = min(day.hours / n_pos, 24.0)
    rep.days = [buckets[k] for k in sorted(buckets)]
    rep.hours = sum(d.hours for d in rep.days)
    return rep


@dataclass
class Month:
    """Месяц фактической стоимости плеча — и насколько он наблюдён."""
    key: str                       # 2026-07
    income: float = 0.0
    cost: float = 0.0
    hours: float = 0.0
    days_in_month: int = 0

    @property
    def net(self) -> float:
        return self.income - self.cost

    @property
    def coverage(self) -> float | None:
        """Доля месяца, попавшая в наблюдение. Неполный месяц иначе выглядел бы дешёвым."""
        if not self.days_in_month:
            return None
        return min(self.hours / (self.days_in_month * 24) * 100, 100.0)

    @property
    def full(self) -> bool:
        cov = self.coverage
        return cov is not None and cov >= 95


def monthly(h: History) -> dict[str, Month]:
    """Суточные данные, свёрнутые по месяцам."""
    out: dict[str, Month] = {}
    for day in h.days:
        year, month, _ = (int(x) for x in day.key.split("-"))
        key = f"{year:04d}-{month:02d}"
        row = out.get(key)
        if row is None:
            days_in = monthrange(year, month)[1]
            row = out[key] = Month(key=key, days_in_month=days_in)
        row.income += day.income
        row.cost += day.cost
        row.hours += day.hours
    return out


def history_series(h: History) -> dict:
    """Данные для графика фактической стоимости по дням."""
    running = 0.0
    cumulative = []
    for d in h.days:
        running += d.net
        cumulative.append(round(running, 2))
    return {
        "labels": [d.key for d in h.days],
        "cost": [-round(d.cost, 2) for d in h.days],
        "income": [round(d.income, 2) for d in h.days],
        "net": [round(d.net, 2) for d in h.days],
        "cumulative": cumulative,
        "hours": [round(d.hours, 1) for d in h.days],
    }


def chart_series(c: Carry) -> dict:
    """Данные для графика: приход и расход по каждой позиции, в месяц.

    Месяц — потому что в день суммы вырождаются в центы, а в год теряется связь с
    тем, что человек видит на счету.
    """
    return {
        "labels": [f"{r.title} · {r.chain}" for r in c.rows],
        "income": [round(r.income_year / MONTHS_IN_YEAR, 2) for r in c.rows],
        # расход отрицательным: столбики вниз читаются как трата без пояснений
        "cost": [-round(r.cost_year / MONTHS_IN_YEAR, 2) for r in c.rows],
        "net": [round(r.net_year / MONTHS_IN_YEAR, 2) for r in c.rows],
    }
