"""Сколько времени позиция реально стояла в диапазоне.

Зачем это отдельная метрика. Годовые по комиссиям сравнивают пулы честно только
при равной «попадаемости»: узкий диапазон с 30% времени внутри и широкий с 90%
дают похожие годовые в те часы, когда работают, но приносят совершенно разные
деньги за месяц. Ширину диапазона выбираете вы, и это единственная цифра, по
которой видно, удачно ли выбрали.

Считается по снапшотам позиций: раз в 15 минут пишется признак in_range, и доля
снапшотов «внутри» — это доля времени. Точность равна шагу снапшота, большего и
не нужно.

ЧЕСТНАЯ ОГОВОРКА, которую обязана показывать и страница: снапшоты пишутся только
когда приложение работает. Мак спал или был выключен — этих часов в данных нет
вовсе. Поэтому метрика измеряет долю НАБЛЮДЁННОГО времени, а не календарного, и
рядом всегда возвращается покрытие: сколько наблюдений набралось против того,
сколько их было бы при непрерывной работе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import config
from app.db.models import Position, PositionSnapshot


@dataclass
class Coverage:
    """Доля времени в диапазоне и надёжность самой оценки."""
    position_id: int
    observations: int = 0          # снапшотов с известным in_range
    inside: int = 0                # из них внутри диапазона
    pct: float | None = None       # доля времени внутри, %
    expected: int = 0              # сколько снапшотов было бы при непрерывной работе
    first: datetime | None = None
    last: datetime | None = None

    @property
    def coverage_pct(self) -> float | None:
        """Насколько наблюдения покрывают период. Меньше ~80% — оценка приблизительна."""
        if not self.expected:
            return None
        return min(self.observations / self.expected * 100, 100.0)

    @property
    def reliable(self) -> bool:
        # 12 наблюдений — это три часа при шаге 15 минут: меньше нельзя даже показывать
        cov = self.coverage_pct
        return self.observations >= 12 and (cov is None or cov >= 60)


def _expected(first: datetime | None, last: datetime | None) -> int:
    if not first or not last:
        return 0
    step = max(config.SNAPSHOT_INTERVAL, 60)
    return max(int((last - first).total_seconds() // step) + 1, 1)


def for_positions(db: Session, position_ids: list[int] | None = None,
                  days: int | None = None) -> dict[int, Coverage]:
    """Время в диапазоне по каждой позиции. days=None — за всю доступную историю."""
    # case, а не iif: iif есть только в SQLite, и запрос перестал бы быть переносимым
    inside_expr = func.sum(case((PositionSnapshot.in_range.is_(True), 1), else_=0))
    q = select(PositionSnapshot.position_id,
               func.count().label("n"), inside_expr.label("inside"),
               func.min(PositionSnapshot.ts), func.max(PositionSnapshot.ts))
    q = q.where(PositionSnapshot.in_range.isnot(None))
    if position_ids:
        q = q.where(PositionSnapshot.position_id.in_(position_ids))
    if days:
        q = q.where(PositionSnapshot.ts >= datetime.now(timezone.utc) - timedelta(days=days))
    q = q.group_by(PositionSnapshot.position_id)

    out: dict[int, Coverage] = {}
    for pid, n, inside, first, last in db.execute(q).all():
        first = first.replace(tzinfo=timezone.utc) if first and not first.tzinfo else first
        last = last.replace(tzinfo=timezone.utc) if last and not last.tzinfo else last
        c = Coverage(position_id=pid, observations=int(n or 0), inside=int(inside or 0),
                     expected=_expected(first, last), first=first, last=last)
        c.pct = (c.inside / c.observations * 100) if c.observations else None
        out[pid] = c
    return out


def for_position(db: Session, position_id: int, days: int | None = None) -> Coverage:
    return for_positions(db, [position_id], days).get(position_id) or Coverage(position_id)


def portfolio(db: Session, days: int | None = 1) -> Coverage:
    """Портфельная цифра: доля наблюдений «внутри» по всем активным позициям.

    Вес у каждой позиции одинаковый, а не по размеру: метрика отвечает на вопрос
    «удачно ли выбраны диапазоны», а не «сколько денег работало». Взвешивание по
    деньгам смешало бы два разных вопроса в одно число.
    """
    ids = [p.id for p in db.scalars(select(Position).where(Position.is_open.is_(True))).all()]
    per = for_positions(db, ids, days) if ids else {}
    total = Coverage(position_id=0)
    for c in per.values():
        total.observations += c.observations
        total.inside += c.inside
        total.expected += c.expected
        total.first = c.first if total.first is None or (c.first and c.first < total.first) else total.first
        total.last = c.last if total.last is None or (c.last and c.last > total.last) else total.last
    total.pct = (total.inside / total.observations * 100) if total.observations else None
    return total
