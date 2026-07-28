"""ABI-хелперы: считаем селекторы на лету, без загрузки полных ABI."""

from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3


def selector(sig: str) -> bytes:
    return Web3.keccak(text=sig)[:4]


def topic(sig: str) -> str:
    return Web3.keccak(text=sig).hex()


def call_data(sig: str, arg_types: list[str], args: list) -> bytes:
    return selector(sig) + (abi_encode(arg_types, args) if arg_types else b"")


def hx(t: str) -> str:
    """Нормализует hex-строку: web3 v7 отдаёт topics без префикса 0x."""
    return t if t.startswith("0x") else "0x" + t


def decode_string(raw: bytes) -> str:
    """symbol()/name() — обычно string, но у старых токенов bytes32."""
    if not raw:
        return "?"
    try:
        return abi_decode(["string"], raw)[0]
    except Exception:  # noqa: BLE001
        try:
            return raw[:32].rstrip(b"\x00").decode("utf-8", "ignore") or "?"
        except Exception:  # noqa: BLE001
            return "?"


__all__ = ["selector", "topic", "call_data", "hx", "decode_string", "abi_decode", "abi_encode"]
