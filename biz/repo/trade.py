from abc import ABC, abstractmethod


class TradeStreamRepo(ABC):

    @abstractmethod
    async def run(self) -> None:
        """Connect, stream trades to the registered callback, reconnect on error."""

    @abstractmethod
    def stop(self) -> None:
        """Signal the run loop to exit gracefully."""
