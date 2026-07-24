# APIs — requirements, alternatives, and workarounds

**Verified 2026-07-24** against official documentation. Sources listed at the end.

> ⚠️ **Re-verify before implementing.** Kalshi's hostnames have changed more than
> once, and third-party sources actively disagree (`api.elections.kalshi.com` vs
> `external-api.kalshi.com` both appear in current write-ups). Treat
> <https://docs.kalshi.com> as the only authority, including over this document.

---

## Contents

1. [Summary: what you actually need](#summary-what-you-actually-need)
2. [Kalshi Trade API v2](#kalshi-trade-api-v2) — required
3. [News & sentiment sources](#news--sentiment-sources)
4. [LLM providers](#llm-providers)
5. [Infrastructure](#infrastructure)
6. [Cost scenarios](#cost-scenarios)
7. [Workarounds when an API is unavailable](#workarounds-when-an-api-is-unavailable)
8. [Sources](#sources)

---

## Summary: what you actually need

**To paper-trade — the phase you're in — the minimum viable stack is free.**

| Need | Minimum (free) | Better | Required? |
| --- | --- | --- | --- |
| Market data | Kalshi public REST (no auth) | Kalshi WebSocket | ✅ Required |
| Account/orders | Kalshi API key (demo) | — | Only for LIVE |
| News | RSS feeds | RSS + GDELT | ✅ Required |
| Sentiment | Built-in keyword heuristic | Groq free tier | ⬜ Optional |
| Storage | SQLite | Postgres (Neon free) | ✅ Required |
| Hosting | Local machine | Fly.io / Render / VPS | ✅ For always-on |

**You can run the entire paper-trading validation for $0.** Nothing below is
required to answer "does my algorithm make money." Paid services buy latency and
convenience, not correctness.

---

## Kalshi Trade API v2

**Required. No alternative exists** — Kalshi is the venue, and there is no third-party
mirror with order placement. (Read-only price data can be scraped or sourced from
aggregators, but you cannot trade through them.)

### Base URLs

```
Production REST   https://external-api.kalshi.com/trade-api/v2
Demo REST         https://external-api.demo.kalshi.co/trade-api/v2     ← .co
Production WS     wss://api.elections.kalshi.com/trade-api/ws/v2
Demo WS           wss://demo-api.kalshi.co/trade-api/ws/v2
```

> The WebSocket hosts come from third-party guides and use a *different* domain than
> the REST hosts. This is exactly the inconsistency flagged above — confirm both in
> the official docs before wiring them up.

**Use demo for all validation.** It is a full sandbox: real market structure,
simulated money.

### Authentication

API key ID + RSA private key. Three headers on every authenticated request:

```
KALSHI-ACCESS-KEY         your API key ID
KALSHI-ACCESS-TIMESTAMP   milliseconds since epoch
KALSHI-ACCESS-SIGNATURE   base64(RSA-PSS-SHA256(message))
```

The signed message is:

```
{timestamp_ms}{HTTP_METHOD}{path}
```

where **`path` includes the `/trade-api/v2` prefix and excludes the query string.**
The documented example is:

```
1703123456789GET/trade-api/v2/portfolio/balance
```

```python
message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
signature = private_key.sign(
    message,
    padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,   # 32 bytes
    ),
    hashes.SHA256(),
)
```

> 🔴 **This is the #1 bug in the sibling repo** — it signs `/markets` instead of
> `/trade-api/v2/markets`, so every authenticated call 401s. See
> [`ARCHITECTURE.md` defect 1](ARCHITECTURE.md#known-defects-in-the-existing-code).

**Getting keys:** Kalshi account → Profile → API Keys. The private key PEM is shown
**once** — save it immediately, outside the repo. `secrets/` and `.env` are already
gitignored here; this repository is public, so a committed key should be considered
compromised and rotated rather than scrubbed.

### Endpoints that matter

| Endpoint | Auth | Purpose |
| --- | :---: | --- |
| `GET /exchange/status` | ❌ | Is the exchange open |
| `GET /markets` | ❌ | Discovery; paginated via `cursor` |
| `GET /markets/{ticker}` | ❌ | Single market |
| `GET /markets/{ticker}/orderbook` | ❌ | Depth — **needed for realistic fills** |
| `GET /portfolio/balance` | ✅ | Cash |
| `GET /portfolio/positions` | ✅ | Open positions |
| `POST /portfolio/orders` | ✅ | Place order |
| `DELETE /portfolio/orders/{id}` | ✅ | Cancel |
| `GET /portfolio/fills` | ✅ | Executions — ground truth for real slippage |

Market data being public is genuinely useful: **you can build and validate the entire
data pipeline before creating any API key.**

### Rate limits

Token-bucket. Most calls cost **10 tokens**.

| Tier | Read tokens/s | Write tokens/s | Effective reads/s |
| --- | --: | --: | --: |
| **Basic** (new accounts) | 200 | 100 | ~20 |
| Advanced | 300 | 300 | ~30 |
| Expert | 600 | 600 | ~60 |
| Premier | 1,000 | 1,000 | ~100 |
| Paragon | 2,000 | 2,000 | ~200 |
| Prime | 4,000 | 4,000 | ~400 |
| Prestige | 6,000 | 8,000 | ~600 |

Basic tier — ~20 reads/sec — is **ample** for a sentiment agent polling ~20 markets
every 30 seconds. You will not approach these limits.

Two operational notes:

- **429s carry no `Retry-After` or `X-RateLimit-*` headers.** You must implement
  backoff blind. Use exponential backoff with jitter.
- **No penalty or cooldown** — the bucket just keeps refilling, so a 429 is
  recoverable and not an account risk.

Higher tiers are granted on trailing-30-day volume share. Irrelevant at your stage.

### Fees

```
taker = 0.07 × C × (1 − C) per contract      C = price in dollars
maker = 25% of taker
settlement = none
```

| Price | Taker/contract | Round trip (taker) |
| --- | --: | --: |
| $0.10 | 0.63¢ | 1.26¢ |
| $0.25 | 1.31¢ | 2.62¢ |
| **$0.50** | **1.75¢** | **3.50¢** |
| $0.75 | 1.31¢ | 2.62¢ |
| $0.90 | 0.63¢ | 1.26¢ |

**Fees peak at 50¢** — precisely where sentiment-driven uncertainty concentrates, and
[where the research says markets are best calibrated](VALIDATION.md), i.e. where edge
is hardest to find. You pay the most where you're least likely to have an edge.

**Holding to resolution costs nothing extra.** Enter once, pay once, settle free.
This structurally favors hold-to-resolution over in-and-out trading.

### WebSocket

Channels include `ticker`, `trade`, `orderbook_delta`, `fill`, `market_positions`,
`user_orders`, `market_lifecycle`. Auth happens during the handshake.

**Recommendation:** start with REST polling (simpler, adequate at 30s intervals),
move to `orderbook_delta` once you care about latency or are watching many markets.

### Python SDKs

There's a community SDK — `TexasCoding/kalshi-python-sdk` — with typed REST + 12
WebSocket `subscribe_*` methods.

**Recommendation: keep the hand-rolled client.** The sibling repo's `kalshi.py` is
322 lines and you must read it anyway to fix its bugs. A third-party SDK adds a
dependency whose maintenance you don't control, for a REST API this small. Use the
SDK as a *reference implementation* for the signing code — that's where it's most
valuable.

---

## News & sentiment sources

### RSS — free, unlimited, recommended primary

No key, no quota, no commercial restriction. Already implemented in
`ingestion.py` via `feedparser`.

Currently configured: Reuters, Politico, The Hill, AP Politics, FT, CBS Politics.

**Verify these still resolve** — Reuters in particular has restructured its feeds
repeatedly, and `feeds.reuters.com` has been unreliable. A dead feed fails silently
in the current code: `_fetch_rss` catches the exception, logs a warning, and returns
`[]`. Your agent keeps running with fewer sources and never tells you.

> **Add a source-health check.** Alert when a feed returns zero items for N
> consecutive cycles. Silent degradation is the most likely way this system rots.

Additional free feeds worth adding: NPR, BBC, Al Jazeera, CNBC, Federal Reserve
press releases, BLS release calendar (for economic-data markets).

### GDELT — free, recommended secondary

Global news database. Free, no key, 15-minute update cycle, 100+ countries, includes
tone/sentiment scoring and entity extraction, history to 1979.

**The historical archive is the standout feature** — it's the only free way to
backtest a news-driven strategy against past events. Steep learning curve and raw
output needs real processing, but nothing else free offers this.

### Commercial news APIs — mostly not worth it

| Service | Free tier | Verdict |
| --- | --- | --- |
| **NewsAPI.org** | 100 req/day, **localhost only**, **24h delay**, **no commercial use** | ❌ Disqualified — a 24h delay is useless for trading |
| **Currents** | ~600–1,000 req/day, commercial OK, no card | ✅ Best free tier if you need one |
| **GNews** | 100 req/day; paid from $84/mo | 🟡 Thin free tier |
| **MediaStack** | 500 req/**month** | ❌ Too small for always-on |
| **APITube** | 30 req/30 min; includes entities + sentiment | 🟡 Usable; enriched data is the draw |

**Recommendation: RSS + GDELT, skip the commercial APIs entirely for now.** They buy
convenience, not coverage you can't otherwise get, and none of their free tiers
support an always-on bot. Revisit only if you identify a specific source RSS can't
reach.

### Social media — high value, high friction

| Source | Status | Notes |
| --- | --- | --- |
| **Truth Social** | 🟡 Implemented, unreliable | Mastodon-compatible; public endpoints often blocked. Existing code tries two URLs then warns. |
| **X/Twitter** | 🔴 Expensive | API is ~$200/mo for meaningful access. Hard to justify pre-validation. |
| **Reddit** | ✅ Free | OAuth, generous limits. `r/politics`, `r/economics` for sentiment aggregate. |
| **Bluesky** | ✅ Free | Open AT Protocol, no meaningful gating. Growing political presence. |

**Workaround for X/Twitter:** don't pay. Major posts by market-moving figures get
reported by wire services within minutes, so RSS captures the *content* at a small
latency cost. You only need the firehose if your edge is explicitly speed — and if it
is, you're competing with colocated bots and should reconsider the premise.

---

## LLM providers

Used **only as a text classifier** (bullish/bearish/neutral + confidence). This is a
small, easy task — frontier models are not needed.

| Provider | Model in config | Why |
| --- | --- | --- |
| **Groq** (1st) | `llama-3.1-8b-instant` | Fast, cheap, generous free tier. Right default. |
| **OpenAI** (2nd) | `gpt-4o-mini` | Fallback |
| **Anthropic** (3rd) | `claude-haiku-4-5` | Fallback |
| **Heuristic** (4th) | keyword scoring | **Zero keys required** |

The heuristic fallback is more valuable than it looks: **the entire system runs with
no LLM keys at all.** Use it to validate plumbing end-to-end before spending
anything, and as a baseline — if the LLM doesn't beat keyword matching on calibration,
it isn't earning its cost.

Cost estimate: ~500 headlines/day × ~400 tokens ≈ 200k tokens/day. On Groq's free
tier, plausibly $0. On `gpt-4o-mini`, cents per day. **Sentiment classification is not
where your money goes.**

---

## Infrastructure

### Database

- **SQLite** — fine for local development. Zero setup.
- **Postgres** — needed for always-on. Neon's free tier is adequate.

> ⚠️ **On ephemeral hosts (Render free tier, most containers), SQLite is wiped on
> every restart.** For a validation run that must accumulate 200+ resolutions over
> weeks, that is fatal — you would silently lose your entire sample. Use Postgres for
> anything long-running.

The sibling repo has a known bug here too: it never normalizes `postgres://` →
`postgresql+psycopg2://`, so following its own Postgres advice fails at
`create_engine`. (Its `PORT-PLAN.md` item 5 documents the fix.)

### Hosting

See [`OPERATIONS.md`](OPERATIONS.md) for detail. Options: local + `launchd`, Fly.io,
Render, or a small VPS.

---

## Cost scenarios

| Scenario | Monthly |
| --- | --: |
| **Paper validation (recommended)** — RSS + GDELT + Groq free + SQLite/Neon + local or Fly free | **$0** |
| Always-on, durable — + small VPS/Fly paid + Neon free | ~$5–10 |
| Latency-focused — + X API + paid news | ~$250+ |

**Do the first one.** Nothing about answering "does this make money" requires
spending money. Buy latency only after you've demonstrated an edge that latency would
amplify.

---

## Workarounds when an API is unavailable

| Blocked | Workaround |
| --- | --- |
| **Kalshi auth failing** | Market data is public — build and test the whole pipeline unauthenticated. Only orders need keys. |
| **No Kalshi account yet** | Demo environment; public endpoints need nothing at all. |
| **429 rate limited** | Exponential backoff + jitter. No `Retry-After` header exists, so back off blind. No cooldown penalty. |
| **Truth Social blocked** | Fall back to RSS — wire services report significant posts within minutes. |
| **X/Twitter too expensive** | Skip it. RSS + Reddit + Bluesky cover the sentiment signal at a latency cost. |
| **News API quota exhausted** | RSS has no quota. Make RSS primary, APIs supplementary — never the reverse. |
| **RSS feed dead** | Multi-source by default; alert on zero-item feeds so failure isn't silent. |
| **All LLM keys missing/exhausted** | Built-in keyword heuristic. System degrades, doesn't stop. |
| **LLM returns malformed JSON** | `_safe_json()` already strips fences and extracts the object; falls through to heuristic. |
| **Kalshi WebSocket unstable** | Fall back to REST polling; reconcile state on reconnect. |
| **Host restarts / SQLite wiped** | Postgres. Non-negotiable for a multi-week run. |

The general principle already present in the codebase and worth preserving: **every
external dependency has a fallback, and the system degrades rather than halts.** The
one place to invert that is *trading* — there, [fail closed](ARCHITECTURE.md#risk-model).
Degrading data quality is acceptable; degrading order safety is not.

---

## Sources

Verified 2026-07-24.

- [Kalshi — Quick Start: Authenticated Requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [Kalshi — Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi — Quick Start: WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Kalshi Fees 2026 — pm.wiki](https://pm.wiki/learn/kalshi-fees-explained)
- [Kalshi API Guide 2026 — pm.wiki](https://pm.wiki/learn/kalshi-api)
- [Kalshi API Tutorial: Auth, WebSockets, Rate Limits & Orders — botforkalshi.com](https://www.botforkalshi.com/blog/kalshi-api-tutorial)
- [Kalshi Python SDK — TexasCoding](https://github.com/TexasCoding/kalshi-python-sdk)
- [Best Free News APIs in 2026 (With Honest Limitations) — APITube](https://apitube.io/blog/post/best-free-news-apis-honest-limitations)
- [Best News APIs 2026: NewsAPI vs Mediastack vs GDELT — DataResearchTools](https://dataresearchtools.com/best-news-apis-comparison/)
- [10 Best Free News APIs for Developers in 2026 — Toolpod](https://toolpod.dev/blog/10-best-free-news-apis-for-developers-in-2026)
