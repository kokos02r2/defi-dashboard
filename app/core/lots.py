"""Учёт партий: сводка по средней цене и текущая переоценка.

Ключевая цифра — средневзвешенная цена по всем партиям одного актива. Именно ниже
неё продавать не хочется, и именно её надо держать перед глазами.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.prices import PriceService
from app.db.models import TokenMeta, TokenLot

# Для активов, которых нет среди токенов позиций, берём общерыночный курс.
# DefiLlama понимает идентификаторы CoinGecko.
FALLBACK_COINS = {
    "ETH": "coingecko:ethereum", "WETH": "coingecko:ethereum",
    "BTC": "coingecko:bitcoin", "WBTC": "coingecko:bitcoin",
    "CBBTC": "coingecko:bitcoin",
    "USDC": "coingecko:usd-coin", "USDT": "coingecko:tether",
    "DAI": "coingecko:dai", "POL": "coingecko:polygon-ecosystem-token",
    "BNB": "coingecko:binancecoin", "WSTETH": "coingecko:wrapped-steth",
    "SUSDE": "coingecko:ethena-staked-usde", "USDE": "coingecko:ethena-usde",
}


def resolve_coin(db: Session, symbol: str) -> str:
    """Ключ цены для символа: сначала ищем реальный токен из позиций, потом общий курс."""
    sym = (symbol or "").strip()
    if not sym:
        return ""
    row = db.scalar(select(TokenMeta).where(TokenMeta.symbol == sym).limit(1))
    if row is not None:
        return f"{row.chain}:{row.address.lower()}"
    return FALLBACK_COINS.get(sym.upper(), "")


def known_symbols(db: Session) -> list[str]:
    """Символы, встречающиеся в позициях — для подсказки в форме."""
    rows = db.scalars(select(TokenMeta.symbol).distinct()).all()
    extra = [s for s in ("ETH", "BTC") if s not in rows]
    return sorted({*rows, *extra})


def summarize(db: Session, prices: PriceService) -> tuple[list[dict], list[dict]]:
    """Возвращает (партии с переоценкой, сводка по активам)."""
    lots = list(db.scalars(select(TokenLot).order_by(TokenLot.acquired_at,
                                                     TokenLot.id)).all())
    if not lots:
        return [], []

    coins = sorted({lot.coin for lot in lots if lot.coin})
    now = prices.current(coins) if coins else {}

    rows: list[dict] = []
    for lot in lots:
        price = now.get(lot.coin)
        price = float(price) if price is not None else None
        cost = lot.cost_usd
        value = (lot.amount or 0) * price if price is not None else None
        rows.append({
            "lot": lot,
            "price_now": price,
            "value_now": value,
            "pnl": (value - cost) if value is not None else None,
            "pnl_pct": ((price / lot.avg_price_usd - 1) * 100
                        if price is not None and lot.avg_price_usd else None),
            "cost": cost,
        })

    # средневзвешенная цена: партия вдвое крупнее весит вдвое больше
    agg: dict[str, dict] = {}
    for r in rows:
        lot = r["lot"]
        a = agg.setdefault(lot.symbol, {"symbol": lot.symbol, "amount": 0.0, "cost": 0.0,
                                        "value": 0.0, "price_now": r["price_now"],
                                        "lots": 0, "priced": True})
        a["amount"] += lot.amount or 0
        a["cost"] += r["cost"]
        a["lots"] += 1
        if r["value_now"] is None:
            a["priced"] = False
        else:
            a["value"] += r["value_now"]
        if r["price_now"] is not None:
            a["price_now"] = r["price_now"]

    summary = []
    for a in agg.values():
        avg = a["cost"] / a["amount"] if a["amount"] else None
        a["avg_price"] = avg
        a["pnl"] = (a["value"] - a["cost"]) if a["priced"] else None
        a["pnl_pct"] = ((a["price_now"] / avg - 1) * 100
                        if a["price_now"] and avg else None)
        if not a["priced"]:
            a["value"] = None
        summary.append(a)
    summary.sort(key=lambda x: -(x["cost"] or 0))
    return rows, summary
