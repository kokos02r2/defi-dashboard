"""Курсы ETH и BTC для тикера в шапке.

Берутся по идентификаторам CoinGecko, а не по адресам WETH/WBTC: нужен курс самой
монеты, а обёртки иногда отклоняются от него на десятые доли процента.

Результат кэшируется в KV на TTL секунд — тикер обновляется раз в минуту у каждой
открытой вкладки, и без кэша это било бы в DefiLlama на каждый запрос.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import KV, utcnow

log = logging.getLogger(__name__)

TTL = 120
KV_KEY = "market_rates"

COINS = [
    ("ETH", "coingecko:ethereum"),
    ("BTC", "coingecko:bitcoin"),
]


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
        out.append({"symbol": sym, "price": float(price),
                    "change_24h": float(ch) if ch is not None else None})
    return out


def market_rates(db: Session, force: bool = False) -> list[dict]:
    row = db.get(KV, KV_KEY)
    now = time.time()
    if not force and row is not None:
        val = row.value or {}
        if now - float(val.get("fetched_at", 0)) < TTL and val.get("rates"):
            return val["rates"]

    rates = _fetch()
    if not rates:
        # сеть подвела — лучше показать прошлые цифры, чем пустое место
        return (row.value or {}).get("rates", []) if row is not None else []

    payload = {"fetched_at": now, "rates": rates}
    if row is None:
        db.add(KV(key=KV_KEY, value=payload))
    else:
        row.value = payload
        row.updated_at = utcnow()
    db.commit()
    return rates
