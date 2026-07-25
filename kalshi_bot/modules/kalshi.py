"""Kalshi Trade API v2 client.

RSA-PSS request signing, market discovery, order-book snapshots, portfolio
reads, order placement.

Docs: https://docs.kalshi.com  (verified 2026-07-24)

Two defects from the source implementation are fixed here:

1. SIGNING PATH. The signed message must be the full path from the API root,
   INCLUDING the `/trade-api/v2` prefix:

       1703123456789GET/trade-api/v2/portfolio/balance

   The source signed only `/portfolio/balance`, so every authenticated request
   failed signature verification. This was easy to miss because all market-data
   endpoints are public — discovery and orderbook reads work unauthenticated,
   and the failure only surfaces on the first order or balance call.

2. PRICE UNITS AND FIELD NAMES. The source read `yes_bid` / `yes_ask` and
   divided by 100 in one function while treating the same field as dollars in
   another, so one path could compute `1.0 - 62 = -61.0` — a negative implied
   probability flowing into edge math.

   Verified against the live API on 2026-07-24, the current shape is neither:

       market:    yes_bid_dollars = '0.1490'   (STRING, already dollars)
                  yes_ask_dollars = '0.1750'
                  no_bid_dollars  = '0.8250'
                  liquidity_dollars, volume_24h_fp, yes_bid_size_fp — all strings
       orderbook: {"orderbook_fp": {"yes_dollars": [["0.6850","33000.00"], ...],
                                    "no_dollars":  [...]}}

   There is NO `category` field on markets at all, so the source's category
   filter silently dropped 100% of markets. Filtering here is by title keyword
   and provisional status instead.

   Sanity check on the inversion: yes_bid 0.1490 with best no_bid 0.8250 gives
   yes_ask = 1 - 0.8250 = 0.1750, matching the reported yes_ask_dollars.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..config import settings

log = logging.getLogger("kalshi")


def to_dollars(value: Any, default: float = 0.0) -> float:
    """Parse a Kalshi `*_dollars` field.

    These arrive as STRINGS already denominated in dollars ('0.1490'), not as
    integer cents. Single conversion point so the unit never has to be guessed
    at a call site — which is precisely how the source ended up computing
    negative probabilities.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class BookLevel:
    price: float      # dollars, 0-1
    contracts: int


@dataclass
class OrderBook:
    """YES-side view of a Kalshi book, normalized to dollars."""

    best_bid: float = 0.0
    best_ask: float = 1.0
    bid_depth: int = 0          # contracts resting at best bid
    ask_depth: int = 0          # contracts available at best ask
    bids: List[BookLevel] = field(default_factory=list)
    asks: List[BookLevel] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return round((self.best_bid + self.best_ask) / 2.0, 4)

    @property
    def spread(self) -> float:
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def is_valid(self) -> bool:
        """A crossed or empty book means don't trade."""
        return 0.0 < self.best_bid < self.best_ask < 1.0


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    category: str
    event_ticker: str
    series_ticker: str
    status: str
    close_time: str
    volume_24h: float
    liquidity: float
    yes_bid: float           # dollars
    yes_ask: float           # dollars
    is_provisional: bool = False

    @property
    def yes_mid(self) -> float:
        return round((self.yes_bid + self.yes_ask) / 2.0, 4)

    @property
    def has_two_sided_quote(self) -> bool:
        return 0.0 < self.yes_bid < self.yes_ask < 1.0


@dataclass
class KalshiOrder:
    order_id: str
    status: str
    ticker: str
    side: str
    count: int
    price: float
    filled_count: int


class KalshiError(RuntimeError):
    """Base for Kalshi client failures."""


class KalshiAuthError(KalshiError):
    """Missing/invalid credentials, or a rejected signature."""


class KalshiClient:
    """Async Kalshi client.

    Safe to construct without credentials: market-data methods still work,
    authenticated methods raise KalshiAuthError rather than silently no-op'ing.
    Silent no-ops were a source-implementation habit that hides misconfiguration
    until it matters.
    """

    def __init__(self) -> None:
        self.base = settings.kalshi_base_url.rstrip("/")
        # e.g. "/trade-api/v2" — this is the prefix the source omitted when signing.
        self.path_prefix = urlparse(self.base).path.rstrip("/")
        self.key_id = settings.kalshi_key_id
        self.private_key_path = settings.kalshi_private_key_path
        self._private_key: Optional[Any] = None

    # -- auth -------------------------------------------------------------

    @property
    def has_credentials(self) -> bool:
        return bool(self.key_id and self.private_key_path)

    def _load_key(self) -> Optional[Any]:
        if self._private_key is None and self.private_key_path:
            try:
                pem = Path(self.private_key_path).expanduser().read_bytes()
                self._private_key = serialization.load_pem_private_key(
                    pem, password=None, backend=default_backend()
                )
            except Exception as e:
                raise KalshiAuthError(
                    f"Could not load Kalshi private key from "
                    f"{self.private_key_path!r}: {e}"
                ) from e
        return self._private_key

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        """Signed headers for an authenticated request.

        `path` is the route below the API root (e.g. "/portfolio/balance").
        The signed message uses the FULL path including the version prefix.
        """
        if not self.has_credentials:
            raise KalshiAuthError(
                "Kalshi credentials not configured. Set KALSHI_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH. Market-data endpoints need no auth."
            )
        key = self._load_key()
        ts = str(int(time.time() * 1000))

        # THE FIX: include self.path_prefix. Signing "/portfolio/balance"
        # instead of "/trade-api/v2/portfolio/balance" yields a 401 on every
        # authenticated call.
        full_path = f"{self.path_prefix}{path}"
        message = f"{ts}{method.upper()}{full_path}".encode("utf-8")

        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    # -- transport --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        authenticated: bool = False,
        max_retries: int = 3,
    ) -> Any:
        url = f"{self.base}{path}"
        headers: Dict[str, str] = {}
        if authenticated:
            headers.update(self._sign(method, path))
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        delay = 0.5
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.request(
                        method, url, params=params, json=json_body, headers=headers
                    )
                if r.status_code == 429:
                    # Kalshi sends no Retry-After / X-RateLimit-* headers, so
                    # back off blind. There is no cooldown penalty.
                    log.warning("Rate limited on %s %s; backing off %.1fs", method, path, delay)
                    await _sleep(delay)
                    delay *= 2
                    continue
                if r.status_code in (401, 403):
                    raise KalshiAuthError(
                        f"Kalshi rejected credentials on {method} {path} "
                        f"({r.status_code}). Check that the signed path includes "
                        f"{self.path_prefix!r}. Body: {r.text[:300]}"
                    )
                r.raise_for_status()
                return r.json()
            except KalshiAuthError:
                raise
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600 and attempt < max_retries - 1:
                    await _sleep(delay)
                    delay *= 2
                    last_exc = e
                    continue
                raise KalshiError(
                    f"Kalshi {method} {path} failed "
                    f"{e.response.status_code}: {e.response.text[:300]}"
                ) from e
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < max_retries - 1:
                    await _sleep(delay)
                    delay *= 2
                    continue
                raise KalshiError(f"Kalshi {method} {path} transport error: {e}") from e
        raise KalshiError(f"Kalshi {method} {path} exhausted retries: {last_exc}")

    # -- public market data ----------------------------------------------

    async def get_exchange_status(self) -> Dict[str, Any]:
        return await self._request("GET", "/exchange/status")

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
        tickers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if tickers:
            params["tickers"] = ",".join(tickers)
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Single market, including settlement `status` and `result`.

        Response shape: {"market": {..., "status": "settled"|"active"|...,
        "result": "yes"|"no"|""}}. Used by the settlement poller to learn ground
        truth once a market resolves.
        """
        data = await self._request("GET", f"/markets/{ticker}")
        return (data or {}).get("market", data) or {}

    async def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        """Normalized YES-side book.

        Kalshi returns {"orderbook": {"yes": [[price_cents, count], ...],
                                      "no":  [[price_cents, count], ...]}}

        Both arrays are BIDS. A NO bid at p cents implies a YES ask at
        (100 - p) cents — that inversion is why mixing units here produced
        negative probabilities in the source.
        """
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        # Current shape is `orderbook_fp` with `yes_dollars` / `no_dollars`.
        # The older `orderbook` / `yes` / `no` keys are accepted as a fallback
        # so a rollback on Kalshi's side doesn't silently zero out every book.
        book = (data or {}).get("orderbook_fp") or (data or {}).get("orderbook") or {}
        yes_rows = book.get("yes_dollars") or book.get("yes") or []
        no_rows = book.get("no_dollars") or book.get("no") or []

        bids: List[BookLevel] = []
        for row in yes_rows:
            price, size = _row(row)
            if price is None:
                continue
            bids.append(BookLevel(price=price, contracts=size))

        asks: List[BookLevel] = []
        for row in no_rows:
            price, size = _row(row)
            if price is None:
                continue
            # A NO bid at $p implies a YES ask at $(1 - p).
            asks.append(BookLevel(price=round(1.0 - price, 4), contracts=size))

        bids.sort(key=lambda l: l.price, reverse=True)   # best bid = highest
        asks.sort(key=lambda l: l.price)                 # best ask = lowest

        ob = OrderBook(bids=bids, asks=asks)
        if bids:
            ob.best_bid = bids[0].price
            ob.bid_depth = bids[0].contracts
        if asks:
            ob.best_ask = asks[0].price
            ob.ask_depth = asks[0].contracts
        return ob

    async def fetch_open_markets(self, max_pages: int = 8) -> List[KalshiMarket]:
        """Open, tradeable markets ranked by keyword relevance.

        Filtering is by title keyword and quote quality, NOT category — the
        markets endpoint returns no category field, so the source's category
        filter dropped every market while looking like it worked.

        Provisional multivariate markets dominate the `status=open` list and
        are almost all unquoted: a scan of 1,200 open markets on 2026-07-24
        found exactly ONE with a two-sided quote. Filtering them out is what
        makes the remainder usable.
        """
        out: List[KalshiMarket] = []
        cursor: Optional[str] = None
        seen = 0
        excluded = settings.exclude_keyword_list

        for _ in range(max_pages):
            data = await self.get_markets(cursor=cursor)
            page = data.get("markets", [])
            seen += len(page)
            for m in page:
                mk = self._parse_market(m)
                if mk is None:
                    continue
                if settings.exclude_provisional and mk.is_provisional:
                    continue
                if settings.require_two_sided_quote and not mk.has_two_sided_quote:
                    continue
                # Drop out-of-scope categories (crypto) by title/ticker match.
                if excluded:
                    blob = f"{mk.title} {mk.event_ticker} {mk.series_ticker} {mk.ticker}".lower()
                    if any(k in blob for k in excluded):
                        continue
                out.append(mk)
            cursor = data.get("cursor")
            if not cursor:
                break

        if seen and not out:
            log.warning(
                "Scanned %d open markets, none tradeable "
                "(provisional=%s, two_sided=%s). Filters may be too strict.",
                seen, settings.exclude_provisional, settings.require_two_sided_quote,
            )

        watch = settings.watch_list
        if watch:
            have = {m.ticker for m in out}
            missing = [t for t in watch if t not in have]
            if missing:
                data = await self.get_markets(tickers=missing)
                for m in data.get("markets", []):
                    mk = self._parse_market(m)
                    if mk is not None:
                        out.append(mk)
            out = [m for m in out if m.ticker in set(watch)]

        kws = settings.keyword_list
        if kws:
            def score(m: KalshiMarket) -> int:
                blob = f"{m.title} {m.event_ticker} {m.series_ticker}".lower()
                return sum(1 for k in kws if k in blob)
            out.sort(key=score, reverse=True)

        return out[: settings.max_markets]

    @staticmethod
    def _parse_market(m: Dict[str, Any]) -> Optional[KalshiMarket]:
        ticker = str(m.get("ticker") or "")
        if not ticker:
            return None
        # `*_dollars` fields are strings already in dollars. The bare
        # `yes_bid`/`yes_ask` names are accepted as a legacy fallback.
        yes_bid = to_dollars(m.get("yes_bid_dollars", m.get("yes_bid")), 0.0)
        yes_ask = to_dollars(m.get("yes_ask_dollars", m.get("yes_ask")), 1.0)
        return KalshiMarket(
            ticker=ticker,
            title=str(m.get("title") or "")[:1024],
            # No `category` field exists on the markets endpoint.
            category="",
            event_ticker=str(m.get("event_ticker") or ""),
            series_ticker=str(m.get("series_ticker") or ""),
            status=str(m.get("status") or ""),
            close_time=str(m.get("close_time") or m.get("expiration_time") or ""),
            volume_24h=to_dollars(m.get("volume_24h_fp", m.get("volume_24h"))),
            liquidity=to_dollars(m.get("liquidity_dollars", m.get("liquidity"))),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            is_provisional=bool(m.get("is_provisional", False)),
        )

    # -- authenticated ----------------------------------------------------

    async def get_balance(self) -> Dict[str, Any]:
        return await self._request("GET", "/portfolio/balance", authenticated=True)

    async def get_positions(self) -> List[Dict[str, Any]]:
        resp = await self._request(
            "GET", "/portfolio/positions", params={"limit": 1000}, authenticated=True
        )
        return resp.get("market_positions", resp.get("positions", [])) if isinstance(resp, dict) else []

    async def get_fills(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Executions — ground truth for measuring real slippage vs. the simulator."""
        resp = await self._request(
            "GET", "/portfolio/fills", params={"limit": limit}, authenticated=True
        )
        return resp.get("fills", []) if isinstance(resp, dict) else []

    async def create_order(
        self,
        ticker: str,
        side: str,                  # "yes" | "no"
        count: int,
        price_cents: int,
        action: str = "buy",
        time_in_force: str = "fill_or_kill",
        client_order_id: Optional[str] = None,
    ) -> KalshiOrder:
        """Place a limit order. Prices are integer cents.

        Note: a fill-or-kill at price 99 emulates a market order but is always
        a TAKER, costing 4x the maker fee. That tradeoff is deliberate here
        (certainty of fill) but should be revisited if fees start to dominate.
        """
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents must be 1-99, got {price_cents}")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        body: Dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side.lower(),
            "type": "limit",
            "count": int(count),
            "time_in_force": time_in_force,
        }
        if side.lower() == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        if client_order_id:
            body["client_order_id"] = client_order_id

        resp = await self._request(
            "POST", "/portfolio/orders", json_body=body, authenticated=True
        )
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        return KalshiOrder(
            order_id=str(order.get("order_id", "")),
            status=str(order.get("status", "")),
            ticker=ticker,
            side=side,
            count=int(order.get("count", count) or count),
            price=cents_to_dollars(price_cents),
            filled_count=int(order.get("filled_count", 0) or 0),
        )


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def _row(row: Any) -> Tuple[Optional[float], int]:
    """Parse one [price, size] book level.

    Both entries arrive as dollar-denominated strings, e.g. ["0.6850", "33000.00"].
    """
    try:
        price, size = row[0], row[1]
        return float(price), int(float(size))
    except (TypeError, ValueError, IndexError):
        return None, 0


kalshi = KalshiClient()
