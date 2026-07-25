"""The Overseer — hard risk gates.

Every proposed trade passes through `evaluate()`, which returns an approved
(possibly resized) plan or a reason for rejection. Rejections are logged: a
cycle producing "47 candidates, all below threshold" is healthy, while "0
candidates" is broken, and only the reason string distinguishes them.

Three gates are new relative to the source implementation:

  * LIQUIDITY   — never take more than a fraction of resting depth. Paper fills
                  of 500 contracts into a book holding 40 are fiction, and this
                  error is correlated with apparent edge: thin markets are
                  exactly where mispricings appear.
  * TIME-TO-CLOSE — skip markets resolving imminently; a news thesis needs room
                  to play out.
  * NET EDGE    — the threshold is applied to fee- and spread-adjusted edge,
                  not to a raw model-minus-market difference.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func

from ..config import settings
from ..database import session_scope
from ..fees import EdgeBreakdown, contracts_for_budget
from ..models import AgentState, LogEvent, Recommendation, Trade

log = logging.getLogger("overseer")


@dataclass(frozen=True)
class TradePlan:
    ticker: str
    market_question: str
    outcome: str              # YES | NO
    side: str                 # BUY
    entry_price: float        # the ASK for a buy — not the mid
    contracts: int
    size_usd: float
    model_probability: float
    breakdown: EdgeBreakdown
    market_mid: float
    available_depth: int
    close_time: str = ""
    signal_id: Optional[int] = None
    snapshot_id: Optional[int] = None
    idem_key: str = ""

    @property
    def net_edge(self) -> float:
        return self.breakdown.net_edge

    @property
    def raw_edge(self) -> float:
        return self.breakdown.raw_edge


@dataclass
class RiskDecision:
    approved: bool
    plan: Optional[TradePlan]
    reason: str = ""


# --- kill switch ---------------------------------------------------------

def kill_switch_active() -> bool:
    with session_scope() as s:
        row = s.get(AgentState, "kill_switch")
        if row and row.value:
            return row.value.lower() == "true"
    return settings.kill_switch


def set_kill_switch(enabled: bool) -> None:
    with session_scope() as s:
        row = s.get(AgentState, "kill_switch")
        if row is None:
            s.add(AgentState(key="kill_switch", value="true" if enabled else "false"))
        else:
            row.value = "true" if enabled else "false"
    log.warning("Kill switch set to %s", enabled)


# --- portfolio state -----------------------------------------------------

def _daily_pnl() -> float:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with session_scope() as s:
        total = (
            s.query(func.coalesce(func.sum(Trade.net_pnl_usd), 0.0))
            .filter(Trade.created_at >= cutoff)
            .scalar()
        )
    return float(total or 0.0)


def _open_positions() -> int:
    with session_scope() as s:
        return int(
            s.query(func.count(Trade.id))
            .filter(Trade.status == "FILLED")
            .filter(Trade.closed_at.is_(None))
            .scalar()
            or 0
        )


def _pending_recommendations() -> int:
    with session_scope() as s:
        return int(
            s.query(func.count(Recommendation.id))
            .filter(Recommendation.status == "PENDING")
            .scalar()
            or 0
        )


def _minutes_to_close(close_time: str) -> Optional[float]:
    if not close_time:
        return None
    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


# --- evaluation ----------------------------------------------------------

def evaluate(plan: TradePlan, count_pending: bool = True) -> RiskDecision:
    """Approve, resize, or reject. Cannot be bypassed by a strategy."""
    if kill_switch_active():
        return RiskDecision(False, None, "KILL_SWITCH active")

    if plan.contracts < 1:
        return RiskDecision(False, None, "Zero contracts affordable")

    if not 0.0 < plan.entry_price < 1.0:
        return RiskDecision(False, None, f"Invalid entry price {plan.entry_price}")

    # Net edge — after fees, spread, slippage.
    if plan.net_edge < settings.min_net_edge:
        return RiskDecision(
            False,
            None,
            f"Net edge {plan.net_edge:.4f} < {settings.min_net_edge} "
            f"(raw {plan.raw_edge:.4f}, costs {plan.breakdown.total_cost:.4f})",
        )

    # Time to resolution.
    mins = _minutes_to_close(plan.close_time)
    if mins is not None and mins < settings.min_minutes_to_close:
        return RiskDecision(False, None, f"Closes in {mins:.0f}m")

    # Liquidity: never take more than a fraction of the book.
    if plan.available_depth > 0:
        max_contracts = int(plan.available_depth * settings.max_book_fraction)
        if max_contracts < 1:
            return RiskDecision(False, None, f"Book too thin ({plan.available_depth} contracts)")
        if plan.contracts > max_contracts:
            plan = replace(
                plan,
                contracts=max_contracts,
                size_usd=round(max_contracts * plan.entry_price, 2),
            )

    # Notional cap.
    if plan.size_usd > settings.max_usd_per_trade:
        capped = contracts_for_budget(settings.max_usd_per_trade, plan.entry_price)
        if capped < 1:
            return RiskDecision(False, None, "Max trade size buys zero contracts")
        plan = replace(
            plan, contracts=capped, size_usd=round(capped * plan.entry_price, 2)
        )

    committed = _open_positions() + (_pending_recommendations() if count_pending else 0)
    if committed >= settings.max_open_positions:
        return RiskDecision(False, None, f"Max open positions ({committed})")

    daily = _daily_pnl()
    if daily <= -abs(settings.daily_drawdown_usd):
        set_kill_switch(True)
        return RiskDecision(False, None, f"Daily drawdown breached: {daily:.2f}")

    return RiskDecision(True, plan)


def kelly_contracts(
    model_probability: float,
    entry_price: float,
    analyst_kelly: float,
    available_depth: int = 0,
) -> int:
    """Whole contracts to buy under a fractional-Kelly sizing rule (MODERATE).

    For a binary contract bought at cost `c` that pays $1 on a win with win
    probability `p`, the full-Kelly bankroll fraction is:

        f* = (p - c) / (1 - c)        (0 when p <= c)

    We stake a *fraction* of that: the global `kelly_fraction` safety multiplier
    (e.g. 0.5 = half-Kelly) times the analyst's own `analyst_kelly` conviction
    (0..1). Full Kelly on correlated, news-driven bets is a fast way to ruin;
    both multipliers only ever shrink the stake. The dollar stake is then capped
    by `max_trade_fraction` of deployable capital, by `max_usd_per_trade`, and by
    a fraction of resting book depth.
    """
    c = min(max(entry_price, 0.01), 0.99)
    p = min(max(model_probability, 0.0), 1.0)
    if p <= c:
        return 0

    f_star = (p - c) / (1.0 - c)
    frac = max(0.0, min(1.0, settings.kelly_fraction)) * max(0.0, min(1.0, analyst_kelly))
    stake = f_star * frac * settings.deployable_capital_usd

    cap = min(
        settings.max_trade_fraction * settings.deployable_capital_usd,
        settings.max_usd_per_trade,
    )
    stake = min(stake, cap)

    contracts = contracts_for_budget(stake, c)
    if available_depth > 0:
        contracts = min(contracts, int(available_depth * settings.max_book_fraction))
    return max(0, contracts)


def record_event(component: str, level: str, message: str, data: Optional[dict] = None) -> None:
    """Structured log row. Rejections belong here — they are data."""
    try:
        with session_scope() as s:
            s.add(
                LogEvent(
                    component=component,
                    level=level,
                    message=message,
                    data=json.dumps(data or {}, default=str),
                )
            )
    except Exception:
        log.exception("Failed to record event: %s", message)
