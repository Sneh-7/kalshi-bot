"""The Scorekeeper — settlement tracking + calibration.

This closes the loop that makes the whole system falsifiable. Kalshi tells us
how each market ACTUALLY resolved; we:

  1. record the ground truth (MarketResolution),
  2. settle any open positions at $1 (win) or $0 (loss), held-to-resolution so
     only the entry fee is charged, and
  3. score every prediction we ever made against the outcome (Brier / log-loss),
     comparing the model to the market baseline.

Step 3 is the paper-week success metric: if the model's Brier/log-loss does not
beat the market's, there is no edge and no real money should be risked. It is
also what lets Kelly sizing and the likelihood strength be tuned to reality
rather than to a guess.

Uses Kalshi settlement only — NOT the sibling's Polymarket Gamma API, which
resolves the wrong exchange.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import distinct

from ..config import settings
from ..database import session_scope
from ..fees import realized_pnl
from ..models import AgentState, MarketResolution, PredictionRecord, Trade
from .kalshi import kalshi

log = logging.getLogger("scorekeeper")

# Poll settlement at most this often (seconds) — one GET per unresolved ticker
# is not something to do every 30s loop tick.
_MIN_POLL_INTERVAL = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(p: float) -> float:
    return min(max(float(p), 1e-4), 1.0 - 1e-4)


# --- polling -------------------------------------------------------------

def _unresolved_tickers() -> List[str]:
    with session_scope() as s:
        resolved = {row[0] for row in s.query(MarketResolution.ticker).all()}
        open_trades = {
            row[0] for row in s.query(distinct(Trade.ticker))
            .filter(Trade.closed_at.is_(None)).all()
        }
        preds = {row[0] for row in s.query(distinct(PredictionRecord.ticker)).all()}
    return list((open_trades | preds) - resolved)


def _record_resolution(ticker: str, outcome: str, raw: dict) -> None:
    with session_scope() as s:
        row = s.get(MarketResolution, ticker)
        if row is None:
            s.add(MarketResolution(
                ticker=ticker,
                resolved_outcome=outcome,
                source="kalshi",
                raw=json.dumps(raw, default=str)[:4000],
            ))
        else:
            row.resolved_outcome = outcome


def _settle_trades(ticker: str, outcome: str) -> int:
    """Close open positions on `ticker` at resolution. Returns count settled."""
    settled = 0
    with session_scope() as s:
        trades = (
            s.query(Trade)
            .filter(Trade.ticker == ticker, Trade.status == "FILLED",
                    Trade.closed_at.is_(None))
            .all()
        )
        for t in trades:
            win = (t.outcome or "").upper() == outcome
            exit_price = 1.0 if win else 0.0
            t.exit_price = exit_price
            t.held_to_resolution = True
            t.gross_pnl_usd = round((exit_price - t.price) * t.contracts, 4)
            t.net_pnl_usd = round(
                realized_pnl(
                    entry_price=t.price, exit_price=exit_price,
                    contracts=t.contracts, held_to_resolution=True,
                ), 4,
            )
            t.closed_at = _now()
            t.status = "SETTLED"
            settled += 1
    return settled


async def poll_resolutions() -> int:
    """Fetch settlement for every ticker we still track. Returns #resolved."""
    resolved = 0
    for ticker in _unresolved_tickers():
        try:
            m = await kalshi.get_market(ticker)
        except Exception as e:
            log.debug("Settlement fetch failed for %s: %s", ticker, e)
            continue
        result = str(m.get("result", "")).lower()
        if result in ("yes", "no"):
            outcome = result.upper()
            _record_resolution(ticker, outcome, m)
            n = _settle_trades(ticker, outcome)
            resolved += 1
            if n:
                log.info("Settled %d position(s) on %s -> %s", n, ticker, outcome)
    _set_state("last_settlement_at", _now().isoformat())
    return resolved


def _set_state(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.get(AgentState, key)
        if row is None:
            s.add(AgentState(key=key, value=value))
        else:
            row.value = value


def _should_poll() -> bool:
    with session_scope() as s:
        row = s.get(AgentState, "last_settlement_at")
    if row is None or not row.value:
        return True
    try:
        last = datetime.fromisoformat(row.value)
    except ValueError:
        return True
    return (_now() - last).total_seconds() >= _MIN_POLL_INTERVAL


async def maybe_poll() -> int:
    """Throttled settlement poll for the main loop."""
    if not settings.settlement_enabled:
        return 0
    if not _should_poll():
        return 0
    return await poll_resolutions()


# --- calibration ---------------------------------------------------------

def calibration_report() -> Dict[str, float]:
    """Brier + log-loss for the model vs. the market baseline over all resolved
    predictions. Lower is better; the model must beat the market to have edge."""
    with session_scope() as s:
        resolutions = {r.ticker: r.resolved_outcome for r in s.query(MarketResolution).all()}
        preds = s.query(PredictionRecord).all()

    n = 0
    brier_m = brier_mkt = ll_m = ll_mkt = 0.0
    for p in preds:
        outcome = resolutions.get(p.ticker)
        if outcome is None:
            continue
        actual = 1.0 if (p.outcome or "").upper() == outcome else 0.0
        pm = _clamp(p.model_probability)
        pk = _clamp(p.market_probability)
        n += 1
        brier_m += (pm - actual) ** 2
        brier_mkt += (pk - actual) ** 2
        ll_m += -(actual * math.log(pm) + (1 - actual) * math.log(1 - pm))
        ll_mkt += -(actual * math.log(pk) + (1 - actual) * math.log(1 - pk))

    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "brier_model": round(brier_m / n, 4),
        "brier_market": round(brier_mkt / n, 4),
        "logloss_model": round(ll_m / n, 4),
        "logloss_market": round(ll_mkt / n, 4),
        "model_beats_market": (brier_m / n) < (brier_mkt / n),
    }


def calibration_text() -> str:
    r = calibration_report()
    if r.get("n", 0) == 0:
        return "📈 Calibration: no resolved predictions yet."
    verdict = "✅ model beats market" if r["model_beats_market"] else "❌ market beats model"
    return (
        f"📈 <b>Calibration</b> (n={r['n']})\n"
        f"Brier  model {r['brier_model']} vs market {r['brier_market']}\n"
        f"LogLoss model {r['logloss_model']} vs market {r['logloss_market']}\n"
        f"{verdict}"
    )
