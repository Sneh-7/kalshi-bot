"""Tests for the production build: analyst, Kelly sizing, settlement/calibration,
Truth Social parsing, and the notifier's read-only helpers."""
from __future__ import annotations

import asyncio

from kalshi_bot import models
from kalshi_bot.config import settings
from kalshi_bot.database import session_scope
from kalshi_bot.modules import analyst, notifier, risk, settlement
from kalshi_bot.modules.ingestion import _strip_html


# --- Kelly sizing --------------------------------------------------------

def test_kelly_positive_edge_sizes_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "deployable_capital_usd", 600.0)
    monkeypatch.setattr(settings, "kelly_fraction", 0.5)
    monkeypatch.setattr(settings, "max_trade_fraction", 0.05)
    monkeypatch.setattr(settings, "max_usd_per_trade", 30.0)

    # p=0.7 c=0.5 -> f* = 0.4; frac = 0.5; stake = 120, capped to $30 -> ~57 contracts.
    n = risk.kelly_contracts(0.70, 0.50, analyst_kelly=1.0, available_depth=0)
    assert n > 0
    # Book-depth cap: 25% of 10 contracts = 2.
    n_thin = risk.kelly_contracts(0.70, 0.50, analyst_kelly=1.0, available_depth=10)
    assert n_thin == 2


def test_kelly_no_edge_is_zero():
    assert risk.kelly_contracts(0.50, 0.50, analyst_kelly=1.0) == 0
    assert risk.kelly_contracts(0.40, 0.50, analyst_kelly=1.0) == 0


# --- Analyst -------------------------------------------------------------

def test_analyst_normalize_clamps_and_defaults():
    d = analyst._normalize(
        {"side": "buy", "probability": 2.0, "confidence": -1,
         "kelly_fraction": 5, "rationale": "x", "key_evidence": ["a", "b"]},
        "test",
    )
    assert d.side == "SKIP"            # invalid side -> SKIP
    assert d.probability == 1.0        # clamped
    assert d.confidence == 0.0
    assert d.kelly_fraction == 1.0


def test_analyst_heuristic_fallback_directions(monkeypatch):
    # Force the deterministic heuristic path (disable live providers) so the test
    # doesn't depend on a local .env / an installed Claude CLI.
    monkeypatch.setattr(settings, "analyst_enabled", False)
    buy_yes = analyst.Candidate(
        ticker="T", question="q", yes_mid=0.48, best_bid=0.45, best_ask=0.50,
        close_time="", prelim_probability=0.70,
    )
    d = asyncio.run(analyst.analyze_finalist(buy_yes))
    assert d.provider == "heuristic" and d.side == "YES"

    skip = analyst.Candidate(
        ticker="T", question="q", yes_mid=0.48, best_bid=0.45, best_ask=0.50,
        close_time="", prelim_probability=0.47,
    )
    assert asyncio.run(analyst.analyze_finalist(skip)).side == "SKIP"


# --- Truth Social parsing ------------------------------------------------

def test_strip_html():
    assert _strip_html("<p>Hello&amp; <b>world</b></p>") == "Hello& world"


# --- Settlement + calibration -------------------------------------------

def test_settlement_settles_trade_pnl(db):
    with session_scope() as s:
        s.add(models.Trade(
            idem_key="k1", mode="PAPER", status="FILLED", ticker="MKT-1",
            outcome="YES", side="BUY", price=0.40, contracts=10, size_usd=4.0,
            entry_fee_usd=0.17, model_probability=0.7,
        ))

    n = settlement._settle_trades("MKT-1", "YES")
    assert n == 1
    with session_scope() as s:
        t = s.query(models.Trade).filter_by(ticker="MKT-1").one()
        assert t.status == "SETTLED"
        assert t.closed_at is not None
        assert t.exit_price == 1.0
        # gross = (1.0 - 0.40) * 10 = 6.0; net = 6.0 - entry fee only (held).
        assert t.gross_pnl_usd == 6.0
        assert t.net_pnl_usd < t.gross_pnl_usd


def test_calibration_model_beats_market(db):
    with session_scope() as s:
        s.add(models.MarketResolution(ticker="MKT-1", resolved_outcome="YES"))
        s.add(models.PredictionRecord(
            ticker="MKT-1", outcome="YES", market_question="q",
            model_probability=0.70, market_probability=0.50,
        ))
    r = settlement.calibration_report()
    assert r["n"] == 1
    assert r["brier_model"] < r["brier_market"]
    assert r["model_beats_market"] is True


# --- Notifier read helpers ----------------------------------------------

def test_notifier_status_and_pause(db):
    assert isinstance(notifier.status_text(), str)
    assert isinstance(notifier.pnl_text(), str)
    notifier.set_paused(True)
    assert notifier.is_paused() is True
    notifier.set_paused(False)
    assert notifier.is_paused() is False
