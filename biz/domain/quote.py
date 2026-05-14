from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from biz.domain.order import OrderSide


@dataclass(frozen=True, slots=True)
class Quote:
    side: OrderSide   # BUY = bid quote, SELL = ask quote
    price: float
    size: float


class QuoteState(NamedTuple):
    symbol: str
    venue: str
    mid: float
    bid: Quote | None       # None when calibration not ready or hard cap stopped this side
    ask: Quote | None
    sigma: float            # price · sec^(-1/2)
    A: float                # trades · sec^(-1)
    k: float                # 1 / price
    gamma: float
    q_norm: float
    ts_ns: int
