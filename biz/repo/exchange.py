from __future__ import annotations

from abc import ABC, abstractmethod

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Order, OrderSide, OrderType


class ExchangeRepo(ABC):

    @abstractmethod
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
        """Submit a limit order. Raises on network error.

        snap: the book snapshot used to compute this quote (paper impl uses it
        for the post-only cross-check; live impls ignore it — venue enforces server-side).
        """

    @abstractmethod
    async def cancel_order(self, client_order_id: str, symbol: str) -> None:
        """Request cancel. Does not guarantee fill-race; OMS handles ghost fills."""

    @abstractmethod
    async def cancel_all(self, symbol: str) -> None:
        """Cancel all open orders for symbol."""

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[Order]:
        """REST fetch open orders for reconciliation on startup."""

    @abstractmethod
    async def get_position(self, symbol: str) -> float:
        """Net position (base asset qty). Positive = long."""
