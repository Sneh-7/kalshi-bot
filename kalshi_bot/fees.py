"""Kalshi fee model and net-edge calculation.

This module did not exist in the source implementation — `grep -i fee` across
that backend returned nothing, and `Trade.fees_usdc` was hardcoded to 0.0.
Combined with midpoint fills, that overstated paper P&L by roughly 3-4c per
contract per round trip, which is larger than most plausible sentiment edges.

Fee schedule verified 2026-07-24 against Kalshi documentation:

    taker fee per contract = fee_rate * C * (1 - C)      C = price in dollars
    maker fee per contract = maker_multiplier * taker
    settlement fee         = none

Fees peak at C = 0.50 (1.75c per contract at the default 0.07 rate) — exactly
where sentiment-driven uncertainty concentrates. Holding to resolution incurs
the entry fee only, which makes hold-to-resolution structurally about half the
cost of round-tripping.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import settings


def taker_fee_per_contract(price: float) -> float:
    """Taker fee in dollars for one contract at `price` (0-1)."""
    c = min(max(price, 0.0), 1.0)
    return settings.kalshi_fee_rate * c * (1.0 - c)


def maker_fee_per_contract(price: float) -> float:
    """Maker fee in dollars for one contract at `price` (0-1)."""
    return settings.kalshi_maker_multiplier * taker_fee_per_contract(price)


def fee_for_order(price: float, contracts: float, is_taker: bool = True) -> float:
    """Total fee in dollars for `contracts` at `price`.

    Rounded UP to the next cent. Kalshi rounds fees up; rounding up is also the
    conservative choice for a simulator whose purpose is to avoid flattering
    itself. If exact parity with live fills matters later, verify the rounding
    rule against real /portfolio/fills data.
    """
    per = taker_fee_per_contract(price) if is_taker else maker_fee_per_contract(price)
    total = per * max(contracts, 0.0)
    return math.ceil(total * 100.0) / 100.0


@dataclass(frozen=True)
class EdgeBreakdown:
    """Every component of a trade decision, kept separate so rejections are
    explainable rather than a single opaque number."""

    raw_edge: float          # |model - market|
    fee: float               # per-contract, dollars
    half_spread: float       # per-contract cost of crossing, dollars
    slippage: float          # per-contract, dollars
    net_edge: float          # what actually has to clear the threshold
    is_taker: bool

    @property
    def total_cost(self) -> float:
        return self.fee + self.half_spread + self.slippage

    def __str__(self) -> str:  # pragma: no cover - logging helper
        return (
            f"raw={self.raw_edge:.4f} fee={self.fee:.4f} "
            f"spread={self.half_spread:.4f} slip={self.slippage:.4f} "
            f"net={self.net_edge:.4f}"
        )


def net_edge(
    model_probability: float,
    entry_price: float,
    best_bid: float | None = None,
    best_ask: float | None = None,
    is_taker: bool = True,
) -> EdgeBreakdown:
    """Cost-adjusted edge for buying at `entry_price`.

    The raw `model - market` difference is not tradeable edge. A model
    probability of 0.55 against a market at 0.50 is roughly break-even once
    fees and the spread are paid — not a 5-point edge.

    `half_spread` is measured from the real book when bid/ask are supplied, and
    falls back to `assumed_slippage` when they are not.
    """
    raw = abs(model_probability - entry_price)

    if best_bid is not None and best_ask is not None and best_ask > best_bid:
        half_spread = (best_ask - best_bid) / 2.0
    else:
        half_spread = settings.assumed_slippage

    fee = taker_fee_per_contract(entry_price) if is_taker else maker_fee_per_contract(entry_price)
    slippage = settings.assumed_slippage

    return EdgeBreakdown(
        raw_edge=raw,
        fee=fee,
        half_spread=half_spread,
        slippage=slippage,
        net_edge=raw - fee - half_spread - slippage,
        is_taker=is_taker,
    )


def contracts_for_budget(budget_usd: float, price: float) -> int:
    """Whole contracts affordable at `price`, leaving room for the entry fee.

    Kalshi trades whole contracts; returning a float here would silently
    misstate both position size and P&L.
    """
    p = min(max(price, 0.01), 0.99)
    per_contract_cost = p + taker_fee_per_contract(p)
    if per_contract_cost <= 0:
        return 0
    return int(budget_usd // per_contract_cost)


def realized_pnl(
    entry_price: float,
    exit_price: float,
    contracts: float,
    entry_is_taker: bool = True,
    exit_is_taker: bool = True,
    held_to_resolution: bool = False,
) -> float:
    """Net P&L in dollars after fees.

    `held_to_resolution=True` charges the entry fee only — settlement is free,
    which is the main structural argument for holding rather than round-tripping.
    """
    gross = (exit_price - entry_price) * contracts
    fees = fee_for_order(entry_price, contracts, entry_is_taker)
    if not held_to_resolution:
        fees += fee_for_order(exit_price, contracts, exit_is_taker)
    return gross - fees
