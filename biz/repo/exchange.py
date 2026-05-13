from abc import ABC, abstractmethod

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
    ) -> None:
        """Submit a limit order. Raises on network error."""

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
