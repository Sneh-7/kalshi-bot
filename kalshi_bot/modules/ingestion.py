"""The Scout — pulls news from free, public RSS feeds.

RSS is the primary source by design, not as a fallback: no key, no quota, no
commercial-use restriction. Most commercial news APIs' free tiers are unusable
for an always-on bot (NewsAPI's free tier delays articles 24 hours and forbids
commercial use, which disqualifies it for trading outright).

Stateless and I/O-bound: pull, dedupe by URL, hand raw text downstream. NO
interpretation happens here.

Adds source-health tracking, which the source implementation lacked. A dead
feed there was caught, logged at WARNING, and returned []. The agent kept
running with fewer sources and never told anyone — the most likely way this
system rots in production.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import feedparser
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import session_scope
from ..models import NewsItem

log = logging.getLogger("scout")

# feed url -> consecutive cycles returning zero items
_empty_streak: Dict[str, int] = defaultdict(int)


@dataclass
class RawNews:
    source: str
    url: str
    title: str
    summary: str
    published_at: Optional[datetime]


def _parse_struct_time(st) -> Optional[datetime]:
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _fetch_rss(url: str) -> List[RawNews]:
    """feedparser is sync but cheap; offload to a thread."""

    def _parse() -> List[RawNews]:
        feed = feedparser.parse(url)
        host = (feed.feed.get("title") if feed.feed else None) or url
        out: List[RawNews] = []
        for entry in feed.entries[:25]:
            out.append(
                RawNews(
                    source=str(host)[:64],
                    url=entry.get("link", ""),
                    title=(entry.get("title", "") or "")[:1024],
                    summary=(entry.get("summary", "") or entry.get("description", "") or "")[:4000],
                    published_at=_parse_struct_time(
                        entry.get("published_parsed") or entry.get("updated_parsed")
                    ),
                )
            )
        return [r for r in out if r.url and r.title]

    try:
        items = await asyncio.to_thread(_parse)
    except Exception as e:
        log.warning("RSS fetch failed %s: %s", url, e)
        items = []

    if items:
        _empty_streak[url] = 0
    else:
        _empty_streak[url] += 1
        if _empty_streak[url] >= settings.source_dead_after_cycles:
            # Loud, because silent source rot is indistinguishable from
            # "quiet news day" unless you say so explicitly.
            log.error(
                "SOURCE DEAD: %s has returned zero items for %d consecutive cycles",
                url,
                _empty_streak[url],
            )
    return items


def source_health() -> Dict[str, int]:
    """Feed URL -> consecutive empty cycles. 0 means healthy."""
    return dict(_empty_streak)


async def ingest_once() -> List[NewsItem]:
    """One ingestion pass. Returns newly inserted items only."""
    groups = await asyncio.gather(
        *[_fetch_rss(u) for u in settings.rss_list], return_exceptions=True
    )

    items: List[RawNews] = []
    for g in groups:
        if isinstance(g, list):
            items.extend(g)
        else:
            log.warning("Source raised: %s", g)

    if not items:
        log.error("Ingestion produced ZERO items across all %d feeds", len(settings.rss_list))
        return []

    kws = settings.keyword_list
    if kws:
        items = [
            n for n in items
            if any(k in f"{n.title} {n.summary}".lower() for k in kws)
        ]

    inserted: List[NewsItem] = []
    with session_scope() as s:
        for n in items:
            row = NewsItem(
                source=n.source,
                url=n.url,
                title=n.title,
                summary=n.summary,
                published_at=n.published_at,
            )
            s.add(row)
            try:
                s.flush()
                inserted.append(row)
            except IntegrityError:
                s.rollback()          # duplicate URL — already seen
                continue
        for row in inserted:
            s.refresh(row)
        s.expunge_all()

    if inserted:
        log.info("Scout: %d new items", len(inserted))
    return inserted
