"""Конфигурация сетей: RPC-эндпоинты, адреса контрактов, ключи DefiLlama.

Перенесено из uniswap_positions.py и дополнено адресами резолверов Fluid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Один и тот же адрес во всех сетях (детерминированный деплой)
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Резолверы Fluid. Адреса совпадают во всех сетях, где протокол развёрнут —
# проверено сверкой байткода через eth_getCode на каждой сети.
FLUID_VAULT_RESOLVER = "0xA5C3E16523eeeDDcC34706b0E6bE88b4c6EA95cC"
FLUID_LENDING_RESOLVER = "0x48D32f49aFeAEC7AE66ad7B9264f446fc11a1569"

# Псевдоадрес нативной монеты в контрактах Fluid
NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


@dataclass
class Chain:
    key: str
    name: str
    chain_id: int
    npm: str                  # Uniswap NonfungiblePositionManager
    factory: str              # UniswapV3Factory
    rpcs: list[str]
    llama: str                # ключ сети в DefiLlama coins API
    explorer: str
    deploy_block: int = 0     # блок деплоя NPM — нижняя граница сканирования логов
    # ноды, отдающие eth_getLogs за широкий диапазон; для логов опрашиваются первыми
    log_rpcs: list[str] = field(default_factory=list)
    has_fluid: bool = False
    native_symbol: str = "ETH"
    # обёрнутая нативная монета — по ней берём цену нативного токена в DefiLlama
    wrapped_native: str = ""


CHAINS: dict[str, Chain] = {
    "ethereum": Chain(
        key="ethereum", name="Ethereum", chain_id=1,
        npm="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        rpcs=[
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
            "https://eth.drpc.org",
            "https://rpc.ankr.com/eth",
            "https://cloudflare-eth.com",
            "https://1rpc.io/eth",
        ],
        llama="ethereum", explorer="https://etherscan.io", deploy_block=12369621,
        log_rpcs=["https://rpc.mevblocker.io", "https://gateway.tenderly.co/public/mainnet",
                  "https://eth.api.onfinality.io/public"],
        has_fluid=True, native_symbol="ETH",
        wrapped_native="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    ),
    "arbitrum": Chain(
        key="arbitrum", name="Arbitrum One", chain_id=42161,
        npm="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        rpcs=[
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.llamarpc.com",
            "https://arbitrum.drpc.org",
        ],
        llama="arbitrum", explorer="https://arbiscan.io", deploy_block=165,
        log_rpcs=["https://arb1.arbitrum.io/rpc"],
        has_fluid=True, native_symbol="ETH",
        wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    ),
    "optimism": Chain(
        key="optimism", name="OP Mainnet", chain_id=10,
        npm="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        rpcs=[
            "https://optimism-rpc.publicnode.com",
            "https://mainnet.optimism.io",
            "https://optimism.llamarpc.com",
            "https://optimism.drpc.org",
        ],
        llama="optimism", explorer="https://optimistic.etherscan.io", deploy_block=0,
        log_rpcs=["https://optimism.gateway.tenderly.co"],
        has_fluid=False, native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
    ),
    "polygon": Chain(
        key="polygon", name="Polygon PoS", chain_id=137,
        npm="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        rpcs=[
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-rpc.com",
            "https://polygon.llamarpc.com",
            "https://polygon.drpc.org",
        ],
        llama="polygon", explorer="https://polygonscan.com", deploy_block=22757547,
        log_rpcs=["https://polygon.gateway.tenderly.co"],
        has_fluid=True, native_symbol="POL",
        wrapped_native="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    ),
    "base": Chain(
        key="base", name="Base", chain_id=8453,
        npm="0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        rpcs=[
            "https://base-rpc.publicnode.com",
            "https://mainnet.base.org",
            "https://base.llamarpc.com",
            "https://base.drpc.org",
        ],
        llama="base", explorer="https://basescan.org", deploy_block=1371680,
        log_rpcs=["https://base.gateway.tenderly.co"],
        has_fluid=True, native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
    ),
    "bsc": Chain(
        key="bsc", name="BNB Chain", chain_id=56,
        npm="0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613",
        factory="0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
        rpcs=[
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.bnbchain.org",
            "https://binance.llamarpc.com",
            "https://bsc.drpc.org",
        ],
        llama="bsc", explorer="https://bscscan.com", deploy_block=26324014,
        has_fluid=False, native_symbol="BNB",
        wrapped_native="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    ),
}

FEE_TIERS = {100: "0.01%", 500: "0.05%", 3000: "0.3%", 10000: "1%"}
MIN_TICK, MAX_TICK = -887272, 887272


def enabled_chains(keys: list[str]) -> list[Chain]:
    return [CHAINS[k] for k in keys if k in CHAINS]
