"""
LocalOrderBook: in-process sorted price level store.
Thread-safe for single asyncio event loop (no lock needed if all ops in same loop).
"""
from __future__ import annotations

import time
from typing import Callable

from sortedcontainers import SortedList

from biz.domain.book import OrderBookSnapshot, PriceLevel


class LocalOrderBook:
    """
    Maintains a full local copy of an order book.
    apply_snapshot / apply_diff are the only mutation points.
    seq == -1 means not yet synced; consumers must check is_fresh() first.
    """

    def __init__(self, symbol: str, venue: str) -> None:
        self.symbol = symbol
        self.venue = venue
        # bids: sorted descending by price → store as (-price, qty) tuples
        self._bids: SortedList[tuple[float, float]] = SortedList(key=lambda x: x[0])
        # asks: sorted ascending by price
        self._asks: SortedList[tuple[float, float]] = SortedList(key=lambda x: x[0])
        self._bid_map: dict[float, float] = {}   # price → qty
        self._ask_map: dict[float, float] = {}
        self._seq: int = -1
        self._event_ts: int = 0
        self._send_ts: int = 0
        self._recv_ts: int = 0
        self._dirty: bool = True  # True until first valid snapshot applied

    # ------------------------------------------------------------------
    # Public mutation API
    # ------------------------------------------------------------------

    def apply_snapshot(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        seq: int,
        event_ts: int,
        recv_ts: int,
        send_ts: int = 0,
    ) -> None:
        self._bid_map.clear()
        self._ask_map.clear()
        self._bids.clear()
        self._asks.clear()

        for price, qty in bids:
            if qty > 0:
                self._bid_map[price] = qty
                self._bids.add((-price, qty))  # negated for descending sort

        for price, qty in asks:
            if qty > 0:
                self._ask_map[price] = qty
                self._asks.add((price, qty))

        self._seq = seq
        self._event_ts = event_ts
        self._send_ts = send_ts
        self._recv_ts = recv_ts
        self._dirty = False

    def apply_diff(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        seq: int,
        event_ts: int,
        recv_ts: int,
        send_ts: int = 0,
    ) -> None:
        """Apply a diff. Caller is responsible for seq gap detection."""
        if self._dirty:
            return  # discard until snapshot is applied

        self._apply_side(bids, self._bid_map, self._bids, negate=True)
        self._apply_side(asks, self._ask_map, self._asks, negate=False)

        self._seq = seq
        self._event_ts = event_ts
        self._send_ts = send_ts
        self._recv_ts = recv_ts

    def mark_dirty(self) -> None:
        """Force OB into unsynced state (e.g. after reconnect / seq gap)."""
        self._dirty = True
        self._seq = -1

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._seq

    def is_fresh(self, max_age_ms: float = 500.0) -> bool:
        if self._dirty or self._seq < 0:
            return False
        age = (time.monotonic_ns() - self._recv_ts) / 1e6
        return age <= max_age_ms

    def snapshot(self, k: int = 20) -> OrderBookSnapshot:
        bids = [PriceLevel(-neg_p, q) for neg_p, q in self._bids[:k]]
        asks = [PriceLevel(p, q) for p, q in self._asks[:k]]
        return OrderBookSnapshot(
            symbol=self.symbol,
            venue=self.venue,
            bids=bids,
            asks=asks,
            event_ts=self._event_ts,
            send_ts=self._send_ts,
            recv_ts=self._recv_ts,
            seq=self._seq,
        )

    def best_bid(self) -> tuple[float, float] | None:
        if not self._bids:
            return None
        neg_p, q = self._bids[0]
        return (-neg_p, q)

    def best_ask(self) -> tuple[float, float] | None:
        if not self._asks:
            return None
        return self._asks[0]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_side(
        self,
        updates: list[tuple[float, float]],
        price_map: dict[float, float],
        sorted_list: SortedList,
        negate: bool,
    ) -> None:
        for price, qty in updates:
            stored_key = -price if negate else price
            old_qty = price_map.get(price)
            if old_qty is not None:
                sorted_list.discard((stored_key, old_qty))

            if qty == 0.0:
                price_map.pop(price, None)
            else:
                price_map[price] = qty
                sorted_list.add((stored_key, qty))
