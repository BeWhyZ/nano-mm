from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class Side(Enum):
    BID = "bid"
    ASK = "ask"


class PriceLevel(NamedTuple):
    price: float
    qty: float


@dataclass(slots=True)
class OrderBookSnapshot:
    symbol: str
    venue: str
    bids: list[PriceLevel]  # price descending
    asks: list[PriceLevel]  # price ascending
    event_ts: int           # exchange event time, ms
    send_ts: int            # exchange send time, ms (0 if unavailable)
    recv_ts: int            # local monotonic_ns at socket recv
    seq: int                # last applied update id (-1 = not yet synced)

    @property
    def is_fresh(self) -> bool:
        return self.seq >= 0

    @property
    def best_bid(self) -> PriceLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> PriceLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        if b and a:
            return (b.price + a.price) / 2.0
        return None

    def micro_price(self, k: int = 5) -> float | None:
        """Volume-weighted mid using top-k levels on each side."""
        bids = self.bids[:k]
        asks = self.asks[:k]
        if not bids or not asks:
            return None
        bid_vol = sum(p.qty for p in bids)
        ask_vol = sum(p.qty for p in asks)
        total = bid_vol + ask_vol
        if total == 0.0:
            return self.mid_price
        return (bids[0].price * ask_vol + asks[0].price * bid_vol) / total

    def spread(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        if b and a:
            return a.price - b.price
        return None

    def age_ms(self) -> float:
        """Milliseconds since local receive time."""
        return (time.monotonic_ns() - self.recv_ts) / 1e6
