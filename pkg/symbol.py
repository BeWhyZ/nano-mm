"""
Internal symbol format: BASE_QUOTE  (e.g. BTC_USDT, ETH_USDT).

Each venue exposes two converters:
  into_external(symbol)  — internal → exchange wire format
  from_external(symbol)  — exchange wire format → internal

Both functions are idempotent on already-correct input.
"""
from __future__ import annotations

# Known spot quote assets, longest first so matching is unambiguous.
# e.g. USDT must be tried before USD to avoid mis-splitting BTCUSDT as BTC + USD + T.
_SPOT_QUOTES = sorted(
    [
        "USDT", "USDC", "BUSD", "TUSD", "FDUSD",
        "BTC", "ETH", "BNB",
        "DAI", "EUR", "GBP", "TRY", "AUD", "BRL",
    ],
    key=len,
    reverse=True,
)


def _strip_sep(symbol: str) -> str:
    """BTC_USDT → BTCUSDT"""
    return symbol.replace("_", "").upper()


def _add_sep(symbol: str) -> str:
    """BTCUSDT → BTC_USDT  (tries known quote suffixes, longest first)."""
    symbol = symbol.upper()
    for quote in _SPOT_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}_{quote}"
    raise ValueError(f"cannot parse external symbol: {symbol!r}")


# ---------------------------------------------------------------------------
# Binance Spot: no separator, uppercase  (BTCUSDT)
# ---------------------------------------------------------------------------

def binance_spot_into_external(symbol: str) -> str:
    return _strip_sep(symbol)


def binance_spot_from_external(symbol: str) -> str:
    return _add_sep(symbol)


# ---------------------------------------------------------------------------
# Bybit Spot V5: same wire format as Binance  (BTCUSDT)
# ---------------------------------------------------------------------------

def bybit_spot_into_external(symbol: str) -> str:
    return _strip_sep(symbol)


def bybit_spot_from_external(symbol: str) -> str:
    return _add_sep(symbol)
