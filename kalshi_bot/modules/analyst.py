"""The Analyst — Tier 2. Claude Opus makes the actual bet decision.

Tier 1 (intelligence.py) is a cheap sentiment classifier that runs on every
headline and, via the Bayesian layer, produces a preliminary edge for every
matched market. That is only a *filter*. The real decision — is there a bet
here, which side, how confident, how much — is made HERE, by Claude Opus, on a
small shortlist of finalists. This is what "Claude makes the decisions" means.

Provider order: Claude (primary) -> Groq (fallback) -> deterministic heuristic
(last resort, so the loop never hard-stops on an API outage).

Cost control: only `analyst_max_finalists` candidates per tick reach Claude, and
the (stable) system prompt is prompt-cached, so Opus spend stays a few dollars a
day rather than scaling with the whole market list.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from ..config import settings
from .intelligence import _safe_json

log = logging.getLogger("analyst")


@dataclass
class Candidate:
    """Everything the analyst needs to price one market."""

    ticker: str
    question: str
    yes_mid: float
    best_bid: float
    best_ask: float
    close_time: str
    # Tier-1 context.
    signal_sentiment: str = "neutral"
    signal_confidence: float = 0.0
    signal_rationale: str = ""
    topic: str = "GEN"
    prelim_probability: float = 0.5      # Bayesian posterior from Tier 1
    headlines: List[str] = field(default_factory=list)
    truth_posts: List[str] = field(default_factory=list)


@dataclass
class Decision:
    side: str               # YES | NO | SKIP
    probability: float      # analyst's P(market resolves YES), 0..1
    confidence: float       # 0..1
    kelly_fraction: float   # 0..1 fraction of full-Kelly the analyst would risk
    rationale: str
    key_evidence: List[str]
    provider: str

    @property
    def is_trade(self) -> bool:
        return self.side in ("YES", "NO")


SYSTEM_PROMPT = (
    "You are a senior prediction-market trader and analyst on Kalshi (a CFTC-"
    "regulated exchange). You are given ONE binary market, its live order book, "
    "and the news/social context that triggered a preliminary signal. Decide "
    "whether there is a genuine, tradeable edge.\n\n"
    "How to think:\n"
    "1. Estimate the TRUE probability that the market resolves YES, using the "
    "evidence — not the market price. Be calibrated: if you are not sure, say so "
    "with lower confidence, do not anchor to 0.5 or to the market.\n"
    "2. Compare your probability to the price. Buying YES needs your probability "
    "ABOVE the ask; buying NO needs it BELOW the bid, by enough to clear ~2-4 "
    "cents of fees and spread. If the edge is marginal or the news does not "
    "actually bear on THIS market, choose SKIP. Skipping is the correct, common "
    "answer — most candidates are not real edges.\n"
    "3. For 'what will Trump say / mention' markets, weigh his recent Truth "
    "Social posts and speech patterns heavily.\n"
    "4. kelly_fraction is how much of full-Kelly you would stake given your "
    "uncertainty and the event risk (0 = pass, 0.25-0.5 = typical, 1 = maximal "
    "conviction). Be conservative; correlated news-driven bets blow up bankrolls.\n"
    "Return ONLY the structured object. probability and confidence are 0-1."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "side": {"type": "string", "enum": ["YES", "NO", "SKIP"]},
        "probability": {"type": "number"},
        "confidence": {"type": "number"},
        "kelly_fraction": {"type": "number"},
        "rationale": {"type": "string"},
        "key_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "side", "probability", "confidence", "kelly_fraction",
        "rationale", "key_evidence",
    ],
    "additionalProperties": False,
}


def _clamp01(x, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def _user_prompt(c: Candidate) -> str:
    heads = "\n".join(f"- {h}" for h in c.headlines[:8]) or "- (none)"
    posts = "\n".join(f"- {p[:400]}" for p in c.truth_posts[:6]) or "- (none)"
    return (
        f"MARKET: {c.question}\n"
        f"TICKER: {c.ticker}\n"
        f"CLOSE: {c.close_time or 'unknown'}\n\n"
        f"ORDER BOOK (probabilities, 0-1):\n"
        f"  YES mid: {c.yes_mid:.3f}  best_bid: {c.best_bid:.3f}  best_ask: {c.best_ask:.3f}\n\n"
        f"PRELIMINARY SIGNAL:\n"
        f"  sentiment: {c.signal_sentiment} (confidence {c.signal_confidence:.2f})\n"
        f"  topic: {c.topic}\n"
        f"  bayesian_prob_yes: {c.prelim_probability:.3f}\n"
        f"  rationale: {c.signal_rationale}\n\n"
        f"RELATED HEADLINES:\n{heads}\n\n"
        f"RECENT TRUMP TRUTH SOCIAL POSTS:\n{posts}\n"
    )


def _normalize(data: dict, provider: str) -> Optional[Decision]:
    if not data:
        return None
    side = str(data.get("side", "SKIP")).upper().strip()
    if side not in ("YES", "NO", "SKIP"):
        side = "SKIP"
    return Decision(
        side=side,
        probability=_clamp01(data.get("probability", 0.5), 0.5),
        confidence=_clamp01(data.get("confidence", 0.0)),
        kelly_fraction=_clamp01(data.get("kelly_fraction", 0.0)),
        rationale=str(data.get("rationale", ""))[:512],
        key_evidence=[str(x)[:200] for x in (data.get("key_evidence") or [])][:6],
        provider=provider,
    )


# --- Claude (primary) ----------------------------------------------------

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def _decide_claude(c: Candidate) -> Optional[Decision]:
    if not settings.anthropic_api_key:
        return None
    try:
        client = _anthropic_client()
        resp = await client.messages.create(
            model=settings.analyst_model,
            max_tokens=3000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.analyst_effort,
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": _user_prompt(c)}],
        )
        if resp.stop_reason == "refusal":
            log.warning("Claude refused analysis for %s", c.ticker)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return _normalize(json.loads(text), f"claude:{settings.analyst_model}")
    except Exception as e:
        log.warning("Claude analyst failed for %s: %s", c.ticker, e)
        return None


# --- Claude Code CLI (subscription, no API key) --------------------------

async def _decide_cli(c: Candidate) -> Optional[Decision]:
    """Run the local Claude Code CLI headless (`claude -p`), using your Claude
    subscription instead of the API. No per-token bill. Slower (spawns a
    process) and bound by subscription usage limits — good on your Mac, less so
    on an unattended server."""
    binary = shutil.which(settings.claude_cli_path)
    if binary is None:
        return None
    prompt = (
        _user_prompt(c)
        + "\n\nReturn ONLY a single JSON object with keys: side (YES|NO|SKIP), "
        "probability (0-1), confidence (0-1), kelly_fraction (0-1), rationale, "
        "key_evidence (array of strings). No prose, no code fences."
    )
    cmd = [
        binary, "-p",
        "--output-format", "json",
        "--model", settings.analyst_cli_model,
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()),
            timeout=settings.analyst_cli_timeout,
        )
        if proc.returncode != 0:
            log.warning("Claude CLI exited %s for %s: %s",
                        proc.returncode, c.ticker, err.decode()[:200])
            return None
        # `--output-format json` wraps the answer text in an envelope.
        envelope = json.loads(out.decode())
        if envelope.get("is_error"):
            return None
        text = envelope.get("result", "")
        data = _safe_json(text)
        return _normalize(data, f"cli:{settings.analyst_cli_model}") if data else None
    except asyncio.TimeoutError:
        log.warning("Claude CLI timed out for %s", c.ticker)
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except Exception as e:
        log.warning("Claude CLI failed for %s: %s", c.ticker, e)
        return None


# --- Groq (fallback) -----------------------------------------------------

async def _decide_groq(c: Candidate) -> Optional[Decision]:
    if not settings.groq_api_key:
        return None
    prompt = (
        _user_prompt(c)
        + "\n\nReturn ONLY a JSON object with keys: side (YES|NO|SKIP), "
        "probability (0-1), confidence (0-1), kelly_fraction (0-1), rationale, "
        "key_evidence (array of strings)."
    )
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": 700,
                },
            )
            r.raise_for_status()
            data = _safe_json(r.json()["choices"][0]["message"]["content"])
            return _normalize(data, f"groq:{settings.groq_model}")
    except Exception as e:
        log.warning("Groq analyst failed for %s: %s", c.ticker, e)
        return None


# --- Heuristic (last resort) ---------------------------------------------

def _decide_heuristic(c: Candidate) -> Decision:
    """Fall back to the Tier-1 Bayesian posterior so the loop never stalls."""
    prob = _clamp01(c.prelim_probability, 0.5)
    if prob > c.best_ask:
        side = "YES"
    elif prob < c.best_bid:
        side = "NO"
    else:
        side = "SKIP"
    return Decision(
        side=side,
        probability=prob,
        confidence=_clamp01(c.signal_confidence),
        kelly_fraction=0.25,
        rationale="Heuristic fallback (Bayesian posterior vs book); no LLM available.",
        key_evidence=[c.signal_rationale] if c.signal_rationale else [],
        provider="heuristic",
    )


_CHAINS = {
    # Claude via the CLI (subscription) first, then API, then Groq.
    "cli": (_decide_cli, _decide_claude, _decide_groq),
    # Claude via the API first, then Groq.
    "claude": (_decide_claude, _decide_groq),
    # Groq first, then Claude via the API.
    "groq": (_decide_groq, _decide_claude),
}


async def analyze_finalist(c: Candidate) -> Decision:
    """Return Claude's decision, falling back through the provider chain to a
    deterministic heuristic so the loop never hard-stops."""
    if settings.analyst_enabled:
        chain = _CHAINS.get(settings.analyst_provider, _CHAINS["claude"])
        for fn in chain:
            res = await fn(c)
            if res is not None:
                return res
    return _decide_heuristic(c)
