"""
Simple in-process counter/gauge registry.
Swap this out for prometheus_client when ready to expose /metrics.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import DefaultDict


_lock = threading.Lock()
_counters: DefaultDict[str, float] = defaultdict(float)
_gauges: DefaultDict[str, float] = defaultdict(float)


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = _fmt(name, labels)
    with _lock:
        _counters[key] += value


def gauge(name: str, value: float, **labels: str) -> None:
    key = _fmt(name, labels)
    with _lock:
        _gauges[key] = value


def snapshot() -> dict[str, float]:
    with _lock:
        return {**_counters, **_gauges}


def _fmt(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    pairs = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{pairs}}}"
