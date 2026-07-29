"""Курсы для тикера в шапке: ETH и BTC, а рядом валюты — USD/RUB, EUR/RUB, EUR/USD.

Крипта берётся по идентификаторам CoinGecko, а не по адресам WETH/WBTC: нужен курс
самой монеты, а обёртки иногда отклоняются от него на десятые доли процента.

Валюты — по официальным курсам ЦБ РФ. Одним запросом приходит и текущее значение,
и предыдущее, поэтому у валют есть динамика, как у монет, без второго обращения.

У двух источников разный темп, поэтому и кэши раздельные: крипта живёт секунды,
курс ЦБ — публикуется раз в рабочий день, и опрашивать его каждые две минуты
незачем. Кэш вообще обязателен: тикер обновляется раз в минуту у каждой открытой
вкладки, и без него это било бы в чужие API на каждый запрос.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import KV, utcnow

log = logging.getLogger(__name__)

TTL = 120
KV_KEY = "market_rates"

COINS = [
    ("ETH", "coingecko:ethereum"),
    ("BTC", "coingecko:bitcoin"),
]

FX_TTL = 30 * 60
FX_KV_KEY = "market_fx"
FX_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


def _http_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "defi-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fetch() -> list[dict]:
    ids = ",".join(c for _, c in COINS)
    prices: dict[str, Any] = {}
    changes: dict[str, Any] = {}
    try:
        prices = (_http_json(f"https://coins.llama.fi/prices/current/{ids}") or {}).get("coins", {})
    except Exception as e:  # noqa: BLE001 — тикер необязателен, дашборд без него живёт
        log.warning("[market] цены недоступны: %s", str(e)[:120])
        return []
    try:
        changes = _http_json(f"https://coins.llama.fi/percentage/{ids}?period=24h") or {}
        changes = changes.get("coins", {})
    except Exception as e:  # noqa: BLE001 — без динамики просто покажем цену
        log.debug("[market] изменение за 24ч недоступно: %s", str(e)[:120])

    out: list[dict] = []
    for sym, coin in COINS:
        price = (prices.get(coin) or {}).get("price")
        if price is None:
            continue
        ch = changes.get(coin)
        out.append({"symbol": sym, "price": float(price), "kind": "crypto",
                    "change_24h": float(ch) if ch is not None else None})
    return out


def _pair(symbol: str, current: float, previous: float) -> dict:
    """Валютная пара с изменением к предыдущему опубликованному курсу.

    Это не «за 24 часа», как у монет: ЦБ публикует курс раз в рабочий день, и в
    выходные цифра стоит на месте — свойство источника, а не сбой. Поле названо
    так же, как у крипты, чтобы шаблон не разбирал два разных формата.
    """
    change = (current / previous - 1) * 100 if previous else None
    return {"symbol": symbol, "price": current, "change_24h": change,
            "kind": "fx", "change_title": "К предыдущему курсу ЦБ"}


def _fetch_fx() -> list[dict]:
    try:
        data = _http_json(FX_URL)
    except Exception as e:  # noqa: BLE001 — валюты тоже необязательны
        log.warning("[market] курсы валют недоступны: %s", str(e)[:120])
        return []

    valute = (data or {}).get("Valute") or {}

    def rub_per(code: str) -> tuple[float, float] | None:
        v = valute.get(code) or {}
        try:
            # Nominal у доллара и евро равен единице, но делим честно: у части
            # валют ЦБ публикует курс за 10 или 100 единиц
            nominal = float(v["Nominal"])
            return float(v["Value"]) / nominal, float(v["Previous"]) / nominal
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            log.debug("[market] нет курса %s в ответе ЦБ", code)
            return None

    usd, eur = rub_per("USD"), rub_per("EUR")
    out: list[dict] = []
    if usd:
        out.append(_pair("USD/RUB", *usd))
    if eur:
        out.append(_pair("EUR/RUB", *eur))
    if usd and eur:
        # Кросс-курс: сам ЦБ его не публикует, но обе ноги взяты на один момент,
        # поэтому делить их корректно — это и есть официальный кросс.
        out.append(_pair("EUR/USD", eur[0] / usd[0], eur[1] / usd[1]))
    return out


def _cached(db: Session, key: str, ttl: int, fetch: Callable[[], list[dict]],
            force: bool = False) -> list[dict]:
    row = db.get(KV, key)
    now = time.time()
    if not force and row is not None:
        val = row.value or {}
        if now - float(val.get("fetched_at", 0)) < ttl and val.get("rates"):
            return val["rates"]

    rates = fetch()
    if not rates:
        # сеть подвела — лучше показать прошлые цифры, чем пустое место
        return (row.value or {}).get("rates", []) if row is not None else []

    payload = {"fetched_at": now, "rates": rates}
    if row is None:
        db.add(KV(key=key, value=payload))
    else:
        row.value = payload
        row.updated_at = utcnow()
    db.commit()
    return rates


def market_rates(db: Session, force: bool = False) -> list[dict]:
    """Всё, что показывает тикер, одним списком: сначала монеты, потом валюты.

    Каждый источник падает независимо: недоступный ЦБ не уносит с собой ETH и BTC.
    """
    return (_cached(db, KV_KEY, TTL, _fetch, force)
            + _cached(db, FX_KV_KEY, FX_TTL, _fetch_fx, force))
