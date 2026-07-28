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
            for c, t in chunk:
                prices = (got.get(c) or {}).get("prices") or []
                near = min(prices, key=lambda x: abs(x["timestamp"] - t), default=None)
                if near and abs(near["timestamp"] - t) <= 6 * 3600:
                    self._store_hist(c, t, Decimal(str(near["price"])))
                else:
                    # запоминаем промах, чтобы не переспрашивать поштучно
                    self._store_hist(c, t, None)

    def at(self, coin: str, ts: int) -> Decimal | None:
        cached = self._hist_cached(coin, ts)
        if cached is not _MISS:
            return cached
        try:
            data = _http_json(
                f"https://coins.llama.fi/prices/historical/{ts}/{coin}?searchWidth={SEARCH_WIDTH}")
            pr = data.get("coins", {}).get(coin, {}).get("price")
            val = Decimal(str(pr)) if pr is not None else None
        except Exception:  # noqa: BLE001
            val = None
        self._store_hist(coin, ts, val)
        return val

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
