"""Оповещения о пересечении цены: ETH или BTC перешёл заданный порог.

Ключевое слово — «перешёл». Условие «цена ниже 2000» выполняется всё время, пока цена
внизу, и слало бы сообщение каждую минуту. Поэтому сравниваются ДВЕ точки: цена на
предыдущей проверке и текущая. Порог считается пересечённым, только если он оказался
между ними.

Из этого следуют два свойства, о которых честнее сказать сразу:

  * на первой проверке оповещение только «заряжается» — направление движения из одной
    точки не определить, и любое срабатывание было бы выдумкой;
  * если цена перешла порог и вернулась ОБРАТНО между двумя проверками, переход не
    будет замечен. Проверка идёт раз в минуту вместе с обновлением цен для тикера,
    так что пропустить можно только очень быстрый выброс.

Цены берутся из того же кэша, что кормит тикер в шапке: своих запросов к API нет.
"""

from __future__ import annotations

import html

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import notify
from app.db.models import PriceAlert, utcnow

SYMBOLS = ("ETH", "BTC")
DIRECTIONS = {"up": "вверх", "down": "вниз"}


def crossed(direction: str, target: float, prev: float, now: float) -> bool:
    """Пересёк ли порог между двумя измерениями.

    Границы включительно по текущей цене: ровно достигнутый порог — это уже переход,
    иначе оповещение на круглом числе могло бы не сработать никогда.
    """
    if direction == "up":
        return prev < target <= now
    return prev > target >= now


def check(db: Session, rates: list[dict]) -> int:
    """Проверяет включённые оповещения по свежим ценам. Возвращает число отправленных.

    rates — то же, что показывает тикер: список со symbol и price.
    """
    prices = {r.get("symbol"): r.get("price") for r in rates
              if r.get("kind") == "crypto" and r.get("price")}
    if not prices:
        return 0

    alerts = list(db.scalars(select(PriceAlert).where(PriceAlert.enabled.is_(True))).all())
    sent = 0
    for a in alerts:
        now_price = prices.get(a.symbol)
        if now_price is None:
            continue
        prev = a.last_price
        a.last_price = float(now_price)
        if prev is None:
            continue                        # первая проверка: только заряжаем
        if not crossed(a.direction, a.price, prev, float(now_price)):
            continue

        a.enabled = False                   # одноразовое: взводится кнопкой заново
        a.triggered_at = utcnow()
        a.triggered_price = float(now_price)
        if notify.send(message(a, prev, float(now_price))):
            sent += 1
    db.flush()
    return sent


def _usd(v: float) -> str:
    """Сумма с пробелами вместо запятых.

    Форматируется отдельной функцией, а не заменой запятых в готовой строке: такая
    замена съедала и запятые самого текста — «Порог $2 000.00, было» превращалось в
    «Порог $2 000.00  было».
    """
    return f"${v:,.2f}".replace(",", " ")


def message(a: PriceAlert, prev: float, now_price: float) -> str:
    arrow = "🔼" if a.direction == "up" else "🔽"
    body = f"{arrow} <b>{a.symbol} {DIRECTIONS[a.direction]}: {_usd(now_price)}</b>"
    body += f"\n\nПорог {_usd(a.price)}, было {_usd(prev)}"
    if a.note:
        body += "\n" + html.escape(a.note, quote=False)
    return body
