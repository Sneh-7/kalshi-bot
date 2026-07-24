"""The conductor. One loop tick:

  Scout (news) -> Quant (probability) -> Oracle (market + book)
  -> Edge (fee-aware) -> Overseer (risk) -> Trader -> mark-to-market

Sub-steps are independently fault-tolerant: a failure logs and the loop
continues. Every prediction is recorded whether or not it trades — logging only
traded predictions would bias the sample toward cases that passed the edge
filter, making calibration look better than it is.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .config import settings
from .database import session_scope
from .fees import contracts_for_budget, net_edge
from .models import (
    AgentState,
    MarketSnapshot,
    NewsItem,
    PredictionRecord,
    Signal,
)
from .modules import execution, ingestion, intelligence, market as oracle, risk

log = logging.getLogger("orchestrator")


def _model_version(provider: str) -> str:
    model = {
        "groq": settings.groq_model,
        "openai": settings.openai_model,
        "anthropic": settings.anthropic_model,
    }.get((provider or "").lower(), "")
    return f"{provider}:{model}" if model else (provider or "heuristic")


def _set_state(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.get(AgentState, key)
        if row is None:
            s.add(AgentState(key=key, value=value))
        else:
            row.value = value


_TOPIC_KEYWORDS = {
    "TRUMP": ["trump", "donald trump", "president trump"],
    "TARIFF": ["tariff", "tariffs", "trade war"],
    "FED": ["fed", "rate", "fomc", "powell"],
    "UKRAINE": ["ukraine", "zelensky", "putin", "russia"],
    "ISRAEL": ["israel", "gaza", "palestine", "hamas", "netanyahu"],
    "ELECTIONS": ["election", "vote", "ballot", "polls", "campaign"],
    "BTC": ["bitcoin", "btc"],
    "OIL": ["oil", "opec", "crude"],
    "MACRO": ["inflation", "cpi", "gdp", "jobs"],
    "TECH": ["ai", "nvidia", "apple", "google", "chip"],
}


def _match_market(sig: Signal, snapshots: List[MarketSnapshot]) -> Optional[MarketSnapshot]:
    """Pick the snapshot whose question best matches the signal.

    This is the weakest link in the whole pipeline. Keyword overlap will match
    a "Fed rate cut" headline to "Will Powell resign?" — producing a confident
    signal on an unrelated market. That is a SILENT failure: the pipeline looks
    healthy and produces trades. Improving this is likely worth more than any
    model upgrade. A minimum score of 2 is a crude guard, not a solution.
    """
    if not snapshots:
        return None
    keywords = list(_TOPIC_KEYWORDS.get((sig.topic or "").upper(), []))
    try:
        keywords += [e.lower() for e in json.loads(sig.entities or "[]") if isinstance(e, str)]
    except (ValueError, TypeError):
        pass
    if not keywords:
        keywords = settings.keyword_list
    keywords = list(dict.fromkeys(keywords))

    best_score, best = 0, None
    for snap in snapshots:
        blob = f"{snap.question} {snap.event_ticker}".lower()
        score = sum(1 for k in keywords if k in blob)
        if score > best_score:
            best_score, best = score, snap
    return best if best_score >= 2 else None


def _log_prediction(
    sig: Signal,
    snap: MarketSnapshot,
    model_prob: float,
    raw_edge: float,
    net: float,
    traded: bool,
    skip_reason: str = "",
) -> None:
    try:
        with session_scope() as s:
            s.add(
                PredictionRecord(
                    ticker=snap.ticker,
                    outcome=snap.outcome,
                    market_question=snap.question,
                    model_probability=float(model_prob),
                    market_probability=float(snap.price),
                    raw_edge=float(raw_edge),
                    net_edge=float(net),
                    traded=traded,
                    skip_reason=skip_reason[:128],
                    sentiment=sig.sentiment or "",
                    confidence=float(sig.confidence or 0.0),
                    llm_provider=sig.llm_provider or "heuristic",
                    model_version=_model_version(sig.llm_provider or "heuristic"),
                    signal_id=sig.id,
                    event_group=(sig.topic or "GEN"),
                )
            )
    except Exception:
        log.exception("Failed to log prediction for signal %s", getattr(sig, "id", "?"))


async def _analyze(item: NewsItem) -> Optional[Signal]:
    extr = await intelligence.extract(item.title, item.summary or "")
    posterior, lr = intelligence.bayesian_update(0.5, extr.sentiment, extr.confidence)
    sig = Signal(
        news_item_id=item.id,
        sentiment=extr.sentiment,
        confidence=extr.confidence,
        topic=extr.topic,
        entities=json.dumps(extr.entities),
        rationale=extr.rationale,
        llm_provider=extr.provider,
        prior=0.5,
        posterior=posterior,
        likelihood_ratio=lr,
    )
    with session_scope() as s:
        s.add(sig)
        s.flush()
        s.refresh(sig)
        s.expunge(sig)
    return sig


async def run_once() -> None:
    log.info("---- tick ----")

    # 1) Scout
    try:
        new_items = await ingestion.ingest_once()
    except Exception:
        log.exception("Ingestion failed")
        new_items = []

    # 2) Quant
    signals: List[Signal] = []
    for item in new_items:
        try:
            sig = await _analyze(item)
            if sig:
                signals.append(sig)
        except Exception:
            log.exception("Analysis failed for item %s", getattr(item, "id", "?"))

    # 3) Oracle
    try:
        markets = await oracle.fetch_markets()
        pairs = await oracle.fetch_books(markets)
        snapshots = oracle.persist_snapshots(pairs)
    except Exception:
        log.exception("Market fetch failed")
        snapshots = []

    execution.expire_old_recommendations()

    considered = traded = rejected = 0

    # 4-6) Decide -> risk -> execute
    for sig in signals:
        if sig.confidence < settings.min_signal_confidence:
            continue

        snap = _match_market(sig, snapshots)
        if snap is None:
            continue

        considered += 1

        # Bayesian posterior, seeded with the market's own price as the prior.
        # Using the market as prior means we only move away from it when the
        # news genuinely warrants it.
        target, _lr = intelligence.bayesian_update(
            snap.price, sig.sentiment, sig.confidence
        )

        # Direction: buy YES if we think it's underpriced, else buy NO.
        if target > snap.price:
            outcome, entry_price, depth = "YES", snap.best_ask, snap.ask_depth
            model_prob = target
        else:
            # Buying NO at (1 - yes_bid); our NO probability is (1 - target).
            outcome, entry_price, depth = "NO", round(1.0 - snap.best_bid, 4), snap.bid_depth
            model_prob = round(1.0 - target, 4)

        breakdown = net_edge(
            model_probability=model_prob,
            entry_price=entry_price,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            is_taker=True,
        )

        contracts = contracts_for_budget(settings.max_usd_per_trade, entry_price)
        plan = risk.TradePlan(
            ticker=snap.ticker,
            market_question=snap.question,
            outcome=outcome,
            side="BUY",
            entry_price=entry_price,
            contracts=contracts,
            size_usd=round(contracts * entry_price, 2),
            model_probability=model_prob,
            breakdown=breakdown,
            market_mid=snap.price,
            available_depth=depth,
            close_time=snap.close_time,
            signal_id=sig.id,
            snapshot_id=snap.id,
            idem_key=execution.make_idem_key(snap.ticker, outcome, sig.id),
        )

        decision = risk.evaluate(plan)
        if not decision.approved:
            rejected += 1
            _log_prediction(
                sig, snap, model_prob, breakdown.raw_edge, breakdown.net_edge,
                traded=False, skip_reason=decision.reason,
            )
            risk.record_event("risk", "INFO", f"Rejected: {decision.reason}",
                              {"ticker": snap.ticker, "net_edge": breakdown.net_edge})
            continue

        try:
            result = await execution.execute(decision.plan)
            if result is not None:
                traded += 1
            _log_prediction(
                sig, snap, model_prob, breakdown.raw_edge, breakdown.net_edge,
                traded=result is not None,
            )
        except Exception as e:
            log.exception("Execution failed")
            risk.record_event("trade", "ERROR", f"Execution failed: {e}", {})

    # 7) Mark to market against the freshest snapshots.
    quotes: Dict[str, Tuple[float, float]] = {
        snap.ticker: (snap.best_bid, snap.best_ask) for snap in snapshots
    }

    def _lookup(ticker: str, outcome: str) -> Optional[Tuple[float, float]]:
        q = quotes.get(ticker)
        if q is None:
            return None
        bid, ask = q
        # A NO position's bid is (1 - yes_ask).
        return (round(1.0 - ask, 4), round(1.0 - bid, 4)) if outcome.upper() == "NO" else q

    try:
        execution.mark_to_market(_lookup)
    except Exception:
        log.exception("Mark-to-market failed")

    _set_state("last_loop_at", datetime.now(timezone.utc).isoformat())
    log.info(
        "tick done: %d news, %d signals, %d markets, %d considered, %d traded, %d rejected",
        len(new_items), len(signals), len(snapshots), considered, traded, rejected,
    )


class AgentLoop:
    """Runs `run_once` on a fixed interval."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await run_once()
            except Exception:
                log.exception("Loop tick crashed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.loop_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        log.info("Agent loop started; interval=%ss", settings.loop_interval_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        log.info("Agent loop stopped")


agent_loop = AgentLoop()
