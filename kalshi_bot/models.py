"""SQLAlchemy models. These ARE the audit trail.

  NewsItem        raw input artifact
  Signal          LLM-extracted structured belief + Bayesian posterior
  MarketSnapshot  market state at decision time (now includes book depth)
  Trade           joins news -> signal -> snapshot -> action, with real fees
  PredictionRecord every probability emitted, traded or not — the falsifiable
                  track record that answers "does this make money"
  MarketResolution ground truth from Kalshi settlement

Timestamps are stored timezone-aware. The source implementation compared a
tz-aware cutoff against tz-naive values read back from SQLite and crashed with
`TypeError: can't compare offset-naive and offset-aware datetimes`; `_utcnow`
plus `DateTime(timezone=True)` keeps both sides aware.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_TS = DateTime(timezone=True)


class NewsItem(Base):
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)
    source = Column(String(64), nullable=False)
    url = Column(String(1024), unique=True, nullable=False)
    title = Column(String(1024), nullable=False)
    summary = Column(Text, default="")
    published_at = Column(_TS, default=_utcnow)
    ingested_at = Column(_TS, default=_utcnow, index=True)

    signals = relationship("Signal", back_populates="news_item")


class Signal(Base):
    """Output of the Quant module: LLM label + deterministic Bayesian update."""

    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    news_item_id = Column(Integer, ForeignKey("news_items.id"), nullable=False)
    created_at = Column(_TS, default=_utcnow, index=True)

    # LLM-extracted. The LLM never outputs the probability itself.
    sentiment = Column(String(16))
    confidence = Column(Float, default=0.0)
    topic = Column(String(64))
    entities = Column(Text, default="[]")
    rationale = Column(Text, default="")
    llm_provider = Column(String(32), default="heuristic")

    # Computed in Python.
    prior = Column(Float, default=0.5)
    posterior = Column(Float, default=0.5)
    likelihood_ratio = Column(Float, default=1.0)

    news_item = relationship("NewsItem", back_populates="signals")


class MarketSnapshot(Base):
    """Kalshi market state at a point in time."""

    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True)
    captured_at = Column(_TS, default=_utcnow, index=True)
    ticker = Column(String(128), index=True, nullable=False)
    event_ticker = Column(String(128), default="")
    question = Column(String(1024), default="")
    outcome = Column(String(8), nullable=False)        # YES | NO
    price = Column(Float, default=0.5)                 # mid, implied probability
    best_bid = Column(Float, default=0.0)
    best_ask = Column(Float, default=1.0)
    # Depth matters: it caps how much could actually have been filled.
    bid_depth = Column(Integer, default=0)
    ask_depth = Column(Integer, default=0)
    liquidity = Column(Float, default=0.0)
    volume_24h = Column(Float, default=0.0)
    close_time = Column(String(64), default="")


class Trade(Base):
    """A decision + execution record. Idempotent via `idem_key`."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    created_at = Column(_TS, default=_utcnow, index=True)
    idem_key = Column(String(128), unique=True, nullable=False)

    mode = Column(String(10), default="PAPER")
    status = Column(String(16), default="FILLED")

    ticker = Column(String(128), index=True, nullable=False)
    market_question = Column(String(1024), default="")
    outcome = Column(String(8), nullable=False)
    side = Column(String(4), nullable=False)

    # Entry. `price` is the actual fill price (the ask for a buy), NOT the mid.
    price = Column(Float, nullable=False)
    contracts = Column(Integer, nullable=False)
    size_usd = Column(Float, nullable=False)
    entry_fee_usd = Column(Float, default=0.0)
    exit_fee_usd = Column(Float, default=0.0)
    is_taker = Column(Boolean, default=True)

    # Decision context.
    model_probability = Column(Float, nullable=False)
    raw_edge = Column(Float, default=0.0)     # |model - market|, before costs
    net_edge = Column(Float, default=0.0)     # after fees + spread + slippage
    market_mid = Column(Float, default=0.0)   # mid at decision time, for reference

    signal_id = Column(Integer, ForeignKey("signals.id"))
    snapshot_id = Column(Integer, ForeignKey("market_snapshots.id"))

    # Realized.
    closed_at = Column(_TS, nullable=True)
    exit_price = Column(Float, nullable=True)
    gross_pnl_usd = Column(Float, default=0.0)
    net_pnl_usd = Column(Float, default=0.0)   # after all fees — the real number
    held_to_resolution = Column(Boolean, default=False)

    kalshi_order_id = Column(String(128), default="")
    notes = Column(Text, default="")

    __table_args__ = (Index("ix_trades_status_created", "status", "created_at"),)

    @property
    def total_fees_usd(self) -> float:
        return (self.entry_fee_usd or 0.0) + (self.exit_fee_usd or 0.0)


class Recommendation(Base):
    """A staged bet awaiting operator approval (RECOMMEND mode)."""

    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True)
    created_at = Column(_TS, default=_utcnow, index=True)
    expires_at = Column(_TS, nullable=True)
    status = Column(String(16), default="PENDING")

    ticker = Column(String(128), index=True, nullable=False)
    market_question = Column(String(1024), default="")
    outcome = Column(String(8), nullable=False)
    side = Column(String(4), nullable=False)

    suggested_price = Column(Float, nullable=False)
    suggested_contracts = Column(Integer, nullable=False)
    suggested_size_usd = Column(Float, nullable=False)
    model_probability = Column(Float, nullable=False)
    net_edge = Column(Float, nullable=False)

    signal_id = Column(Integer, ForeignKey("signals.id"))
    snapshot_id = Column(Integer, ForeignKey("market_snapshots.id"))

    decided_at = Column(_TS, nullable=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    notes = Column(Text, default="")

    __table_args__ = (
        Index("ix_recommendations_status_created", "status", "created_at"),
    )


class AgentState(Base):
    """Key/value store for runtime flags. Kill switch lives here so it
    survives a restart."""

    __tablename__ = "agent_state"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(_TS, default=_utcnow, onupdate=_utcnow)


class LogEvent(Base):
    """Structured operational log, distinct from Python logger output.

    Rejections are logged here too — "47 candidates, all below threshold" and
    "0 candidates" mean very different things, and only this table can tell
    them apart after the fact.
    """

    __tablename__ = "log_events"

    id = Column(Integer, primary_key=True)
    created_at = Column(_TS, default=_utcnow, index=True)
    level = Column(String(16), default="INFO")
    component = Column(String(32), default="agent")
    message = Column(Text, default="")
    data = Column(Text, default="{}")


class PredictionRecord(Base):
    """Every probability the agent forms, whether or not it trades.

    This is the falsifiable track record. Logging only traded predictions
    would bias the sample toward cases that passed the edge filter, making
    calibration look better than it is.
    """

    __tablename__ = "prediction_records"

    id = Column(Integer, primary_key=True)
    created_at = Column(_TS, default=_utcnow, index=True)

    ticker = Column(String(128), index=True, nullable=False)
    outcome = Column(String(8), nullable=False)
    market_question = Column(String(1024), default="")

    model_probability = Column(Float, nullable=False)
    market_probability = Column(Float, nullable=False)
    raw_edge = Column(Float, default=0.0)
    net_edge = Column(Float, default=0.0)
    traded = Column(Boolean, default=False)
    skip_reason = Column(String(128), default="")

    sentiment = Column(String(16), default="")
    confidence = Column(Float, default=0.0)
    llm_provider = Column(String(32), default="heuristic")
    model_version = Column(String(64), default="")
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)

    # Groups correlated predictions from one news cycle. Without this, 15
    # positions driven by one story look like 15 independent samples.
    event_group = Column(String(128), default="", index=True)

    __table_args__ = (
        Index("ix_predictions_ticker_created", "ticker", "created_at"),
    )


class MarketResolution(Base):
    """Ground truth from Kalshi settlement. One row per ticker."""

    __tablename__ = "market_resolutions"

    ticker = Column(String(128), primary_key=True)
    resolved_outcome = Column(String(8), nullable=False)   # YES | NO
    resolved_at = Column(_TS, default=_utcnow)
    source = Column(String(64), default="kalshi")
    raw = Column(Text, default="{}")
