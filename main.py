#!/usr/bin/env python3
"""kalshi-bot — headless entry point.

    python3 main.py            run the agent loop continuously
    python3 main.py --once     run a single tick and exit
    python3 main.py --check    verify configuration and connectivity, no trading

Defaults to PAPER mode against Kalshi's DEMO environment. Reaching production
with real money requires deliberately changing both KALSHI_BASE_URL and
TRADING_MODE.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from kalshi_bot.config import settings
from kalshi_bot.database import init_db


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )


async def _check() -> int:
    """Verify config and connectivity without trading."""
    from kalshi_bot.modules.kalshi import kalshi

    print(f"mode          {settings.trading_mode}")
    print(f"base url      {settings.kalshi_base_url}")
    print(f"environment   {'DEMO' if settings.is_demo else 'PRODUCTION ⚠️'}")
    print(f"database      {settings.database_url}")
    print(f"credentials   {'present' if kalshi.has_credentials else 'absent (market data only)'}")
    print(f"signing prefix{kalshi.path_prefix!r}")
    print(f"rss feeds     {len(settings.rss_list)}")
    print()

    ok = True

    try:
        status = await kalshi.get_exchange_status()
        print(f"✅ exchange status: {status}")
    except Exception as e:
        print(f"❌ exchange status failed: {e}")
        ok = False

    try:
        markets = await kalshi.fetch_open_markets(max_pages=1)
        print(f"✅ fetched {len(markets)} markets")
        if markets:
            m = markets[0]
            print(f"   sample: {m.ticker} bid={m.yes_bid:.2f} ask={m.yes_ask:.2f}")
            book = await kalshi.get_orderbook(m.ticker)
            print(
                f"✅ orderbook: bid={book.best_bid:.2f}({book.bid_depth}) "
                f"ask={book.best_ask:.2f}({book.ask_depth}) valid={book.is_valid}"
            )
    except Exception as e:
        print(f"❌ market data failed: {e}")
        ok = False

    if kalshi.has_credentials:
        try:
            bal = await kalshi.get_balance()
            print(f"✅ balance (auth OK): {bal}")
        except Exception as e:
            print(f"❌ authenticated call failed: {e}")
            print("   If this is a 401, check the signed path includes the API prefix.")
            ok = False
    else:
        print("⏭  skipping authenticated checks (no credentials)")

    return 0 if ok else 1


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi sentiment trading agent")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--check", action="store_true", help="verify config/connectivity")
    args = parser.parse_args()

    _setup_logging()

    if args.check:
        return await _check()

    init_db()

    if settings.trading_mode == "LIVE" and not settings.is_demo:
        logging.warning("=" * 62)
        logging.warning("LIVE MODE ON PRODUCTION — real money is at risk")
        logging.warning("=" * 62)

    from kalshi_bot.orchestrator import agent_loop, run_once

    if args.once:
        await run_once()
        return 0

    agent_loop.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await agent_loop.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(130)
