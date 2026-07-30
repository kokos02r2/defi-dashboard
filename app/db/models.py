"""Модели данных.

Ключевая идея схемы — разделить данные по изменяемости:

  иммутабельное (кэшируется навсегда): TokenMeta, BlockTime, PriceCache
  append-only (досканируется инкрементально): PositionEvent
  живое (перезаписывается каждую минуту): Position.*_usd, tick_current, ...
  историческое (только растёт): Snapshot, PositionSnapshot, Alert

Сырые ончейн-величины хранятся строками: они не помещаются в 64-битный INTEGER
SQLite. Долларовые оценки — Float, точности с запасом хватает для отображения.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Доступ
# --------------------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    positions: Mapped[list["Position"]] = relationship(back_populates="wallet",
                                                       cascade="all, delete-orphan")

    @property
    def short(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}"

    @property
    def title(self) -> str:
        return self.label or self.short


# --------------------------------------------------------------------------------------
# Иммутабельные кэши — то, ради чего в основном и заводится БД
# --------------------------------------------------------------------------------------

class TokenMeta(Base):
    """symbol/decimals токена. Меняться не может, живёт вечно."""
    __tablename__ = "token_meta"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_token"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), index=True)
    address: Mapped[str] = mapped_column(String(42), index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="?")
    decimals: Mapped[int] = mapped_column(Integer, default=18)


class BlockTime(Base):
    """Время блока. Иммутабельно после финализации."""
    __tablename__ = "block_times"
    __table_args__ = (UniqueConstraint("chain", "number", name="uq_block"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), index=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    timestamp: Mapped[int] = mapped_column(Integer)


class PriceCache(Base):
    """Цена монеты на конкретный час. Прошлое не меняется — храним навсегда.

    hour = timestamp // 3600. Для текущей цены hour = 0 и запись перезаписывается.
    """
    __tablename__ = "price_cache"
    __table_args__ = (UniqueConstraint("coin", "hour", name="uq_price"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), index=True)   # 'ethereum:0xabc…'
    hour: Mapped[int] = mapped_column(Integer, index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = котировки нет
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------------------
# Позиции
# --------------------------------------------------------------------------------------

PROTO_UNIV3 = "uniswap_v3"
PROTO_FLUID_LEND = "fluid_lending"
PROTO_FLUID_VAULT = "fluid_vault"

PROTOCOL_TITLES = {
    PROTO_UNIV3: "Uniswap V3",
    PROTO_FLUID_LEND: "Fluid Lending",
    PROTO_FLUID_VAULT: "Fluid Vault",
}


class Position(Base):
    """Нормализованная позиция — общая форма для всех протоколов.

    Всё, что специфично для конкретного протокола (тики, диапазоны, health factor,
    список событий), лежит в detail: JSON. Общие поля вынесены в колонки, чтобы по
    ним можно было сортировать, фильтровать и складывать итоги без разбора JSON.
    """
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("wallet_id", "protocol", "chain", "external_id", name="uq_position"),
        Index("ix_pos_open", "is_open"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), index=True)
    protocol: Mapped[str] = mapped_column(String(24), index=True)
    chain: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str] = mapped_column(String(80))   # tokenId / nftId / адрес fToken

    title: Mapped[str] = mapped_column(String(96), default="")     # 'WETH/USDC 0.3%'
    subtitle: Mapped[str] = mapped_column(String(96), default="")  # 'залог ETH → долг USDC'
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- деньги (USD)
    value_usd: Mapped[float | None] = mapped_column(Float, nullable=True)   # активы позиции
    debt_usd: Mapped[float | None] = mapped_column(Float, nullable=True)    # долг (Fluid vault)
    net_usd: Mapped[float | None] = mapped_column(Float, nullable=True)     # активы минус долг
    fees_unclaimed_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_claimed_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    deposited_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    withdrawn_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    apr: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- риск
    in_range: Mapped[bool | None] = mapped_column(Boolean, nullable=True)     # Uniswap
    health_factor: Mapped[float | None] = mapped_column(Float, nullable=True)  # Fluid vault
    ltv: Mapped[float | None] = mapped_column(Float, nullable=True)

    opened_at: Mapped[int | None] = mapped_column(Integer, nullable=True)   # unix
    closed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- состояние синхронизации
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    history_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scanned_block: Mapped[int] = mapped_column(Integer, default=0)
    live_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    wallet: Mapped["Wallet"] = relationship(back_populates="positions")
    events: Mapped[list["PositionEvent"]] = relationship(back_populates="position",
                                                         cascade="all, delete-orphan")

    @property
    def protocol_title(self) -> str:
        return PROTOCOL_TITLES.get(self.protocol, self.protocol)

    @property
    def needs_history(self) -> bool:
        """У закрытой позиции с полной историей досканировать нечего — она иммутабельна."""
        return not (self.history_complete and not self.is_open)


class PositionEvent(Base):
    """Событие позиции (Uniswap: Increase/Decrease/Collect/Transfer).

    Append-only. Пара (block, log_index) уникальна и задаёт точный порядок в цепочке —
    он важен, потому что Collect разбирается на тело и комиссии последовательно.
    """
    __tablename__ = "position_events"
    __table_args__ = (
        UniqueConstraint("position_id", "block", "log_index", name="uq_event"),
        Index("ix_event_pos", "position_id", "block", "log_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"),
                                             index=True)
    kind: Mapped[str] = mapped_column(String(16))      # increase/decrease/collect/transfer
    block: Mapped[int] = mapped_column(Integer)
    log_index: Mapped[int] = mapped_column(Integer)
    tx: Mapped[str] = mapped_column(String(80), default="")
    timestamp: Mapped[int | None] = mapped_column(Integer, nullable=True)

    liquidity: Mapped[str] = mapped_column(String(80), default="0")
    amount0: Mapped[str] = mapped_column(String(80), default="0")
    amount1: Mapped[str] = mapped_column(String(80), default="0")
    fee0: Mapped[str] = mapped_column(String(80), default="0")
    fee1: Mapped[str] = mapped_column(String(80), default="0")

    usd_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_usd_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    price0_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price1_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)   # from/to для transfer

    position: Mapped["Position"] = relationship(back_populates="events")


# --------------------------------------------------------------------------------------
# История портфеля — то, чего принципиально не мог дать CLI
# --------------------------------------------------------------------------------------

class Snapshot(Base):
    """Точка на графике капитала. wallet_id = NULL — суммарно по всем кошелькам."""
    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snap_ts", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"),
                                                  nullable=True, index=True)

    value_usd: Mapped[float] = mapped_column(Float, default=0.0)
    debt_usd: Mapped[float] = mapped_column(Float, default=0.0)
    net_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fees_unclaimed_usd: Mapped[float] = mapped_column(Float, default=0.0)
    positions_open: Mapped[int] = mapped_column(Integer, default=0)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)  # по протоколам и сетям


class PositionSnapshot(Base):
    """История по отдельной позиции — чтобы видеть, как накапливались комиссии."""
    __tablename__ = "position_snapshots"
    __table_args__ = (Index("ix_psnap", "position_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"),
                                             index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    value_usd: Mapped[float | None] = mapped_column(Float, nullable=True)   # активы / залог
    debt_usd: Mapped[float | None] = mapped_column(Float, nullable=True)    # долг (Fluid vault)
    net_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_unclaimed_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    health_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_range: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Ставки и балансы В ТОКЕНАХ на момент замера — чтобы потом посчитать, сколько
    # плечо стоило ФАКТИЧЕСКИ, а не «если ставка не изменится».
    #
    # Ставки во Fluid плавают вместе с загрузкой пула, и восстановить их задним числом
    # нельзя: ни в базе, ни в дешёвом виде в блокчейне их нет. Записанная ставка плюс
    # записанный баланс плюс известный интервал между замерами дают проинтегрированную
    # стоимость — то есть факт.
    #
    # В токенах, а не в долларах: долларовая сумма долга шевелится вместе с курсом
    # (пусть у USDC и слабо), а нас интересует именно рост самого долга от процентов.
    borrow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)   # % годовых
    supply_rate: Mapped[float | None] = mapped_column(Float, nullable=True)   # % годовых
    debt_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    collateral_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)


class Alert(Base):
    """Событие, требующее внимания: выход из диапазона, просадка health factor."""
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"),
                                                    nullable=True)
    kind: Mapped[str] = mapped_column(String(32))       # out_of_range / back_in_range / health / liquidated
    severity: Mapped[str] = mapped_column(String(16), default="info")   # info / warning / danger
    message: Mapped[str] = mapped_column(Text, default="")
    seen: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # состояние доставки в Telegram; в UI алерт виден в любом случае
    #   pending    — ждёт отправки
    #   sent       — доставлен
    #   suppressed — придавлен паузой против дребезга на границе диапазона
    #   disabled   — уведомления не настроены (чтобы потом не пришёл поток старых)
    #   failed     — отправить не удалось
    notify_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TempDeposit(Base):
    """Деньги, заведённые в позиции ВРЕМЕННО — например, чтобы поднять health factor.

    Такая сумма физически лежит внутри позиции и попадает в её стоимость, но прибылью
    не является: это собственные деньги, положенные «в долг самому себе». При сравнении
    с исходным вложением она вычитается из текущей стоимости, иначе дашборд показал бы
    ровно на эту сумму несуществующий рост.

    Забрали деньги обратно — удалите запись, и сравнение вернётся к прежней базе.
    """
    __tablename__ = "temp_deposits"

    id: Mapped[int] = mapped_column(primary_key=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TokenLot(Base):
    """Партия актива с зафиксированной средней ценой покупки.

    Практический смысл: позиция Uniswap вышла вниз из диапазона и распродала стейбл
    в ETH по некоторой средней цене. Этот ETH перекладывается во Fluid под залог, но
    цена набора нигде в блокчейне не записана — её надо помнить, чтобы потом не
    продать дешевле, чем купил.
    """
    __tablename__ = "token_lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    coin: Mapped[str] = mapped_column(String(64), default="")   # ключ цены DefiLlama
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    source_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def cost_usd(self) -> float:
        return (self.amount or 0) * (self.avg_price_usd or 0)


class BtcBuy(Base):
    """Покупка BTC со заклеймленных комиссий — просто журнал, отдельно от всего.

    Этот BTC СОЗНАТЕЛЬНО не участвует ни в одном расчёте дашборда: ни в чистой
    стоимости, ни в PnL, ни в сравнении с исходным вложением. Причина простая —
    он куплен на уже полученные комиссии и лежит во Fluid как самостоятельный
    актив; подмешав его в итоги портфеля, мы посчитали бы одни и те же деньги
    дважды: сперва как комиссию, потом как купленный на неё биткоин.

    Поэтому таблица не читается ни в jobs/refresh.py, ни в расчёте итогов —
    только на своей странице. Если однажды понадобится включить её в общий счёт,
    это будет отдельное осознанное решение, а не побочный эффект.
    """
    __tablename__ = "btc_buys"

    id: Mapped[int] = mapped_column(primary_key=True)
    amount_btc: Mapped[float] = mapped_column(Float, default=0.0)
    price_usd: Mapped[float] = mapped_column(Float, default=0.0)   # цена за 1 BTC
    bought_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def cost_usd(self) -> float:
        return (self.amount_btc or 0) * (self.price_usd or 0)


class KV(Base):
    """Служебное состояние: время последних прогонов, статус задач."""
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
