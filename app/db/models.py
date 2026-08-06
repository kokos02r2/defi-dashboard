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

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer,
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

    # Несобранные комиссии В ТОКЕНАХ и цены на момент замера — чтобы считать, сколько
    # НАЧИСЛИЛОСЬ за сутки. По долларовой сумме этого не узнать: у позиции вне
    # диапазона комиссии не капают вовсе, а их долларовая оценка всё равно ходит
    # туда-сюда вместе с курсом, и разница получилась бы то плюс, то минус.
    # В токенах же несобранное только растёт — до момента сбора.
    fees0_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees1_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    price0_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price1_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


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


class PriceAlert(Base):
    """Оповещение о пересечении цены: «сообщи, когда ETH перейдёт $2000 вниз».

    Пересечение, а не просто «цена ниже порога». Разница существенная: условие «ниже»
    выполняется постоянно, пока цена внизу, и слало бы сообщение каждую минуту. Чтобы
    поймать именно переход, нужна предыдущая цена — она хранится в last_price, и на
    первой проверке оповещение только «заряжается», ничего не отправляя: направление
    движения из одной точки неизвестно.

    Срабатывает один раз и выключается. Цена у порога ходит туда-сюда, и повторные
    сообщения превратились бы в поток — как это было с выходом из диапазона, пока там
    не появилась пауза. Взвести заново можно кнопкой.
    """
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))          # ETH / BTC
    price: Mapped[float] = mapped_column(Float)              # порог в долларах
    direction: Mapped[str] = mapped_column(String(4))        # up / down
    note: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    triggered_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def label(self) -> str:
        return f"{self.symbol} {'выше' if self.direction == 'up' else 'ниже'} ${self.price:,.2f}".replace(",", " ")


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


# --------------------------------------------------------------------------------------
# Личные финансы
#
# Второе пространство дашборда: доходы и расходы вне блокчейна. С крипто-частью оно
# не пересекается НИЧЕМ, кроме входа и вёрстки — ни одна таблица ниже не читается в
# jobs/refresh.py и не влияет на чистую стоимость портфеля. Это сознательно: деньги
# на карте и деньги в позиции Uniswap — разные сущности, и сложить их в один итог
# можно только осмысленным отдельным решением, а не побочным эффектом.
# --------------------------------------------------------------------------------------

class FxRate(Base):
    """Официальный курс ЦБ РФ: сколько рублей за единицу валюты в конкретный день.

    Рубль как опорная величина выбран не из-за особой роли, а потому что ЦБ публикует
    всё именно в такой форме. Курс любой пары получается делением двух строк, взятых
    на один день, — так считается и официальный кросс-курс.

    Курс за прошедший день неизменен навсегда, поэтому кэш здесь бессрочный: запрос к
    ЦБ делается один раз на дату. За выходные и праздники курса не публикуют — тогда
    под запрошенной датой лежит курс ближайшего предыдущего рабочего дня, и это тоже
    навсегда: прошлые выходные новыми данными не обрастут.
    """
    __tablename__ = "fx_rates"

    day: Mapped[datetime] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    rub: Mapped[float] = mapped_column(Float)
    # дата, на которую ЦБ реально опубликовал этот курс: для субботы здесь стоит пятница
    as_of: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinAccount(Base):
    """Счёт: карта конкретного банка, наличные, счёт в другой валюте.

    Из операций остаток не считается: это требовало бы, чтобы в базе была каждая до
    последней операция и начальное сальдо, иначе цифра тихо расходится с реальностью.
    Счёт здесь нужен для другого: сказать, из какой выписки пришла операция и в какой
    она валюте, и дать разбивку расходов по банкам. Сколько на счёте лежит сейчас —
    отдельная запись руками, см. FinBalance.
    """
    __tablename__ = "fin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    note: Mapped[str] = mapped_column(String(200), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Сопоставление колонок последней удачной загрузки для этого счёта: у каждого банка
    # своя шапка, но она не меняется от выписки к выписке — второй раз указывать не надо.
    import_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinCategory(Base):
    """Категория расхода или дохода. Плоский список, без вложенности.

    Без групп и без лимитов: спрошено прямо — нужно понимать, на что ушли деньги,
    а не следить за исполнением бюджета. Вложенность и планы добавили бы экраны,
    которые никто не открывает.
    """
    __tablename__ = "fin_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(8), index=True)   # expense / income
    color: Mapped[str] = mapped_column(String(16), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("name", "kind", name="uq_fin_category_name_kind"),)


class FinTx(Base):
    """Одна операция: расход или доход.

    Сумма всегда положительная, а знак живёт в kind. Иначе одно и то же приходится
    помнить в двух местах, и рано или поздно в базу попадает доход с минусом.

    Пересчёт в базовую валюту записан в amount_base прямо при сохранении, вместе с
    применённым курсом. Считать его на лету при каждом открытии отчёта означало бы,
    что цифры за прошлый год меняются от сегодняшнего курса — история должна стоять
    на месте. Курс берётся на дату операции, а не на сегодня.
    """
    __tablename__ = "fin_tx"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("fin_accounts.id", ondelete="CASCADE"), index=True)
    day: Mapped[datetime] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)    # expense / income
    amount: Mapped[float] = mapped_column(Float, default=0.0)   # в валюте операции
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    amount_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_code: Mapped[str] = mapped_column(String(3), default="")
    rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_categories.id", ondelete="SET NULL"), nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    # Не учитывать в итогах, но и не терять. Ровно для переводов между своими счетами:
    # они приходят в выписке наравне с покупками, но расходом не являются — учтёшь их,
    # и расходы вырастут на сумму каждого перевода.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(8), default="manual")   # manual / import
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_batches.id", ondelete="SET NULL"), nullable=True, index=True)
    # Отпечаток операции — защита от повторной загрузки. Выписки перекрываются: скачал
    # за июль, потом за июнь–июль, и половина строк приходит второй раз.
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped["FinAccount"] = relationship(lazy="joined")
    category: Mapped["FinCategory | None"] = relationship(lazy="joined")

    __table_args__ = (Index("ix_fin_tx_day_kind", "day", "kind"),)

    @property
    def signed_base(self) -> float:
        """Сумма в базовой валюте со знаком: расход — минус. Для графиков и сальдо."""
        v = self.amount_base or 0.0
        return -v if self.kind == "expense" else v

    @property
    def rule_hint(self) -> str:
        """Заготовка образца для правила: самое длинное слово из описания.

        В выписке описание выглядит как «MERCADONA 4021 BARCELONA 12/07» — правилом
        должно стать «mercadona», а не вся строка с номером терминала и датой, иначе
        оно не подойдёт ни к одной следующей операции. Цифры отбрасываются по той же
        причине. Человек видит подставленное слово и при необходимости правит.
        """
        import re
        words = [w for w in re.split(r"[^\w]+", (self.note or "").lower())
                 if len(w) >= 4 and not w.isdigit()]
        return max(words, key=len) if words else ""


class FinImportBatch(Base):
    """Одна загрузка файла: сколько строк пришло, сколько добавилось, сколько повторов.

    Партия нужна, чтобы загрузку можно было отменить целиком. Залил не тот файл или не
    тот счёт — одна кнопка возвращает базу к прежнему состоянию. Без этого разбирать
    двести чужих строк пришлось бы руками.
    """
    __tablename__ = "fin_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_accounts.id", ondelete="SET NULL"), nullable=True)
    filename: Mapped[str] = mapped_column(String(200), default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    ignored: Mapped[int] = mapped_column(Integer, default=0)   # отсеяно правилами
    failed: Mapped[int] = mapped_column(Integer, default=0)    # не разобрана строка
    mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    account: Mapped["FinAccount | None"] = relationship(lazy="joined")


class FinRule(Base):
    """Правило автокатегоризации: подстрока в описании — значит эта категория.

    Главная вещь во всей затее. Триста строк из банковской выписки вручную не разложит
    никто, и через месяц учёт заброшен. Одно правило «mercadona → Продукты» разбирает
    все будущие выписки само.

    Правило со skip не категоризует, а отсеивает строку при загрузке — этим убираются
    переводы между своими счетами, которых мы вообще не хотим видеть.
    """
    __tablename__ = "fin_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(200))          # подстрока, регистр не важен
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_categories.id", ondelete="CASCADE"), nullable=True)
    skip: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(8), default="")    # пусто — к любым операциям
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    category: Mapped["FinCategory | None"] = relationship(lazy="joined")


class FinBalance(Base):
    """Сколько денег есть сейчас: остаток на счёте или наличными на конкретную дату.

    Записывается руками и из операций не выводится. Остаток, посчитанный по выписке,
    требовал бы, чтобы в базе была каждая до последней операция и начальное сальдо —
    а выписки заливаются раз в месяц и не всегда полностью, так что он тихо расходился
    бы с реальностью. Здесь человек просто вписывает число, которое видит в банке.

    Пересчёт в валюту отчётов записан рядом, как и у операций: иначе прошлогодний
    остаток менялся бы каждый день вместе с курсом, и по истории нельзя было бы
    понять, копятся деньги или нет.
    """
    __tablename__ = "fin_balances"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("fin_accounts.id", ondelete="CASCADE"), index=True)
    day: Mapped[datetime] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)   # в валюте счёта
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    amount_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_code: Mapped[str] = mapped_column(String(3), default="")
    rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped["FinAccount"] = relationship(lazy="joined")

    # Одна запись на счёт и дату: вписал сумму второй раз за день — это уточнение,
    # а не вторая пачка денег.
    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_fin_balance_day"),)


class KV(Base):
    """Служебное состояние: время последних прогонов, статус задач."""
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
