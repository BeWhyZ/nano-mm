"""
FairValueEngine: compute mid / micro-price / spread / OBI from L2 snapshot.

Plug on_tick into BinanceSpotOrderBookTracker's on_update parameter.
"""
from __future__ import annotations

from typing import NamedTuple

import structlog

from biz.domain.book import OrderBookSnapshot


class FairPriceState(NamedTuple):
    symbol: str
    venue: str
    mid: float
    micro: float        # volume-weighted imbalance mid (Stoikov)
    spread_bps: float
    obi: float          # order-book imbalance: (bid_vol-ask_vol)/(bid_vol+ask_vol), [-1,1]
    ob_age_ms: float


class FairValueEngine:
    """
    Stateless per-symbol fair-value estimator.

    on_tick() is safe to call from an asyncio callback; it does no I/O.
    state is None until the first valid snapshot arrives.
    """

    def __init__(self, symbol: str, lg: structlog.stdlib.BoundLogger, micro_k: int = 5) -> None:
        self._symbol = symbol.upper()
        self.lg = lg.bind(symbol=self._symbol)
        self._micro_k = micro_k
        self._state: FairPriceState | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_tick(self, snap: OrderBookSnapshot) -> None:
        if not snap.is_fresh:
            return

        mid = snap.mid_price
        micro = snap.micro_price(self._micro_k)
        spread = snap.spread()
        if mid is None or micro is None or spread is None:
            return

        bids = snap.bids[: self._micro_k]
        asks = snap.asks[: self._micro_k]
        bid_vol = sum(p.qty for p in bids)
        ask_vol = sum(p.qty for p in asks)
        total_vol = bid_vol + ask_vol
        obi = (bid_vol - ask_vol) / total_vol if total_vol > 0.0 else 0.0

        spread_bps = spread / mid * 1e4

        state = FairPriceState(
            symbol=snap.symbol,
            venue=snap.venue,
            mid=mid,
            micro=micro,
            spread_bps=spread_bps,
            obi=obi,
            ob_age_ms=snap.age_ms(),
        )
        self._state = state

        self.lg.debug(
            "fair_value",
            mid=round(mid, 4),
            micro=round(micro, 4),
            spread_bps=round(spread_bps, 3),
            obi=round(obi, 4),
            ob_age_ms=round(state.ob_age_ms, 2),
        )

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def state(self) -> FairPriceState | None:
        return self._state
