"""Structured logging with async background writes and daily file rotation.

Usage
-----
Startup (once):
    from pkg.logger import configure, get_logger
    configure(level="INFO", log_dir="log")

Per-service:
    class MyService:
        def __init__(self, lg: structlog.stdlib.BoundLogger) -> None:
            self.lg = lg.bind(component="my_service")

        async def run(self) -> None:
            self.lg.info("started", symbol="BTCUSDT")

Shutdown (once):
    from pkg.logger import stop
    stop()
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
from pathlib import Path
from typing import Any

import structlog

_queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
_listener: logging.handlers.QueueListener | None = None


class _RawQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that enqueues the raw LogRecord without pre-formatting.

    The default QueueHandler.prepare() calls self.format(record), which converts
    record.msg from a structlog event dict to a plain string before the
    ProcessorFormatter in the listener thread can unpack it.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record

def _capture_exc_info(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    import sys

    if event_dict.get("exc_info") is True:
        # Resolve eagerly in the calling thread; by the time the QueueListener
        # thread runs ExceptionRenderer, sys.exc_info() is already empty.
        event_dict["exc_info"] = sys.exc_info()
    return event_dict


_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    _capture_exc_info,
]


def configure(
    level: str = "INFO",
    log_dir: str | Path = "logs",
) -> None:
    """Wire up structlog → stdlib → QueueHandler → QueueListener → [file, console].

    The caller thread only enqueues; the background listener thread owns all I/O.
    File output: JSON Lines, rotated at UTC midnight, kept 30 days.
    Console output: coloured dev format.
    """
    global _listener

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_level = getattr(logging, level.upper())

    file_handler = _make_file_handler(log_dir, log_level)
    console_handler = _make_console_handler(log_level)

    _listener = logging.handlers.QueueListener(
        _queue,
        file_handler,
        console_handler,
        respect_handler_level=True,
    )
    _listener.start()

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_RawQueueHandler(_queue))
    root.setLevel(log_level)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def stop() -> None:
    """Flush the queue and stop the background thread. Call once on graceful shutdown."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


def _make_file_handler(
    log_dir: Path, level: int
) -> logging.handlers.TimedRotatingFileHandler:
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_dir / "nano-mm.jsonl",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    handler.namer = _rotated_name
    handler.setLevel(level)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.ExceptionRenderer(),
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
    )
    return handler


def _make_console_handler(level: int) -> logging.StreamHandler:  # type: ignore[type-arg]
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(),
            ],
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
    )
    return handler


def _rotated_name(default_name: str) -> str:
    """Rename  'nano-mm.jsonl.2026-05-12'  →  'nano-mm.2026-05-12.jsonl'."""
    # default_name ends with  .<date-suffix>  where date has no dots
    stem, date_suffix = default_name.rsplit(".", 1)
    base = stem.removesuffix(".jsonl")
    return f"{base}.{date_suffix}.jsonl"
