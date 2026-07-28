"""Математика Uniswap V3. Формулы перенесены из uniswap_positions.py дословно."""

from __future__ import annotations

from decimal import Decimal, getcontext

getcontext().prec = 60

Q96 = Decimal(2) ** 96
Q128 = 1 << 128
MASK256 = (1 << 256) - 1

STABLES = {"USDC", "USDT", "DAI", "USDC.E", "USDBC", "FRAX", "LUSD", "TUSD", "USDE", "SUSD",
           "GHO", "USDS", "BUSD", "MIM", "CRVUSD", "USDD", "PYUSD", "FDUSD", "USDP", "RLUSD"}


def is_stable(symbol: str) -> bool:
    return (symbol or "").upper() in STABLES


def sqrt_ratio_at_tick(tick: int) -> Decimal:
    return Decimal("1.0001") ** (Decimal(tick) / 2)


def price_at_tick(tick: int, dec0: int, dec1: int) -> Decimal:
    """Цена token0, выраженная в token1 (сколько token1 за 1 token0)."""
    return (Decimal("1.0001") ** Decimal(tick)) * (Decimal(10) ** (dec0 - dec1))


def amounts_from_liquidity(liquidity: int, sqrt_p_x96: int, tick_lower: int,
                           tick_upper: int) -> tuple[Decimal, Decimal]:
    """Сколько token0/token1 (в raw-единицах) лежит в позиции при текущей цене."""
    if liquidity == 0:
        return Decimal(0), Decimal(0)
    sp = Decimal(sqrt_p_x96) / Q96
    sa = sqrt_ratio_at_tick(tick_lower)
    sb = sqrt_ratio_at_tick(tick_upper)
    spc = min(max(sp, sa), sb)
    amount0 = Decimal(liquidity) * (sb - spc) / (spc * sb)
    amount1 = Decimal(liquidity) * (spc - sa)
    return amount0, amount1


def uncollected_fees(liquidity: int, fee_growth_global: int, fee_outside_lower: int,
                     fee_outside_upper: int, fee_growth_inside_last: int,
                     tick_cur: int, tick_lower: int, tick_upper: int) -> int:
    """Комиссии, накопленные позицией, но ещё не собранные (raw-единицы токена)."""
    below = fee_outside_lower if tick_cur >= tick_lower else (fee_growth_global - fee_outside_lower) & MASK256
    above = fee_outside_upper if tick_cur < tick_upper else (fee_growth_global - fee_outside_upper) & MASK256
    inside = (fee_growth_global - below - above) & MASK256
    delta = (inside - fee_growth_inside_last) & MASK256
    return (delta * liquidity) // Q128


def solve_breakeven(liquidity: int, sa: Decimal, sb: Decimal,
                    target: Decimal) -> tuple[Decimal | None, str, Decimal]:
    """
    При каком корне цены s = √P стоимость позиции станет равна target?

    Стоимость позиции в единицах котируемого токена (формула LP — она уже учитывает,
    что по мере роста цены позиция распродаёт актив):
        s < sa      V = L·(1/sa − 1/sb)·s²      растёт линейно по цене (весь актив на руках)
        sa ≤ s ≤ sb V = L·(2s − s²/sb − sa)     растёт как √P — часть актива уже продана
        s > sb      V = L·(sb − sa)             ПОТОЛОК: актив распродан полностью

    Из-за потолка возврат вложенного может быть недостижим ни при какой цене.
    Возвращает (s*, статус, максимально достижимая стоимость).
    """
    L = Decimal(liquidity)
    v_max = L * (sb - sa)
    if target > v_max:
        return None, "unreachable", v_max

    v_at_sa = L * (sa - sa * sa / sb)          # стоимость на нижней границе диапазона
    if target <= v_at_sa:
        k = L * (1 / sa - 1 / sb)
        return (target / k).sqrt(), "below_range", v_max

    # L·(2s − s²/sb − sa) = target  ⇒  s² − 2·sb·s + sb·(sa + target/L) = 0
    disc = sb * sb - sb * sa - sb * target / L
    disc = disc if disc > 0 else Decimal(0)
    return sb - disc.sqrt(), "in_range", v_max   # меньший корень — тот, что внутри диапазона
