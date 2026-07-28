"""Общий контракт провайдеров.

Смысл слоя: UI, БД и расчёт итогов не знают ничего про конкретные протоколы.
Чтобы добавить Aave, Morpho или Uniswap V4, достаточно написать новый класс с
методом fetch() — трогать интерфейс и схему не придётся.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.chains import Chain
from app.core.prices import PriceService
from app.core.rpc import Rpc
from app.core.tokens import TokenService


@dataclass
class EventData:
    """Событие позиции в форме, пригодной для записи в БД."""
    kind: str
    block: int
    log_index: int
    tx: str = ""
    timestamp: int | None = None
    liquidity: int = 0
    amount0: int = 0
    amount1: int = 0
    fee0: int = 0
    fee1: int = 0
    usd_at_time: float | None = None
    fee_usd_at_time: float | None = None
    price0_usd: float | None = None
    price1_usd: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class RawPosition:
    """Нормализованная позиция — то, что провайдер отдаёт наружу."""
    protocol: str
    chain: str
    external_id: str
    title: str = ""
    subtitle: str = ""
    is_open: bool = True

    value_usd: float | None = None
    debt_usd: float | None = None
    net_usd: float | None = None
    fees_unclaimed_usd: float | None = None
    fees_claimed_usd: float | None = None
    deposited_usd: float | None = None
    withdrawn_usd: float | None = None
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    apr: float | None = None

    in_range: bool | None = None
    health_factor: float | None = None
    ltv: float | None = None

    opened_at: int | None = None
    closed_at: int | None = None

    detail: dict = field(default_factory=dict)

    # управление синхронизацией истории
    events: list[EventData] | None = None      # None = историю не трогали
    history_complete: bool = False
    last_scanned_block: int = 0


@dataclass
class KnownPosition:
    """Что мы уже знаем о позиции — чтобы не сканировать историю заново."""
    external_id: str
    last_scanned_block: int
    history_complete: bool
    is_open: bool
    db_id: int | None = None
    opened_at: int | None = None


@dataclass
class Ctx:
    """Всё, что нужно провайдеру для работы в одной сети."""
    db: Session
    chain: Chain
    rpc: Rpc
    prices: PriceService
    tokens: TokenService
    history_budget: float = 240.0


class Provider:
    key: str = ""
    title: str = ""

    def supports(self, chain: Chain) -> bool:
        raise NotImplementedError

    def fetch(self, ctx: Ctx, wallet: str, known: dict[str, KnownPosition],
              with_history: bool) -> list[RawPosition]:
        """
        Собрать позиции кошелька в одной сети.

        known    — уже сохранённые позиции; провайдер использует last_scanned_block,
                   чтобы досканировать только новые блоки.
        with_history — режим sync (читаем логи) против live (только текущее состояние).
        """
        raise NotImplementedError


def to_float(v: Decimal | float | None) -> float | None:
    if v is None:
        return None
    return float(v)


def jsonable(v: Any) -> Any:
    """Decimal и прочее -> то, что переживёт запись в JSON-колонку."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, bytes):
        return v.hex()
    return v
