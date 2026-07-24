"""The Quant — turns text into a calibrated probability.

The LLM is used ONLY as an NLP classifier. It labels whether a headline makes
the YES side of a related binary market more likely, less likely, or neutral.
The probability itself comes from deterministic Bayesian math.

This split is deliberate. LLMs are poorly calibrated probability estimators —
ask one for "the probability" and you get a confident number with no frequency
guarantee behind it. Keeping the arithmetic outside the model makes the system
auditable and tunable against real outcomes.

Providers tried in order: Groq -> OpenAI -> Anthropic -> keyword heuristic.
The heuristic means the agent runs with zero API keys, which is also the
baseline the LLM has to beat to justify its cost.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import httpx

from ..config import settings

log = logging.getLogger("quant")


@dataclass
class Extraction:
    sentiment: str        # bullish | bearish | neutral
    confidence: float     # 0..1, confidence in the LABEL only
    topic: str
    entities: List[str]
    rationale: str
    provider: str


SYSTEM_PROMPT = (
    "You are a strict JSON extractor for a prediction-market trading agent. "
    "Given a news headline, label whether it makes the related YES outcome "
    "more likely, less likely, or neutral. "
    "Examples of YES outcomes: 'Will Trump say X?', 'Will the Fed hike?'.\n"
    "Output a single JSON object with keys: "
    "sentiment (bullish|bearish|neutral), "
    "confidence (float 0-1, your confidence in this LABEL only), "
    "topic (short uppercase tag like TRUMP, FED, UKRAINE, ELECTIONS), "
    "entities (array of relevant people/orgs), "
    "rationale (one short sentence).\n"
    "Return ONLY the JSON, no prose."
)


def _user_msg(title: str, summary: str) -> str:
    return f"HEADLINE: {title}\n\nBODY:\n{summary[:1500]}"


def _safe_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        return json.loads(text[a : b + 1])
    except Exception:
        return None


def _normalize(d: dict, provider: str) -> Extraction:
    sent = str(d.get("sentiment", "neutral")).lower().strip()
    if sent not in {"bullish", "bearish", "neutral"}:
        sent = "neutral"
    try:
        conf = float(d.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    ents = d.get("entities") or []
    if isinstance(ents, str):
        ents = [ents]
    return Extraction(
        sentiment=sent,
        confidence=max(0.0, min(1.0, conf)),
        topic=(str(d.get("topic", "GEN"))[:32].upper() or "GEN"),
        entities=[str(x)[:48] for x in ents][:8],
        rationale=str(d.get("rationale", ""))[:512],
        provider=provider,
    )


async def _call_groq(c: httpx.AsyncClient, title: str, summary: str) -> Optional[Extraction]:
    if not settings.groq_api_key:
        return None
    try:
        r = await c.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": settings.groq_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_msg(title, summary)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 300,
            },
            timeout=20.0,
        )
        r.raise_for_status()
        data = _safe_json(r.json()["choices"][0]["message"]["content"])
        return _normalize(data, "groq") if data else None
    except Exception as e:
        log.warning("Groq call failed: %s", e)
        return None


async def _call_openai(c: httpx.AsyncClient, title: str, summary: str) -> Optional[Extraction]:
    if not settings.openai_api_key:
        return None
    try:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_msg(title, summary)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=25.0,
        )
        r.raise_for_status()
        data = _safe_json(r.json()["choices"][0]["message"]["content"])
        return _normalize(data, "openai") if data else None
    except Exception as e:
        log.warning("OpenAI call failed: %s", e)
        return None


async def _call_anthropic(c: httpx.AsyncClient, title: str, summary: str) -> Optional[Extraction]:
    if not settings.anthropic_api_key:
        return None
    try:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": settings.anthropic_model,
                "max_tokens": 400,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": _user_msg(title, summary)}],
            },
            timeout=25.0,
        )
        r.raise_for_status()
        data = _safe_json(r.json()["content"][0]["text"])
        return _normalize(data, "anthropic") if data else None
    except Exception as e:
        log.warning("Anthropic call failed: %s", e)
        return None


# --- Heuristic fallback: works with zero API keys ------------------------

_BULL = {
    "will", "plan", "plans", "announce", "announces", "announced", "says",
    "said", "speech", "statement", "warns", "threatens", "calls for",
    "demands", "unveil", "agreement", "deal", "sanctions", "tariff",
    "rate cut", "rate hike", "stimulus", "surge", "approve", "approved",
    "passes", "signed", "executive order",
}
_BEAR = {
    "won't", "will not", "no plan", "denies", "denied", "refuses", "rejects",
    "rejected", "walks back", "downplays", "unlikely", "no evidence",
    "not considering", "backs off", "retreats", "delays", "delayed",
    "postponed", "cancels", "cancelled", "fades", "cooling",
}
_TOPICS = [
    ("TRUMP", ("trump", "donald trump", "president trump")),
    ("TARIFF", ("tariff", "tariffs", "trade war")),
    ("FED", ("fed", "fomc", "powell", "rate cut", "rate hike", "interest rates")),
    ("UKRAINE", ("ukraine", "zelensky", "putin", "russia")),
    ("ISRAEL", ("israel", "gaza", "palestine", "hamas", "netanyahu")),
    ("ELECTIONS", ("election", "vote", "ballot", "polls", "campaign")),
    ("OIL", ("oil", "opec", "crude")),
    ("BTC", ("bitcoin", "btc", "crypto")),
    ("MACRO", ("inflation", "cpi", "ppi", "gdp", "jobs", "unemployment")),
    ("TECH", ("ai", "nvidia", "apple", "google", "microsoft", "semiconductor")),
]


def _heuristic(title: str, summary: str) -> Extraction:
    blob = f"{title} {summary}".lower()
    bull = sum(1 for w in _BULL if w in blob)
    bear = sum(1 for w in _BEAR if w in blob)
    if bull == bear:
        sent, conf = "neutral", 0.5
    elif bull > bear:
        sent, conf = "bullish", min(0.85, 0.5 + 0.1 * (bull - bear))
    else:
        sent, conf = "bearish", min(0.85, 0.5 + 0.1 * (bear - bull))

    topic = "GEN"
    for tag, keys in _TOPICS:
        if any(k in blob for k in keys):
            topic = tag
            break
    return Extraction(
        sentiment=sent,
        confidence=round(conf, 2),
        topic=topic,
        entities=[],
        rationale=f"Heuristic: {bull} bullish vs {bear} bearish keywords.",
        provider="heuristic",
    )


async def extract(title: str, summary: str) -> Extraction:
    async with httpx.AsyncClient() as client:
        for fn in (_call_groq, _call_openai, _call_anthropic):
            res = await fn(client, title, summary)
            if res is not None:
                return res
    return _heuristic(title, summary)


def bayesian_update(prior: float, sentiment: str, confidence: float) -> Tuple[float, float]:
    """Update a binary YES probability given a labeled news signal.

    Log-odds update. `likelihood_strength` controls how far one headline can
    move the estimate.

    WARNING: `likelihood_strength` (default 4.0) is NOT fitted — it was chosen,
    not measured. Until calibration data from resolved markets sets it, treat
    every posterior as having unknown scale. See docs/VALIDATION.md.
    """
    prior = min(max(prior, 1e-4), 1 - 1e-4)
    confidence = min(max(confidence, 0.0), 1.0)
    k = settings.likelihood_strength

    if sentiment == "bullish":
        lr = 1.0 + k * confidence
    elif sentiment == "bearish":
        lr = 1.0 / (1.0 + k * confidence)
    else:
        lr = 1.0

    log_odds = math.log(prior / (1 - prior)) + math.log(lr)
    posterior = 1 / (1 + math.exp(-log_odds))
    # Clamp: an extreme prior/LR combination can round to exactly 0.0 or 1.0,
    # which breaks edge math and blows up log() in log-loss scoring.
    posterior = min(max(posterior, 1e-4), 1 - 1e-4)
    return round(posterior, 4), round(lr, 4)
