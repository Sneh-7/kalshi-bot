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
import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import feedparser
import httpx
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import session_scope
from ..models import NewsItem

log = logging.getLogger("scout")

# Browser-like UA — Truth Social's public endpoints reject some default agents.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]+>")

TRUTH_SOURCE = "TruthSocial"

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


# --- Truth Social --------------------------------------------------------


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_truth_free() -> List[RawNews]:
    """Read a user's public statuses via Truth Social's Mastodon-compatible API.

    Public timelines need no auth. Flow: resolve the account id by handle, then
    pull recent statuses. Unofficial and can break — hence the health streak and
    the paid fallback.
    """
    base = settings.truth_social_base_url.rstrip("/")
    user = settings.truth_social_username
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    out: List[RawNews] = []
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as c:
        lk = await c.get(f"{base}/accounts/lookup", params={"acct": user})
        lk.raise_for_status()
        acct_id = (lk.json() or {}).get("id")
        if not acct_id:
            return []
        r = await c.get(
            f"{base}/accounts/{acct_id}/statuses",
            params={"limit": settings.truth_social_max_posts, "exclude_replies": "true"},
        )
        r.raise_for_status()
        for st in r.json() or []:
            text = _strip_html(st.get("content", ""))
            if not text:
                continue
            url = st.get("url") or f"truthsocial:{st.get('id')}"
            out.append(
                RawNews(
                    source=TRUTH_SOURCE,
                    url=url,
                    title=f"Trump (Truth Social): {text[:180]}",
                    summary=text[:4000],
                    published_at=_parse_iso(st.get("created_at")),
                )
            )
    return out


async def _fetch_truth_paid() -> List[RawNews]:
    """Fallback via a paid scraper API. Expects a JSON array (or {posts:[...]})
    of objects with content/text + url + created_at fields."""
    if not settings.truth_social_api_url:
        return []
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if settings.truth_social_api_key:
        headers["Authorization"] = f"Bearer {settings.truth_social_api_key}"
    async with httpx.AsyncClient(timeout=25.0, headers=headers) as c:
        r = await c.get(
            settings.truth_social_api_url,
            params={"handle": settings.truth_social_username,
                    "limit": settings.truth_social_max_posts},
        )
        r.raise_for_status()
        payload = r.json()
    rows = payload.get("posts", payload) if isinstance(payload, dict) else payload
    out: List[RawNews] = []
    for st in rows or []:
        text = _strip_html(st.get("content") or st.get("text") or "")
        if not text:
            continue
        url = st.get("url") or st.get("uri") or f"truthsocial:{st.get('id')}"
        out.append(
            RawNews(
                source=TRUTH_SOURCE,
                url=url,
                title=f"Trump (Truth Social): {text[:180]}",
                summary=text[:4000],
                published_at=_parse_iso(st.get("created_at") or st.get("timestamp")),
            )
        )
    return out


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _fetch_truth_social() -> List[RawNews]:
    """Truth Social posts with free->paid failover and health tracking."""
    if not settings.truth_social_enabled:
        return []
    key = "truth_social"
    items: List[RawNews] = []
    prefer_paid = settings.truth_social_provider == "paid"

    order = (_fetch_truth_paid, _fetch_truth_free) if prefer_paid else (
        _fetch_truth_free, _fetch_truth_paid
    )
    for fn in order:
        try:
            items = await fn()
        except Exception as e:
            log.warning("Truth Social (%s) failed: %s", fn.__name__, e)
            items = []
        if items:
            break

    if items:
        _empty_streak[key] = 0
    else:
        _empty_streak[key] += 1
        if _empty_streak[key] >= settings.source_dead_after_cycles:
            log.error(
                "SOURCE DEAD: Truth Social returned zero posts for %d cycles. "
                "The free scrape may be blocked — configure TRUTH_SOCIAL_API_URL "
                "for the paid fallback.",
                _empty_streak[key],
            )
    return items


def recent_truth_social(limit: int = 8) -> List[str]:
    """Most recent Trump Truth Social post texts, newest first — analyst context."""
    with session_scope() as s:
        rows = (
            s.query(NewsItem)
            .filter(NewsItem.source == TRUTH_SOURCE)
            .order_by(desc(NewsItem.published_at))
            .limit(limit)
            .all()
        )
        return [r.summary or r.title for r in rows]


async def ingest_once() -> List[NewsItem]:
    """One ingestion pass. Returns newly inserted items only."""
    groups = await asyncio.gather(
        *[_fetch_rss(u) for u in settings.rss_list], return_exceptions=True
    )

    rss_items: List[RawNews] = []
    for g in groups:
        if isinstance(g, list):
            rss_items.extend(g)
        else:
            log.warning("Source raised: %s", g)

    # RSS is keyword-filtered for relevance; Truth Social posts are not — a Trump
    # post is inherently relevant to "what will Trump say" markets even when it
    # contains none of the watch keywords.
    kws = settings.keyword_list
    if kws:
        rss_items = [
            n for n in rss_items
            if any(k in f"{n.title} {n.summary}".lower() for k in kws)
        ]

    truth_items: List[RawNews] = []
    if settings.truth_social_enabled:
        try:
            truth_items = await _fetch_truth_social()
        except Exception:
            log.exception("Truth Social ingestion failed")

    items = rss_items + truth_items
    if not items:
        log.error(
            "Ingestion produced ZERO items across %d feeds + Truth Social",
            len(settings.rss_list),
        )
        return []

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
