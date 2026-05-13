from abc import ABC, abstractmethod

from biz.domain.order import Fill, Order, OrderStatus


class OrderTrackerRepo(ABC):

    @abstractmethod
    def add(self, order: Order) -> None:
        """Register a new order (status=PENDING_NEW)."""

    @abstractmethod
    def on_ack(self, client_order_id: str, exchange_order_id: str) -> Order:
        """PENDING_NEW → OPEN."""

    @abstractmethod
    def on_reject(self, client_order_id: str, reason: str) -> Order:
        """PENDING_NEW → REJECTED."""

    @abstractmethod
    def on_fill(self, fill: Fill) -> Order:
        """Apply a fill; handles ghost fills (PENDING_CANCEL + fill is legal)."""

    @abstractmethod
    def on_cancel_ack(self, client_order_id: str, canceled_qty: float) -> Order:
        """PENDING_CANCEL → CANCELED."""

    @abstractmethod
    def on_cancel_reject(self, client_order_id: str, reason: str) -> Order:
        """Cancel rejected (e.g. already filled); revert to active state."""

    @abstractmethod
    def on_expired(self, client_order_id: str) -> Order:
        """TIF expired."""

    @abstractmethod
    def mark_pending_cancel(self, client_order_id: str) -> Order:
        """Record that a cancel request was submitted."""

    @abstractmethod
    def mark_failed(self, client_order_id: str) -> Order:
        """Network/timeout: mark for REST reconcile."""

    @abstractmethod
    def get(self, client_order_id: str) -> Order | None:
        """Lookup by client_order_id."""

    @abstractmethod
    def get_by_exchange_id(self, exchange_order_id: str) -> Order | None: ...

    @abstractmethod
    def active_orders(self) -> list[Order]:
        """All orders with is_active status."""

    @abstractmethod
    def inventory(self) -> float:
        """Net inventory (base asset). Updated on every fill."""
