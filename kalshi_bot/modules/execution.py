"""The Trader — idempotent execution.

PAPER     simulate a fill at the ASK, charge real Kalshi fees, cap by depth
RECOMMEND stage a pending row for manual approval
LIVE      place a real order (requires credentials and explicit opt-in)

Two defects from the source implementation are fixed here, and together they
are the difference between a paper result that predicts live performance and
one that flatters it:

1. FILL PRICE. The source filled at `plan.price`, which came from the market
   module as the MIDPOINT, while the note string claimed "Paper fill at best
   ask". Real taker orders cross the spread. On a 4c-wide market that is a free
   2c per contract that does not exist live.

2. FEES. `Trade.fees_usdc` was hardcoded to 0.0 and no fee math existed
   anywhere in that codebase. Kalshi charges 0.07 * C * (1-C) per contract as
   taker — 1.75c at 50c, where sentiment trading concentrates.

Combined, those two overstated paper P&L by roughly 3-4c per contract per
round trip, which is larger than most plausible sentiment edges. A losing
strategy could report a profit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import session_scope
from ..fees import fee_for_order, realized_pnl
from ..models import Recommendation, Trade
from .kalshi import kalshi
from .risk import TradePlan

log = logging.getLogger("trader")


def make_idem_key(ticker: str, outcome: str, signal_id: Optional[int]) -> str:
    return f"{ticker}:{outcome}:{signal_id or 'none'}"


def _record_paper_fill(plan: TradePlan) -> Trade:
    """Simulate a taker fill at the ask, with fees charged.

    `plan.entry_price` is the ASK (set by the orchestrator from the book), not
    the mid — see module docstring.
    """
    fill_price = min(max(plan.entry_price, 0.01), 0.99)
    contracts = int(plan.contracts)
    entry_fee = fee_for_order(fill_price, contracts, is_taker=True)
    gross_cost = fill_price * contracts

    return Trade(
        idem_key=plan.idem_key,
        mode="PAPER",
        status="FILLED",
        ticker=plan.ticker,
        market_question=plan.market_question,
        outcome=plan.outcome,
        side=plan.side,
        price=fill_price,
        contracts=contracts,
        size_usd=round(gross_cost, 2),
        entry_fee_usd=entry_fee,
        is_taker=True,
        model_probability=plan.model_probability,
        raw_edge=plan.raw_edge,
        net_edge=plan.net_edge,
        market_mid=plan.market_mid,
        signal_id=plan.signal_id,
        snapshot_id=plan.snapshot_id,
        notes=(
            f"Paper fill at ask {fill_price:.2f} "
            f"(mid was {plan.market_mid:.2f}); entry fee ${entry_fee:.2f}"
        ),
    )


def create_recommendation(plan: TradePlan) -> Optional[Recommendation]:
    rec = Recommendation(
        ticker=plan.ticker,
        market_question=plan.market_question,
        outcome=plan.outcome,
        side=plan.side,
        suggested_price=plan.entry_price,
        suggested_contracts=plan.contracts,
        suggested_size_usd=plan.size_usd,
        model_probability=plan.model_probability,
        net_edge=plan.net_edge,
        signal_id=plan.signal_id,
        snapshot_id=plan.snapshot_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        notes=f"idem={plan.idem_key} | {plan.breakdown}",
    )
    with session_scope() as s:
        s.add(rec)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            log.info("Recommendation idempotency hit: %s", plan.idem_key)
            return None
        s.refresh(rec)
        s.expunge(rec)
    log.info(
        "Recommendation: %s %s %d @ %.2f (net edge %.4f)",
        rec.side, rec.ticker, rec.suggested_contracts, rec.suggested_price, rec.net_edge,
    )
    return rec


async def _place_live_order(plan: TradePlan) -> Trade:
    """Place a real Kalshi order. Fill-or-kill at the ask (always taker)."""
    side = "yes" if plan.outcome.upper() == "YES" else "no"
    price_cents = int(round(plan.entry_price * 100))
    price_cents = min(max(price_cents, 1), 99)

    order = await kalshi.create_order(
        ticker=plan.ticker,
        side=side,
        count=plan.contracts,
        price_cents=price_cents,
        action="buy",
        time_in_force="fill_or_kill",
        client_order_id=plan.idem_key[:36],
    )
    filled = int(order.filled_count or 0)
    if filled == 0:
        raise RuntimeError(f"Fill-or-kill order not filled for {plan.ticker}")

    fill_price = order.price
    entry_fee = fee_for_order(fill_price, filled, is_taker=True)
    return Trade(
        idem_key=plan.idem_key,
        mode="LIVE",
        status="FILLED",
        ticker=plan.ticker,
        market_question=plan.market_question,
        outcome=plan.outcome,
        side=plan.side,
        price=fill_price,
        contracts=filled,
        size_usd=round(fill_price * filled, 2),
        entry_fee_usd=entry_fee,
        is_taker=True,
        model_probability=plan.model_probability,
        raw_edge=plan.raw_edge,
        net_edge=plan.net_edge,
        market_mid=plan.market_mid,
        signal_id=plan.signal_id,
        snapshot_id=plan.snapshot_id,
        kalshi_order_id=order.order_id,
        notes=f"Live order {order.order_id}",
    )


async def execute(plan: TradePlan) -> Optional[object]:
    """Execute per TRADING_MODE. Returns Recommendation, Trade, or None."""
    mode = settings.trading_mode

    if mode == "RECOMMEND":
        return create_recommendation(plan)

    if mode == "PAPER":
        trade = _record_paper_fill(plan)
    elif mode == "LIVE":
        if not kalshi.has_credentials:
            raise RuntimeError("LIVE mode requires Kalshi credentials")
        trade = await _place_live_order(plan)
    else:
        raise ValueError(f"Unknown trading mode {mode!r}")

    with session_scope() as s:
        s.add(trade)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            log.info("Trade idempotency hit: %s", plan.idem_key)
            return None
        s.refresh(trade)
        s.expunge(trade)

    log.info(
        "%s trade: %s %s %d @ %.2f | fee $%.2f | net edge %.4f",
        trade.mode, trade.outcome, trade.ticker[:28], trade.contracts,
        trade.price, trade.entry_fee_usd, trade.net_edge,
    )
    return trade


def expire_old_recommendations() -> int:
    cutoff = datetime.now(timezone.utc)
    with session_scope() as s:
        rows = (
            s.query(Recommendation)
            .filter(Recommendation.status == "PENDING")
            .filter(Recommendation.expires_at < cutoff)
            .all()
        )
        for r in rows:
            r.status = "EXPIRED"
        return len(rows)


def mark_to_market(price_lookup: Callable[[str, str], Optional[Tuple[float, float]]]) -> None:
    """Update unrealized P&L on open positions, net of exit fees.

    `price_lookup(ticker, outcome)` returns (best_bid, best_ask) or None.
    Marking a long position at the BID is the honest choice — that is where you
    could actually sell. Marking at the mid would reintroduce, on exit, exactly
    the optimism this module removes on entry.
    """
    with session_scope() as s:
        open_trades = (
            s.query(Trade)
            .filter(Trade.status == "FILLED")
            .filter(Trade.closed_at.is_(None))
            .all()
        )
        for t in open_trades:
            quote = price_lookup(t.ticker, t.outcome)
            if quote is None:
                continue
            best_bid, _best_ask = quote
            t.exit_price = best_bid
            t.gross_pnl_usd = round((best_bid - t.price) * t.contracts, 4)
            t.net_pnl_usd = round(
                realized_pnl(
                    entry_price=t.price,
                    exit_price=best_bid,
                    contracts=t.contracts,
                    held_to_resolution=False,
                ),
                4,
            )
