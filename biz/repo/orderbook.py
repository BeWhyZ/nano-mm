from abc import ABC, abstractmethod

from biz.domain.book import OrderBookSnapshot


class OrderBookRepo(ABC):

    @abstractmethod
    def snapshot(self, k: int = 20) -> OrderBookSnapshot:
        """Return top-k levels on each side. Raises if OB is not yet synced."""

    @abstractmethod
    def is_fresh(self, max_age_ms: float = 500.0) -> bool:
        """True if OB is synced and last update is within max_age_ms."""

    @abstractmethod
    def seq(self) -> int:
        """Last applied update id. -1 if not yet synced."""
