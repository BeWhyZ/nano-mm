#!/usr/bin/env python3
"""
Watch Binance Spot L2 orderbook and print fair-value metrics on each tick.

Usage:
    uv run python -m cmd.watch_book [SYMBOL]
    uv run python -m cmd.watch_book ETH_USDT
"""
from __future__ import annotations

import asyncio
import sys

import aiohttp

from biz.usecase.fair_value import FairPriceState
from pkg import logger
from server.fair_value_server import FairValueServer


def _print_state(s: FairPriceState) -> None:
    bar_len = 20
    pos = int((s.obi + 1.0) / 2.0 * bar_len)
    bar = "[" + "-" * pos + "|" + "-" * (bar_len - pos) + "]"
    print(
        f"\r{s.symbol:<10} "
        f"mid={s.mid:>10.4f}  "
        f"micro={s.micro:>10.4f}  "
        f"spread={s.spread_bps:>6.3f}bps  "
        f"OBI={s.obi:>+.3f} {bar}  "
        f"age={s.ob_age_ms:>5.1f}ms    ",
        end="",
        flush=True,
    )


async def main(symbol: str) -> None:
    logger.configure(level="WARNING")
    lg = logger.get_logger("watch_book")
    print(f"Watching {symbol} — Ctrl-C to stop")
    async with aiohttp.ClientSession() as session:
        server = FairValueServer(symbol, session, on_state=_print_state, lg=lg)
        try:
            await server.run()
        except asyncio.CancelledError:
            pass
        finally:
            print()


if __name__ == "__main__":
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC_USDT"
    asyncio.run(main(sym))
