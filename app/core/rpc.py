"""Работа с публичными RPC-нодами: переключение при сбоях, Multicall3, чтение логов.

Перенесено из uniswap_positions.py практически без изменений — это самая
отлаженная часть исходного скрипта. Добавлено:
  * кэш таймстемпов блоков можно подставить снаружи (мы держим его в SQLite);
  * логирование через logging вместо печати в stderr.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import os
import re
import time

from web3 import HTTPProvider, Web3

from app.core.abi import abi_decode, call_data
from app.core.chains import MULTICALL3, Chain

log = logging.getLogger(__name__)

MAX_LOG_SPAN = 5_000_000   # выше этого публичные шлюзы отваливаются по таймауту
TS_WORKERS = 6             # больше не помогает: ноды начинают резать по частоте

_TRANSIENT = ("429", "too many requests", "timed out", "timeout", "502", "503", "504",
              "521", "522", "connection", "temporarily", "internal error", "rate limit")


def _is_transient(msg: str) -> bool:
    """Отличаем «нода перегружена» от «нода режет диапазон» — реакция разная."""
    low = msg.lower()
    return any(t in low for t in _TRANSIENT)


def _parse_range_hint(msg: str) -> int | None:
    """Многие ноды сообщают свой лимит прямо в тексте ошибки — используем его."""
    for pat in (r"maximum block range[^0-9]{0,20}(\d{3,9})",
                r"block range[^0-9]{0,20}(\d{3,9})",
                r"up to (\d{3,9}) blocks",
                r"limited to (\d{3,9})"):
        m = re.search(pat, msg, re.I)
        if m:
            return max(int(m.group(1)) - 1, 500)
    return None


class Rpc:
    """Обёртка над несколькими публичными нодами: при ошибке/лимите берём следующую."""

    def __init__(self, chain: Chain, extra_rpc: str | None = None,
                 ts_cache: dict[int, int] | None = None):
        self.chain = chain
        env = os.environ.get(f"RPC_{chain.key.upper()}")
        head = ([env] if env else []) + ([extra_rpc] if extra_rpc else [])
        self.urls = head + list(chain.rpcs)
        # для eth_getLogs сначала пробуем ноды с архивом и широким диапазоном
        self.log_urls = head + list(chain.log_rpcs) + [u for u in chain.rpcs if u not in chain.log_rpcs]
        self.idx = 0
        self.max_span: int | None = None        # выученный лимит диапазона для getLogs
        self.ts_cache: dict[int, int] = ts_cache if ts_cache is not None else {}
        self.full_range_ok: bool | None = None  # тянет ли сеть логи за весь период разом
        self._w3: dict[str, Web3] = {}

    def w3(self, url: str) -> Web3:
        if url not in self._w3:
            self._w3[url] = Web3(HTTPProvider(url, request_kwargs={"timeout": 25}))
        return self._w3[url]

    def run(self, fn, urls: list[str] | None = None, quiet: bool = False):
        """fn(w3) -> результат. Перебираем ноды, пока не получится."""
        last = None
        pool = urls or self.urls
        order = pool[self.idx % len(pool):] + pool[:self.idx % len(pool)] if urls is None else pool
        for url in order:
            try:
                res = fn(self.w3(url))
                if urls is None:
                    self.idx = self.urls.index(url)
                return res
            except Exception as e:  # noqa: BLE001 — нам важен любой сбой ноды
                last = e
                if not quiet:
                    log.debug("[rpc] %s -> %s: %s", url, type(e).__name__, str(e)[:110])
                time.sleep(0.15)
        raise RuntimeError(f"все RPC {self.chain.name} недоступны: {last}")

    def eth_call(self, to: str, data: bytes) -> bytes:
        return self.run(lambda w: bytes(w.eth.call({"to": Web3.to_checksum_address(to), "data": data})))

    def block_number(self) -> int:
        return self.run(lambda w: w.eth.block_number)

    def contract_call(self, address: str, abi: list, fn_name: str, *args):
        """Вызов через полный ABI — для контрактов со сложными вложенными структурами."""
        def _call(w: Web3):
            c = w.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
            return getattr(c.functions, fn_name)(*args).call()
        return self.run(_call)

    def get_logs(self, params: dict, a: int, b: int) -> list[dict]:
        """Один запрос логов на диапазон [a, b]. Кидает исключение, если никто не смог."""
        q = dict(params, fromBlock=a, toBlock=b)
        return [dict(x) for x in self.run(lambda w: w.eth.get_logs(q), urls=self.log_urls, quiet=True)]

    def get_logs_ranged(self, params: dict, a: int, b: int, deadline: float,
                        max_requests: int = 60) -> list[dict] | None:
        """
        Логи за [a, b] с подстройкой под лимит конкретной ноды.

        Сначала (один раз на сеть) пробуем весь диапазон одним запросом — часть нод это
        умеет. Если нет, идём окнами: размер окна растёт при успехе и сжимается при отказе,
        причём временные сбои (429/таймаут/5xx) не считаются лимитом диапазона.

        None — если не уложились в дедлайн, в лимит запросов или окно стало слишком мелким.
        """
        if b < a:
            return []

        if self.full_range_ok is not False:
            try:
                res = self.get_logs(params, a, b)
                self.full_range_ok = True
                return res
            except Exception as e:  # noqa: BLE001
                if not _is_transient(str(e)):
                    self.full_range_ok = False
                log.debug("[logs] полный диапазон недоступен, иду окнами")

        out: list[dict] = []
        span = self.max_span or 1_000_000
        cur, used, retries = a, 0, 0
        while cur <= b:
            if time.time() > deadline or used >= max_requests:
                return None
            end = min(cur + span - 1, b)
            used += 1
            try:
                out.extend(self.get_logs(params, cur, end))
                cur = end + 1
                retries = 0
                self.max_span = span
                span = min(span * 2, MAX_LOG_SPAN)     # нода тянет — пробуем шире
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if _is_transient(msg) and retries < 2:
                    retries += 1
                    time.sleep(1.0)
                    continue
                retries = 0
                hint = _parse_range_hint(msg)
                span = hint if hint else max(span // 4, 500)
                self.max_span = span
                log.debug("[logs] сузил окно до %s блоков", span)
                if span < 500:
                    return None
        return out

    def fetch_block_times(self, blocks: set[int], deadline: float) -> None:
        """Таймстемпы блоков — параллельно и с кэшем: самая дорогая часть по числу запросов."""
        todo = sorted(b for b in blocks if b not in self.ts_cache)
        if not todo:
            return
        log.debug("[ts] запрашиваю время %s блоков", len(todo))

        def one(bn: int):
            if time.time() > deadline:
                return bn, None
            try:
                return bn, self.run(lambda w: w.eth.get_block(bn)["timestamp"], quiet=True)
            except Exception:  # noqa: BLE001
                return bn, None

        with cf.ThreadPoolExecutor(TS_WORKERS) as pool:
            for bn, ts in pool.map(one, todo):
                if ts is not None:
                    self.ts_cache[bn] = ts

        # добираем то, что не доехало из-за частотных лимитов: без времени блока
        # событие выпадает из долларовых расчётов, поэтому вторая попытка окупается
        missed = [b for b in todo if b not in self.ts_cache]
        if missed:
            log.debug("[ts] повтор для %s блоков", len(missed))
            for bn in missed:
                if time.time() > deadline:
                    break
                _, ts = one(bn)
                if ts is not None:
                    self.ts_cache[bn] = ts


def multicall(rpc: Rpc, calls: list[tuple[str, bytes]], chunk: int = 40) -> list[tuple[bool, bytes]]:
    """Multicall3.aggregate3 — десятки чтений за один HTTP-запрос."""
    out: list[tuple[bool, bytes]] = []
    for i in range(0, len(calls), chunk):
        part = calls[i:i + chunk]
        payload = call_data(
            "aggregate3((address,bool,bytes)[])",
            ["(address,bool,bytes)[]"],
            [[(Web3.to_checksum_address(t), True, d) for t, d in part]],
        )
        ret = rpc.eth_call(MULTICALL3, payload)
        decoded = abi_decode(["(bool,bytes)[]"], ret)[0]
        out.extend((bool(ok), bytes(res)) for ok, res in decoded)
    return out
