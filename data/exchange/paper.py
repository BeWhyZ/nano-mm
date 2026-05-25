"""
PaperExchange: in-process simulated venue implementing ExchangeRepo.

Semantics:
  - All orders are LIMIT_MAKER (post-only).  If a submitted bid would cross the
    current best ask (or a submitted ask would cross the best bid) the order is
    rejected synchronously via the on_reject callback.
  - ACKs, reject notifications, and cancel ACKs are delivered synchronously via
    the callbacks passed to __init__.  The executor wires these directly to an
    OrderTracker instance.
  - ACK callbacks fire *before* submit_limit returns, so the executor's on_ack
    handler (which registers the order with FillSimulator) is always called before
    the coroutine that submits moves on.
  - get_position always returns 0.0; live inventory is tracked by OrderTracker.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable

import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Order, OrderSide, OrderStatus, OrderType
from biz.repo.exchange import ExchangeRepo


class PaperExchange(ExchangeRepo):

    def __init__(
        self,
        symbol: str,
        venue: str,
        on_ack: Callable[[str, str], None],
        on_reject: Callable[[str, str], None],
        on_cancel_ack: Callable[[str, float], None],
        lg: structlog.stdlib.BoundLogger,
    ) -> None:
        self._symbol = symbol
        self._venue = venue
        self._on_ack = on_ack
        self._on_reject = on_reject
        self._on_cancel_ack = on_cancel_ack
        self.lg = lg.bind(component="paper_exchange", symbol=symbol)

        # coid → remaining_qty (paper-side mirror; updated by notify_fill)
        self._remaining: dict[str, float] = {}
        self._exid_seq = itertools.count(1)

        self._latest_snap: OrderBookSnapshot | None = None

    # ------------------------------------------------------------------
    # Book feed (called by executor to keep the post-only check current)
    # ------------------------------------------------------------------

    def set_book(self, snap: OrderBookSnapshot) -> None:
        self._latest_snap = snap

    # ------------------------------------------------------------------
    # ExchangeRepo
    # ------------------------------------------------------------------

    async def submit_limit(
        self,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        post_only: bool = True,
        snap: OrderBookSnapshot | None = None,
    ) -> None:
        book = snap if snap is not None else self._latest_snap

        # Post-only cross check.
        if book is not None:
            if side == OrderSide.BUY:
                best_ask = book.best_ask
                if best_ask is not None and price >= best_ask.price:
                    self.lg.warning(
                        "paper_reject_cross",
                        coid=client_order_id,
                        side="buy",
                        price=price,
                        best_ask=best_ask.price,
                    )
                    self._on_reject(client_order_id, "post_only_cross")
                    return
            else:
                best_bid = book.best_bid
                if best_bid is not None and price <= best_bid.price:
                    self.lg.warning(
                        "paper_reject_cross",
                        coid=client_order_id,
                        side="sell",
                        price=price,
                        best_bid=best_bid.price,
                    )
                    self._on_reject(client_order_id, "post_only_cross")
                    return

        exid = f"PAPER-{next(self._exid_seq):09d}"
        self._remaining[client_order_id] = qty
        self.lg.debug("paper_ack", coid=client_order_id, exid=exid, side=side.value, price=price, qty=qty)
        self._on_ack(client_order_id, exid)

    async def cancel_order(self, client_order_id: str, symbol: str) -> None:
        remaining = self._remaining.pop(client_order_id, None)
        if remaining is None:
            self.lg.debug("paper_cancel_unknown", coid=client_order_id)
            return
        self.lg.debug("paper_cancel_ack", coid=client_order_id, remaining=remaining)
        self._on_cancel_ack(client_order_id, remaining)

    async def cancel_all(self, symbol: str) -> None:
        for coid in list(self._remaining.keys()):
            await self.cancel_order(coid, symbol)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return []

    async def get_position(self, symbol: str) -> float:
        return 0.0

    # ------------------------------------------------------------------
    # Fill notification (called by executor after FillSimulator fires)
    # ------------------------------------------------------------------

    def notify_fill(self, client_order_id: str, fill_qty: float) -> None:
        """Update internal remaining qty after a simulated fill.  Pops if fully filled."""
        remaining = self._remaining.get(client_order_id)
        if remaining is None:
            return
        new_remaining = max(0.0, remaining - fill_qty)
        if new_remaining <= 1e-12:
            self._remaining.pop(client_order_id, None)
        else:
            self._remaining[client_order_id] = new_remaining
