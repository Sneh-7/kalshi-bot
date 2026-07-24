"""The Oracle — read-only Kalshi market data.

Captures a snapshot per market on each poll, including book depth, so every
recommendation has a full audit trail and so position sizing can be capped by
liquidity that actually existed at decision time.

Fetching the real order book (rather than only the summary bid/ask) is a
deliberate cost: it is one extra request per market, and it is what makes
realistic fill simulation possible at all.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

from ..database import session_scope
from ..models import MarketSnapshot
from .kalshi import KalshiMarket, OrderBook, kalshi

log = logging.getLogger("oracle")


async def fetch_markets() -> List[KalshiMarket]:
    try:
        return await kalshi.fetch_open_markets()
    except Exception as e:
        log.warning("Kalshi market fetch failed: %s", e)
        return []


async def fetch_books(markets: List[KalshiMarket]) -> List[Tuple[KalshiMarket, Optional[OrderBook]]]:
    """Fetch order books concurrently, tolerating individual failures."""
    async def one(m: KalshiMarket) -> Tuple[KalshiMarket, Optional[OrderBook]]:
        try:
            return m, await kalshi.get_orderbook(m.ticker)
        except Exception as e:
            log.debug("Orderbook fetch failed for %s: %s", m.ticker, e)
            return m, None

    return list(await asyncio.gather(*[one(m) for m in markets]))


def persist_snapshots(
    pairs: List[Tuple[KalshiMarket, Optional[OrderBook]]]
) -> List[MarketSnapshot]:
    """Persist one YES snapshot per market. Returns detached rows.

    Only markets with a valid (uncrossed, non-empty) book are stored — a
    market you cannot price is a market you must not trade.
    """
    saved: List[MarketSnapshot] = []
    with session_scope() as s:
        for m, book in pairs:
            if book is not None and book.is_valid:
                bid, ask = book.best_bid, book.best_ask
                bid_depth, ask_depth = book.bid_depth, book.ask_depth
                price = book.mid
            else:
                # Fall back to the summary quote, with no depth information.
                bid, ask = m.yes_bid, m.yes_ask
                bid_depth = ask_depth = 0
                price = m.yes_mid
                if not (0.0 < bid < ask < 1.0):
                    continue

            row = MarketSnapshot(
                ticker=m.ticker,
                event_ticker=m.event_ticker,
                question=m.title,
                outcome="YES",
                price=price,
                best_bid=bid,
                best_ask=ask,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                liquidity=m.liquidity,
                volume_24h=m.volume_24h,
                close_time=m.close_time,
            )
            s.add(row)
            s.flush()
            saved.append(row)
        for row in saved:
            s.refresh(row)
        s.expunge_all()
    return saved
