from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp


class Exchange(StrEnum):
    BINANCE_SPOT = "binance_spot"
    BYBIT_SPOT = "bybit_spot"


@dataclass(frozen=True)
class ExchangeApi:
    rest: str
    ws: str
    rest_testnet: str
    ws_testnet: str


# https://developers.binance.com/docs/binance-spot-api-docs
BINANCE_SPOT = ExchangeApi(
    rest="https://api.binance.com",
    ws="wss://stream.binance.com:9443/stream",
    rest_testnet="https://testnet.binance.vision",
    ws_testnet="wss://testnet.binance.vision/stream",
)

# https://bybit-exchange.github.io/docs/v5/guide
BYBIT_SPOT = ExchangeApi(
    rest="https://api.bybit.com",
    ws="wss://stream.bybit.com/v5/public/spot",
    rest_testnet="https://api-testnet.bybit.com",
    ws_testnet="wss://stream-testnet.bybit.com/v5/public/spot",
)

EXCHANGE_API: dict[Exchange, ExchangeApi] = {
    Exchange.BINANCE_SPOT: BINANCE_SPOT,
    Exchange.BYBIT_SPOT: BYBIT_SPOT,
}


async def get(
    session: aiohttp.ClientSession,
    base_url: str,
    path: str,
    params: dict[str, Any] | None = None,
    proxy: str | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    async with session.get(url, params=params, proxy=proxy) as resp:
        resp.raise_for_status()
        return await resp.json()
