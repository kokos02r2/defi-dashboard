"""Цены в USD через DefiLlama (бесплатно, без ключа), с кэшем в SQLite.

Разделение по изменяемости:
  * историческая цена за прошедший час больше не изменится — храним вечно;
  * текущая цена живёт TTL секунд, чтобы минутный рефреш не долбил API.

Отсутствие котировки тоже кэшируется (price = NULL): иначе по каждому «мусорному»
токену мы бы ходили в API на каждом круге обновления.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.chains import NATIVE_TOKEN, Chain
from app.db.models import PriceCache, utcnow

log = logging.getLogger(__name__)

CURRENT_TTL = 55           # секунд; чуть меньше периода live-обновления
SEARCH_WIDTH = "6h"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# Запасной ключ для ИСТОРИЧЕСКИХ цен.
#
# У DefiLlama история по адресу токена на конкретной сети начинается не с рождения
# токена: по WETH и USD₮0 на Arbitrum данных раньше осени 2024 нет вовсе — ни с
# шестичасовым окном поиска, ни с четырёхдневным. А тот же самый актив под
# идентификатором CoinGecko отдаётся за любую дату. На реальных данных из-за этого
# потерялось 65 сборов комиссий на $1838 — они просто выпали из всех итогов.
#
# Подмена корректна, а не удобна: обёртка отличается от самой монеты на десятые
# доли процента, и тикер в шапке по той же причине берёт курс монеты, а не обёртки.
# Только для истории: текущую цену адресный ключ отдаёт всегда и точнее.
FALLBACK_COINS = {
    # ETH и обёртки
    "coingecko:ethereum": ("ethereum:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                           "arbitrum:0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
                           "base:0x4200000000000000000000000000000000000006",
                           "optimism:0x4200000000000000000000000000000000000006"),
    # стейблкоины: у мостовых версий история особенно рваная
    "coingecko:usd-coin": ("ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                           "arbitrum:0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                           "base:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                           "optimism:0x0b2c639c533813f4aa9d7837caf62653d097ff85",
                           "polygon:0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"),
    "coingecko:tether": ("ethereum:0xdac17f958d2ee523a2206206994597c13d831ec7",
                         "arbitrum:0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
                         "polygon:0xc2132d05d31c914a87c6611c10748aeb04b58e8f"),
    # BTC и обёртки
    "coingecko:bitcoin": ("ethereum:0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
                          "arbitrum:0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",
                          "base:0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"),
}

# развёрнутая карта: адресный ключ -> ключ CoinGecko
_FALLBACK: dict[str, str] = {addr: cg for cg, addrs in FALLBACK_COINS.items()
                             for addr in addrs}


def fallback_coin(coin: str) -> str | None:
    """Чем заменить ключ, если по нему исторических данных нет."""
    return _FALLBACK.get((coin or "").lower())


def _nearest(entry: Any, ts: int) -> Decimal | None:
    """Ближайшая к моменту котировка из ответа batchHistorical, если она достаточно близко."""
    prices = (entry or {}).get("prices") or []
    near = min(prices, key=lambda x: abs(x["timestamp"] - ts), default=None)
    if near and abs(near["timestamp"] - ts) <= 6 * 3600:
        return Decimal(str(near["price"]))
    return None


def coin_key(chain: Chain, token: str) -> str:
    """Ключ монеты для DefiLlama. Нативную монету подменяем обёрнутой версией."""
    addr = (token or "").lower()
    if addr in (NATIVE_TOKEN.lower(), ZERO_ADDR) and chain.wrapped_native:
        addr = chain.wrapped_native.lower()
    return f"{chain.llama}:{addr}"


def _http_json(url: str, timeout: int = 30) -> Any:
    # без User-Agent часть шлюзов отвечает 403 на python-urllib
    req = urllib.request.Request(url, headers={"User-Agent": "defi-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class PriceService:
    """Цены с двухуровневым кэшем: память на время задачи + SQLite между запусками."""

    def __init__(self, db: Session):
        self.db = db
        self._mem: dict[str, Decimal | None] = {}

    # ---------------------------------------------------------------- текущие цены

    def current(self, coins: list[str]) -> dict[str, Decimal]:
        coins = sorted(set(coins))
        if not coins:
            return {}

        fresh_after = time.time() - CURRENT_TTL
        todo: list[str] = []
        for c in coins:
            if c in self._mem:
                continue
            row = self.db.scalar(select(PriceCache).where(PriceCache.coin == c,
                                                          PriceCache.hour == 0))
            if row is not None and row.fetched_at.timestamp() > fresh_after:
                self._mem[c] = Decimal(str(row.price)) if row.price is not None else None
            else:
                todo.append(c)

        for i in range(0, len(todo), 40):
            part = todo[i:i + 40]
            got: dict[str, Any] = {}
            try:
                data = _http_json("https://coins.llama.fi/prices/current/" + ",".join(part))
                got = data.get("coins", {})
            except Exception as e:  # noqa: BLE001 — цены необязательны, отчёт строим и без них
                log.warning("[prices] текущие котировки недоступны: %s", str(e)[:120])
            for c in part:
                pr = got.get(c, {}).get("price")
                val = Decimal(str(pr)) if pr is not None else None
                self._mem[c] = val
                self._upsert(c, 0, val)

        return {c: v for c in coins if (v := self._mem.get(c)) is not None}

    # ------------------------------------------------------------ исторические цены

    def prefetch(self, pairs: set[tuple[str, int]]) -> None:
        """
        Пакетная загрузка исторических цен.

        Иначе на каждое событие каждой позиции уходил бы отдельный HTTP-запрос: для
        кошелька на сотню пулов это сотни последовательных обращений и минуты ожидания.
        """
        need: dict[str, list[int]] = {}
        for coin, ts in sorted(pairs):
            if self._hist_cached(coin, ts) is _MISS:
                need.setdefault(coin, []).append(ts)
        if not need:
            return

        flat = [(c, t) for c, tss in need.items() for t in tss]
        log.info("[prices] исторических котировок нужно: %s", len(flat))

        for i in range(0, len(flat), 80):
            chunk = flat[i:i + 80]
            want: dict[str, list[int]] = {}
            for c, t in chunk:
                want.setdefault(c, []).append(t)
            url = ("https://coins.llama.fi/batchHistorical?coins="
                   + urllib.parse.quote(json.dumps(want)) + f"&searchWidth={SEARCH_WIDTH}")
            try:
                data = _http_json(url, timeout=60)
            except Exception as e:  # noqa: BLE001 — есть поштучный запасной путь
                log.warning("[prices] пакетная загрузка не удалась: %s", str(e)[:120])
                continue
            got = data.get("coins", {})
            missed: list[tuple[str, int]] = []
            for c, t in chunk:
                val = _nearest(got.get(c), t)
                if val is not None:
                    self._store_hist(c, t, val)
                else:
                    missed.append((c, t))
            self._prefetch_fallback(missed)

    def _prefetch_fallback(self, missed: list[tuple[str, int]]) -> None:
        """Второй заход по промахам — под ключом CoinGecko.

        Результат кладём под ИСХОДНЫЙ ключ: дальше вся программа спрашивает цену по
        адресу токена и получает её из кэша, ничего не зная про подмену.
        """
        want: dict[str, list[int]] = {}
        back: dict[tuple[str, int], tuple[str, int]] = {}
        for coin, ts in missed:
            alt = fallback_coin(coin)
            if not alt:
                self._store_hist(coin, ts, None)     # заменить нечем — промах навсегда
                continue
            want.setdefault(alt, []).append(ts)
            back[(alt, ts)] = (coin, ts)
        if not want:
            return

        got: dict[str, Any] = {}
        try:
            url = ("https://coins.llama.fi/batchHistorical?coins="
                   + urllib.parse.quote(json.dumps(want)) + f"&searchWidth={SEARCH_WIDTH}")
            got = (_http_json(url, timeout=60) or {}).get("coins", {})
        except Exception as e:  # noqa: BLE001 — не вышло, значит цены просто нет
            log.warning("[prices] запасной источник недоступен: %s", str(e)[:120])

        recovered = 0
        for (alt, ts), (coin, _) in back.items():
            val = _nearest(got.get(alt), ts)
            self._store_hist(coin, ts, val)
            recovered += val is not None
        if recovered:
            log.info("[prices] по запасному ключу восстановлено котировок: %s", recovered)

    def at(self, coin: str, ts: int) -> Decimal | None:
        cached = self._hist_cached(coin, ts)
        if cached is not _MISS:
            return cached
        val = self._one_hist(coin, ts)
        if val is None:
            alt = fallback_coin(coin)
            if alt:
                val = self._one_hist(alt, ts)
                if val is not None:
                    log.debug("[prices] %s за %s взят по ключу %s", coin, ts, alt)
        self._store_hist(coin, ts, val)
        return val

    def _one_hist(self, coin: str, ts: int) -> Decimal | None:
        try:
            data = _http_json(
                f"https://coins.llama.fi/prices/historical/{ts}/{coin}?searchWidth={SEARCH_WIDTH}")
            pr = data.get("coins", {}).get(coin, {}).get("price")
            return Decimal(str(pr)) if pr is not None else None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------- внутреннее

    def _hist_cached(self, coin: str, ts: int):
        hour = ts // 3600
        key = f"{coin}@{hour}"
        if key in self._mem:
            return self._mem[key]
        row = self.db.scalar(select(PriceCache).where(PriceCache.coin == coin,
                                                      PriceCache.hour == hour))
        if row is None:
            return _MISS
        val = Decimal(str(row.price)) if row.price is not None else None
        self._mem[key] = val
        return val

    def _store_hist(self, coin: str, ts: int, val: Decimal | None) -> None:
        hour = ts // 3600
        self._mem[f"{coin}@{hour}"] = val
        self._upsert(coin, hour, val)

    def _upsert(self, coin: str, hour: int, val: Decimal | None) -> None:
        row = self.db.scalar(select(PriceCache).where(PriceCache.coin == coin,
                                                      PriceCache.hour == hour))
        price = float(val) if val is not None else None
        if row is None:
            self.db.add(PriceCache(coin=coin, hour=hour, price=price, fetched_at=utcnow()))
        else:
            row.price = price
            row.fetched_at = utcnow()


class _Miss:
    """Отличает «в кэше нет записи» от «в кэше записано, что цены не существует»."""
    def __repr__(self) -> str:
        return "<miss>"


_MISS = _Miss()
