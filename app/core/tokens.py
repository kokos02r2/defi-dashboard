"""Метаданные токенов (symbol/decimals) с вечным кэшем в SQLite.

symbol и decimals у ERC-20 неизменны, поэтому запрошенное однажды больше никогда
не перечитывается из сети — на кошельке с десятками пар это заметная экономия.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from web3 import Web3

from app.core.abi import abi_decode, call_data, decode_string
from app.core.chains import NATIVE_TOKEN, Chain
from app.core.rpc import Rpc, multicall
from app.db.models import TokenMeta

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


class TokenService:
    def __init__(self, db: Session, chain: Chain, rpc: Rpc):
        self.db = db
        self.chain = chain
        self.rpc = rpc
        self._mem: dict[str, tuple[str, int]] = {}

    def _is_native(self, addr: str) -> bool:
        return addr.lower() in (NATIVE_TOKEN.lower(), ZERO_ADDR)

    def resolve(self, addresses: list[str]) -> dict[str, tuple[str, int]]:
        """Адрес -> (symbol, decimals). Чего нет в кэше — добираем одним multicall."""
        want = sorted({Web3.to_checksum_address(a) for a in addresses if a})
        out: dict[str, tuple[str, int]] = {}
        missing: list[str] = []

        for a in want:
            if self._is_native(a):
                out[a] = (self.chain.native_symbol, 18)
                continue
            if a in self._mem:
                out[a] = self._mem[a]
                continue
            row = self.db.scalar(select(TokenMeta).where(TokenMeta.chain == self.chain.key,
                                                         TokenMeta.address == a))
            if row is not None:
                self._mem[a] = (row.symbol, row.decimals)
                out[a] = self._mem[a]
            else:
                missing.append(a)

        if missing:
            calls: list[tuple[str, bytes]] = []
            for a in missing:
                calls.append((a, call_data("symbol()", [], [])))
                calls.append((a, call_data("decimals()", [], [])))
            res = multicall(self.rpc, calls)
            for i, a in enumerate(missing):
                ok_s, raw_s = res[2 * i]
                ok_d, raw_d = res[2 * i + 1]
                sym = decode_string(raw_s) if ok_s else "?"
                try:
                    dec = abi_decode(["uint8"], raw_d)[0] if ok_d and raw_d else 18
                except Exception:  # noqa: BLE001 — нестандартный токен
                    dec = 18
                self._mem[a] = (sym, dec)
                out[a] = (sym, dec)
                self.db.add(TokenMeta(chain=self.chain.key, address=a, symbol=sym, decimals=dec))
            self.db.flush()

        return out
