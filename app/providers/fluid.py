"""Провайдер Fluid: депозиты (fTokens) и залоговые позиции (Vaults).

Читается через периферийные резолверы протокола — по одному вызову на продукт,
вместо десятков обращений к отдельным контрактам:

  LendingResolver.getUserPositions(user) -> (FTokenDetails, UserPosition)[]
  VaultResolver.positionsByUser(user)    -> (UserPosition[], VaultEntireData[])

ABI резолверов лежат в abis/ рядом — они получены из верифицированного исходника
контракта, а не написаны руками: структура VaultEntireData слишком велика, чтобы
угадывать её на глаз.

Health factor считается по ончейн-оракулу самого Fluid, а не по ценам DefiLlama:
ликвидация произойдёт именно по нему. Долларовые оценки при этом берутся из
DefiLlama — чтобы Fluid складывался с Uniswap в один портфель по одной методике.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from web3 import Web3

from app.core.chains import (FLUID_LENDING_RESOLVER, FLUID_VAULT_RESOLVER, NATIVE_TOKEN, Chain)
from app.core.prices import coin_key
from app.db.models import PROTO_FLUID_LEND, PROTO_FLUID_VAULT
from app.providers.base import Ctx, KnownPosition, Provider, RawPosition, jsonable

log = logging.getLogger(__name__)

ABI_DIR = Path(__file__).parent / "abis"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# Оракул Fluid отдаёт цену с масштабом 1e27: она переводит сырые единицы залога
# в сырые единицы долга. Проверено сверкой с реальной позицией (vault #1: 0.109 ETH
# залога против 121.31 USDC долга дало курс ETH ≈ $1888).
ORACLE_SCALE = Decimal(10) ** 27
# Проценты в конфигах: 1% = 100, то есть 10000 = 100%
PCT_SCALE = Decimal(10000)
# Ставки в резолверах хранятся с тем же масштабом
RATE_SCALE = Decimal(100)


@lru_cache(maxsize=4)
def _abi(name: str) -> list:
    return json.loads((ABI_DIR / name).read_text())


def _addr_ok(a: str | None) -> bool:
    return bool(a) and a != ZERO_ADDR


def _tok(chain: Chain, addr: str) -> str:
    return chain.native_symbol if (addr or "").lower() == NATIVE_TOKEN.lower() else addr


# --------------------------------------------------------------------------------------
# Lending — депозиты в fTokens
# --------------------------------------------------------------------------------------

class FluidLendingProvider(Provider):
    key = PROTO_FLUID_LEND
    title = "Fluid Lending"

    def supports(self, chain: Chain) -> bool:
        return chain.has_fluid

    def fetch(self, ctx: Ctx, wallet: str, known: dict[str, KnownPosition],
              with_history: bool) -> list[RawPosition]:
        chain = ctx.chain
        try:
            rows = ctx.rpc.contract_call(FLUID_LENDING_RESOLVER, _abi("fluid_lending_resolver.json"),
                                         "getUserPositions", Web3.to_checksum_address(wallet))
        except Exception as e:  # noqa: BLE001 — сеть без Fluid или нода не отвечает
            log.warning("[%s] Fluid lending недоступен: %s", chain.name, str(e)[:120])
            return []

        out: list[RawPosition] = []
        # резолвер возвращает ВСЕ fToken'ы, в том числе с нулевым балансом
        active = [(d, u) for d, u in rows if u[1] > 0 or u[0] > 0]
        if not active:
            return []

        assets = [d[6] for d, _ in active if _addr_ok(d[6])]
        meta = ctx.tokens.resolve(assets)
        coins = [coin_key(chain, d[6]) for d, _ in active if _addr_ok(d[6])]
        now = ctx.prices.current(coins)

        for details, pos in active:
            ftoken_addr = Web3.to_checksum_address(details[0])
            asset = Web3.to_checksum_address(details[6]) if _addr_ok(details[6]) else ""
            sym, dec = meta.get(asset, (details[4] or "?", int(details[5] or 18)))
            shares, underlying = pos[0], pos[1]

            price = now.get(coin_key(chain, asset)) if asset else None
            human = Decimal(underlying) / (Decimal(10) ** dec)
            value = float(human * price) if price is not None else None

            supply_rate = Decimal(details[12] or 0) / RATE_SCALE      # % годовых
            rewards_rate = Decimal(details[11] or 0) / RATE_SCALE
            total_apr = float(supply_rate + rewards_rate)

            detail = {
                "kind": "fluid_lending",
                "ftoken": ftoken_addr,
                "ftoken_symbol": details[4],
                "asset": {"address": asset, "symbol": sym, "decimals": dec},
                "shares": str(shares),
                "underlying": str(underlying),
                "underlying_human": float(human),
                "price_usd": float(price) if price is not None else None,
                "supply_rate": float(supply_rate),
                "rewards_rate": float(rewards_rate),
                "wallet_balance": str(pos[2]),
                "explorer_ftoken": f"{chain.explorer}/address/{ftoken_addr}",
            }

            out.append(RawPosition(
                protocol=PROTO_FLUID_LEND, chain=chain.key, external_id=ftoken_addr,
                title=f"{sym} депозит",
                subtitle=f"{details[4]} · {total_apr:.2f}% годовых",
                is_open=underlying > 0,
                value_usd=value, net_usd=value, apr=total_apr,
                detail=jsonable(detail),
                history_complete=True,   # у депозита нет истории событий для сканирования
            ))
        return out


# --------------------------------------------------------------------------------------
# Vaults — залог и долг
# --------------------------------------------------------------------------------------

class FluidVaultProvider(Provider):
    key = PROTO_FLUID_VAULT
    title = "Fluid Vault"

    def supports(self, chain: Chain) -> bool:
        return chain.has_fluid

    def fetch(self, ctx: Ctx, wallet: str, known: dict[str, KnownPosition],
              with_history: bool) -> list[RawPosition]:
        chain = ctx.chain
        try:
            positions, vaults = ctx.rpc.contract_call(
                FLUID_VAULT_RESOLVER, _abi("fluid_vault_resolver.json"),
                "positionsByUser", Web3.to_checksum_address(wallet))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] Fluid vaults недоступны: %s", chain.name, str(e)[:120])
            return []
        if not positions:
            return []

        # собираем адреса всех задействованных токенов одним махом
        addrs: list[str] = []
        for v in vaults:
            cv = v[3]
            for tup in (cv[8], cv[9]):
                addrs += [a for a in tup if _addr_ok(a)]
        meta = ctx.tokens.resolve(addrs)
        now = ctx.prices.current([coin_key(chain, a) for a in set(addrs)])

        out: list[RawPosition] = []
        for pos, vault in zip(positions, vaults):
            rp = self._one(ctx, pos, vault, meta, now)
            if rp is not None:
                out.append(rp)
        return out

    def _one(self, ctx: Ctx, pos, vault, meta, now) -> RawPosition | None:
        chain = ctx.chain
        (nft_id, owner, is_liquidated, is_supply_only, tick, _tick_id,
         _bsup, _bbor, _bdust, supply, borrow, dust_borrow) = pos
        vault_addr = Web3.to_checksum_address(vault[0])
        is_smart_col, is_smart_debt = bool(vault[1]), bool(vault[2])
        cv, cfg, rates = vault[3], vault[4], vault[5]

        col_t0, col_t1 = cv[8]
        deb_t0, deb_t1 = cv[9]
        collateral_factor = Decimal(cfg[2]) / PCT_SCALE
        liq_threshold = Decimal(cfg[3]) / PCT_SCALE
        liq_penalty = Decimal(cfg[6]) / PCT_SCALE
        oracle_operate = Decimal(cfg[9] or 0)
        oracle_liquidate = Decimal(cfg[10] or 0)

        if supply == 0 and borrow == 0:
            return None

        col_addr = Web3.to_checksum_address(col_t0) if _addr_ok(col_t0) else ""
        deb_addr = Web3.to_checksum_address(deb_t0) if _addr_ok(deb_t0) else ""
        col_sym, col_dec = meta.get(col_addr, ("?", 18))
        deb_sym, deb_dec = meta.get(deb_addr, ("?", 18))
        if (col_t0 or "").lower() == NATIVE_TOKEN.lower():
            col_sym, col_dec = chain.native_symbol, 18
        if (deb_t0 or "").lower() == NATIVE_TOKEN.lower():
            deb_sym, deb_dec = chain.native_symbol, 18

        col_price = now.get(coin_key(chain, col_addr)) if col_addr else None
        deb_price = now.get(coin_key(chain, deb_addr)) if deb_addr else None

        col_human = Decimal(supply) / (Decimal(10) ** col_dec)
        deb_human = Decimal(borrow) / (Decimal(10) ** deb_dec)

        # Smart collateral / smart debt хранят позицию в ДОЛЯХ DEX-пула Fluid, а не в
        # токене: supply/borrow там — не количество монет, и делить их на decimals
        # токена бессмысленно (получаются триллионы cbBTC). Пересчёт долей в токены
        # требует DexResolver, которого в этой версии нет.
        smart = is_smart_col or is_smart_debt
        col_usd = float(col_human * col_price) if (col_price is not None and not is_smart_col) else None
        deb_usd = float(deb_human * deb_price) if (deb_price is not None and not is_smart_debt) else None

        # ── риск по оракулу Fluid: ликвидация произойдёт именно по нему.
        # Для smart-позиций формула неприменима — оракул там связывает доли пулов, а
        # не токены, и наивный расчёт даёт заведомо ложный результат (проверено: живая
        # здоровая позиция показывала HF 0.92, то есть «уже подлежит ликвидации»).
        # Лучше не показать метрику, чем показать выдуманную.
        health = ltv = liq_price = cur_price = None
        if not smart:
            if supply > 0 and oracle_operate > 0:
                # oraclePrice переводит сырые единицы залога в сырые единицы долга
                col_in_debt_raw = Decimal(supply) * oracle_operate / ORACLE_SCALE
                if col_in_debt_raw > 0:
                    ltv = float(Decimal(borrow) / col_in_debt_raw)
                    # без долга ликвидировать нечего — health factor не определён
                    if borrow > 0 and ltv > 0 and liq_threshold > 0:
                        health = float(liq_threshold / Decimal(str(ltv)))
                        # цена залога (в токене долга), при которой LTV упрётся в порог
                        raw_liq = Decimal(borrow) / (Decimal(supply) * liq_threshold)
                        liq_price = float(raw_liq * (Decimal(10) ** (col_dec - deb_dec)))
            if oracle_operate > 0:
                cur_price = float(oracle_operate / ORACLE_SCALE
                                  * (Decimal(10) ** (col_dec - deb_dec)))

        net = None
        if col_usd is not None:
            net = col_usd - (deb_usd or 0.0)

        supply_rate = Decimal(rates[10] or 0) / RATE_SCALE      # supplyRateVault
        borrow_rate = Decimal(rates[11] or 0) / RATE_SCALE      # borrowRateVault

        # Годовые считаем на СОБСТВЕННЫЙ капитал, а не на залог: заёмные средства
        # усиливают и доход, и убыток, и без этого число вводит в заблуждение.
        net_apr = None
        if col_usd is not None and net and net > 0:
            net_apr = float((supply_rate * Decimal(str(col_usd))
                             - borrow_rate * Decimal(str(deb_usd or 0))) / Decimal(str(net)))
        elif borrow == 0:
            net_apr = float(supply_rate)

        detail = {
            "kind": "fluid_vault",
            "vault": vault_addr,
            "vault_id": int(cv[10]),
            "vault_type": int(cv[11]),
            "nft_id": int(nft_id),
            "owner": Web3.to_checksum_address(owner),
            "is_liquidated": bool(is_liquidated),
            "is_supply_only": bool(is_supply_only),
            "is_smart_col": is_smart_col,
            "is_smart_debt": is_smart_debt,
            "smart_warning": smart,
            "tick": int(tick),
            # human заполняется только там, где величина действительно в токенах:
            # у smart-позиции это доли пула, и «6819693023371 cbBTC» ввело бы в заблуждение
            "collateral": {"address": col_addr, "symbol": col_sym, "decimals": col_dec,
                           "raw": str(supply),
                           "human": None if is_smart_col else float(col_human),
                           "is_shares": is_smart_col,
                           "price_usd": float(col_price) if col_price is not None else None,
                           "usd": col_usd,
                           "token1": Web3.to_checksum_address(col_t1) if _addr_ok(col_t1) else ""},
            "debt": {"address": deb_addr, "symbol": deb_sym, "decimals": deb_dec,
                     "raw": str(borrow),
                     "human": None if is_smart_debt else float(deb_human),
                     "is_shares": is_smart_debt,
                     "price_usd": float(deb_price) if deb_price is not None else None,
                     "usd": deb_usd, "dust": str(dust_borrow),
                     "token1": Web3.to_checksum_address(deb_t1) if _addr_ok(deb_t1) else ""},
            "risk": {
                "unavailable": smart,
                "health_factor": health,
                "ltv": ltv,
                "ltv_pct": ltv * 100 if ltv is not None else None,
                "collateral_factor": float(collateral_factor * 100),
                "liquidation_threshold": float(liq_threshold * 100),
                "liquidation_penalty": float(liq_penalty * 100),
                "liquidation_price": liq_price,
                "current_price": cur_price,
                "price_unit": f"{deb_sym} за 1 {col_sym}",
                "drop_to_liquidation_pct": (
                    (liq_price / cur_price - 1) * 100
                    if (liq_price and cur_price and cur_price > 0) else None),
            },
            "rates": {"supply": float(supply_rate), "borrow": float(borrow_rate),
                      "net_on_equity": net_apr},
            "explorer_vault": f"{chain.explorer}/address/{vault_addr}",
        }

        subtitle = f"залог {col_sym}"
        if borrow > 0:
            subtitle += f" → долг {deb_sym}"
        # в одном сейфе может быть несколько позиций — без номера NFT они
        # выглядят в списке как дубликаты
        subtitle += f" · NFT {int(nft_id)}"
        if smart:
            subtitle += " · smart"

        return RawPosition(
            protocol=PROTO_FLUID_VAULT, chain=chain.key, external_id=str(int(nft_id)),
            title=f"{col_sym}/{deb_sym} vault #{int(cv[10])}" if borrow > 0
                  else f"{col_sym} vault #{int(cv[10])}",
            subtitle=subtitle,
            is_open=supply > 0 or borrow > 0,
            value_usd=col_usd, debt_usd=deb_usd, net_usd=net,
            health_factor=health, ltv=ltv * 100 if ltv is not None else None,
            apr=net_apr,
            detail=jsonable(detail),
            history_complete=True,   # состояние читается целиком, логи не нужны
        )
