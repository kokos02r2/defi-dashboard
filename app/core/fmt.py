"""Форматирование чисел для интерфейса. Перенесено из uniswap_positions.py."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


def _d(v) -> Decimal | None:
    """Всё, что не превращается в число, — это «нет данных», а не падение страницы.

    Здесь важна именно терпимость: сюда попадает и Undefined от Jinja, если шаблон
    ждёт переменную, которой роут не передал. Раньше такой случай ронял весь ответ
    в 500 — из-за одной несуществующей цифры пропадала вся страница.
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (ArithmeticError, ValueError, TypeError):
        return None


def amt(raw, decimals: int, digits: int = 6) -> str:
    """Сырое количество токена -> человеческая строка."""
    v = _d(raw)
    if v is None:
        return "—"
    v = v / (Decimal(10) ** decimals)
    if v == 0:
        return "0"
    if abs(v) >= 1000:
        return f"{v:,.2f}".replace(",", " ")
    q = Decimal(1).scaleb(-digits)
    return f"{v.quantize(q):f}".rstrip("0").rstrip(".")


def usd(v) -> str:
    v = _d(v)
    if v is None:
        return "н/д"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}".replace(",", " ")


def usd_short(v) -> str:
    """Компактно для плиток: $12.3k, $1.05M."""
    v = _d(v)
    if v is None:
        return "н/д"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.2f}M"
    if a >= 10_000:
        return f"{sign}${a / 1000:.1f}k"
    return f"{sign}${a:,.2f}".replace(",", " ")


def price(v) -> str:
    """Цена в пуле. У позиций «на весь диапазон» границы упираются в MIN/MAX_TICK —
    там осмысленнее показать ∞/0, чем число с полусотней знаков."""
    v = _d(v)
    if v is None:
        return "н/д"
    if v >= Decimal("1e18"):
        return "∞"
    if v == 0 or v <= Decimal("1e-18"):
        return "0"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.2f}".replace(",", " ")
    if a >= 1:
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return f"{v:.10f}".rstrip("0").rstrip(".")


def usd_price(v) -> str:
    """Курс токена в долларах — без лишней точности (стейблу не нужны 10 знаков)."""
    v = _d(v)
    if v is None:
        return "н/д"
    if v >= 1000:
        return f"{v:,.2f}".replace(",", " ")
    if v >= 1:
        return f"{v:.2f}"
    if v >= Decimal("0.01"):
        return f"{v:.4f}"
    return price(v)


def pct(v, digits: int = 2, sign: bool = False) -> str:
    v = _d(v)
    if v is None:
        return "н/д"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return f"{s}%"


def ts(v) -> str:
    if not v:
        return "н/д"
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M")
    return datetime.fromtimestamp(v, timezone.utc).strftime("%d.%m.%Y %H:%M")


def ts_short(v) -> str:
    if not v:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%d.%m %H:%M")
    return datetime.fromtimestamp(v, timezone.utc).strftime("%d.%m %H:%M")


def plural(n, one: str, few: str, many: str) -> str:
    """Русское согласование числительных: 1 сбор, 2 сбора, 5 сборов.

    В шаблоне то же самое выражением получается нечитаемым, а «5 сбора» в интерфейсе
    выглядит как недоделка.
    """
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def days_since(v) -> str:
    if not v:
        return "—"
    import time
    return f"{(time.time() - v) / 86400:.1f} дн."


FILTERS = {
    "amt": amt, "usd": usd, "usd_short": usd_short, "price": price,
    "usd_price": usd_price, "pct": pct, "ts": ts, "ts_short": ts_short,
    "days_since": days_since, "plural": plural,
}
