"""Изменение капитала за период — по снапшотам портфеля.

Отдельным модулем, потому что этот расчёт нужен в двух местах: плитка «Чистая
стоимость» на дашборде и ежедневная сводка в Telegram. Считать его дважды означало
бы однажды получить на экране одну цифру, а в сообщении другую.

Источник — тот же ряд снапшотов, что рисует график капитала, поэтому цифра не может
разойтись с картинкой.
"""

from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Snapshot, utcnow


def net_change(db: Session, net_now: float, hours: int = 24,
               wallet_id: int | None = None, allow_shorter: bool = False
               ) -> tuple[float | None, float | None, float | None]:
    """Насколько изменилась чистая стоимость за последние hours.

    Возвращает (в долларах, в процентах, фактическая длина периода в часах) или
    (None, None, None), если сравнивать не с чем. Ноль был бы неправдой: «не
    изменилось» и «не знаем» — разные вещи.

    Берём ближайший снапшот НЕ новее заданного момента: ряд прерывист, если
    приложение стояло, и точной отметки «ровно сутки назад» может не быть.

    allow_shorter=True — если истории меньше запрошенного периода, сравниваем с самой
    ранней точкой и сообщаем ФАКТИЧЕСКУЮ длину. Это для графика: там подпись строится
    по возвращённой длине, поэтому «за 30д» на трёхдневной истории не появится.
    Плитка и сводка зовут со строгим False: там период назван заранее, и растянуть его
    на что попало значило бы соврать в подписи.
    """
    now = utcnow()
    base = select(Snapshot)
    # снапшот портфеля целиком лежит с wallet_id = NULL, по кошельку — со своим id
    base = base.where(Snapshot.wallet_id == wallet_id) if wallet_id \
        else base.where(Snapshot.wallet_id.is_(None))

    prev = db.scalar(base.where(Snapshot.ts <= now - timedelta(hours=hours))
                     .order_by(Snapshot.ts.desc()).limit(1))
    if prev is None and allow_shorter:
        prev = db.scalar(base.order_by(Snapshot.ts.asc()).limit(1))
    if prev is None or not prev.net_usd:
        return None, None, None

    ts = prev.ts if prev.ts.tzinfo else prev.ts.replace(tzinfo=timezone.utc)
    span_h = (now - ts).total_seconds() / 3600.0
    delta = net_now - prev.net_usd
    return delta, delta / prev.net_usd * 100, span_h
