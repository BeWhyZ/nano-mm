from __future__ import annotations

import time
from dataclasses import dataclass

from biz.domain.order import OrderSide


@dataclass(slots=True)
class TradeTick:
    """A single public market trade (aggTrade).

    `side` is the aggressor side (BUY = taker bought from resting ask;
    SELL = taker sold into resting bid).
    """
    symbol: str
    venue: str
    price: float
    qty: float
    side: OrderSide
    event_ts: int   # exchange event time, ms
    recv_ts: int    # local monotonic_ns at socket recv

    def age_ms(self) -> float:
        return (time.monotonic_ns() - self.recv_ts) / 1e6
