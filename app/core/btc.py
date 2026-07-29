"""Журнал покупок BTC: сколько всего набрано и по какой средней цене.

Раздел сознательно изолирован. Этот BTC куплен на уже заклеймленные комиссии, и
подмешивать его в итоги портфеля нельзя: комиссии в них уже учтены, а купленный на
них биткоин посчитался бы теми же деньгами второй раз. Поэтому здесь нет ничего,
что читал бы планировщик или расчёт итогов — только своя страница.

Средняя цена считается как деньги / количество, а не как среднее из цен покупок:
второе завышало бы вклад мелких сделок. Купив 0.01 BTC по $100k и 1 BTC по $60k,
вы набрали по $60.4k, а не «по $80k».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.market import market_rates
from app.db.models import BtcBuy


@dataclass
class Row:
    """Покупка вместе с состоянием набора на её момент."""
    buy: BtcBuy
    cumulative_btc: float = 0.0
    avg_after: float = 0.0      # средняя цена набора после этой покупки
    share: float = 0.0          # доля этой покупки в общем количестве, %


@dataclass
class Summary:
    rows: list[Row] = field(default_factory=list)   # в порядке покупок
    total_btc: float = 0.0
    total_cost: float = 0.0
    avg_price: float | None = None
    best_price: float | None = None      # самая удачная покупка
    worst_price: float | None = None
    price_now: float | None = None
    value_now: float | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    first: datetime | None = None
    last: datetime | None = None


def _when(buy: BtcBuy) -> datetime:
    """Дата покупки, а если её стёрли — дата записи: чем-то надо упорядочить."""
    return buy.bought_at or buy.created_at


def btc_price(db: Session) -> float | None:
    """Текущая цена BTC из того же кэша, что кормит тикер в шапке.

    Отдельного запроса не делаем: цена там уже есть и живёт две минуты.
    """
    for rate in market_rates(db):
        if rate.get("symbol") == "BTC":
            return rate.get("price")
    return None


def summarize(db: Session, price_now: float | None = None) -> Summary:
    buys = list(db.scalars(select(BtcBuy)).all())
    buys.sort(key=_when)

    s = Summary(price_now=price_now)
    running_btc = running_cost = 0.0
    for buy in buys:
        amount, price = buy.amount_btc or 0.0, buy.price_usd or 0.0
        running_btc += amount
        running_cost += amount * price
        s.rows.append(Row(buy=buy, cumulative_btc=running_btc,
                          avg_after=running_cost / running_btc if running_btc else 0.0))

    s.total_btc, s.total_cost = running_btc, running_cost
    if not buys:
        return s

    s.avg_price = running_cost / running_btc if running_btc else None
    prices = [b.price_usd for b in buys if b.price_usd]
    s.best_price, s.worst_price = (min(prices), max(prices)) if prices else (None, None)
    s.first, s.last = _when(buys[0]), _when(buys[-1])
    for row in s.rows:
        row.share = (row.buy.amount_btc or 0.0) / running_btc * 100 if running_btc else 0.0

    if price_now:
        s.value_now = running_btc * price_now
        s.pnl = s.value_now - running_cost
        s.pnl_pct = (s.pnl / running_cost * 100) if running_cost else None
    return s


def chart_series(s: Summary) -> dict:
    """Данные для графика: сколько набрано и по какой цене брали.

    Столбики — количество каждой покупки, линии — цена этой покупки и средняя
    набора после неё. Вместе это отвечает на главный вопрос такого журнала:
    покупки идут выше или ниже собственной средней.
    """
    return {
        "labels": [_when(r.buy).strftime("%d.%m.%y") for r in s.rows],
        "amount": [round(r.buy.amount_btc or 0.0, 8) for r in s.rows],
        "cumulative": [round(r.cumulative_btc, 8) for r in s.rows],
        "price": [round(r.buy.price_usd or 0.0, 2) for r in s.rows],
        "avg": [round(r.avg_after, 2) for r in s.rows],
        "notes": [r.buy.note or "" for r in s.rows],
        "price_now": round(s.price_now, 2) if s.price_now else None,
    }
