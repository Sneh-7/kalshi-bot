# Architecture — Kalshi sentiment trading agent

**Researched and written 2026-07-24.** API details verified against Kalshi's official
docs on that date; see [`APIS.md`](APIS.md) for sources and re-verification notes.

> ## Status: design document. Nothing in *this* repo is implemented.
>
> This repo (`kalshi-bot`) is still a scaffold — `main.py` prints `Hello, World!`.
>
> **However, a working implementation of almost exactly this system already exists**
> in the sibling checkout `polymarket-sentiment-agent/`, and it already targets
> Kalshi. The central recommendation of this document is therefore **port and fix
> that code rather than write this from scratch.** See [Build strategy](#build-strategy).

---

## Contents

1. [The actual goal](#the-actual-goal)
2. [Build strategy: port, don't rewrite](#build-strategy)
3. [System design](#system-design)
4. [The pipeline stage by stage](#the-pipeline-stage-by-stage)
5. [Edge math — where fees change everything](#edge-math)
6. [Known defects in the existing code](#known-defects-in-the-existing-code)
7. [Risk model](#risk-model)
8. [What "does it make money" requires](#what-does-it-make-money-requires)
9. [Build order](#build-order)

---

## The actual goal

Your stated aim, restated precisely because it determines the whole design:

> Run a news-sentiment agent against Kalshi continuously, paper-trade it first, and
> find out **whether the algorithm would actually have made money** versus the real
> Kalshi market.

That last clause is the hard part, and it is not primarily an engineering problem.
Building a bot that places plausible paper trades is a weekend. Building one whose
paper P&L *predicts* live P&L is the real work, and it is where nearly every
retail trading bot quietly fails.

The single most important design consequence: **the paper trader must model costs
that don't exist in paper.** A simulator that fills at the midpoint with no fees will
report profits from a strategy that loses money live. This is not a rounding error —
[quantified below](#edge-math), it is roughly 3–4¢ per contract on a $1 contract,
which is larger than most realistic sentiment edges.

## Build strategy

**Recommendation: port `polymarket-sentiment-agent/`, don't start over.**

That checkout is ~2,600 lines of Python implementing this exact pipeline, and
despite its name it is **already pointed at Kalshi** — `modules/kalshi.py` is a
322-line Kalshi Trade API v2 client, and `config.py` has `KALSHI_*` settings and a
`PAPER | RECOMMEND | LIVE` mode switch.

What already exists there:

| Module | Lines | What it does |
| --- | --: | --- |
| `modules/kalshi.py` | 322 | Kalshi v2 client: RSA-PSS signing, markets, orderbook, orders |
| `orchestrator.py` | 284 | The main loop (Scout → Quant → Oracle → Overseer → Trader) |
| `modules/execution.py` | 281 | Paper fills, recommendations, live order placement |
| `modules/intelligence.py` | 280 | LLM sentiment extraction + Bayesian probability update |
| `modules/track_record.py` | 262 | Brier score, log-loss, calibration bins |
| `modules/ingestion.py` | 220 | RSS + CryptoPanic + Truth Social news pulling |
| `modules/risk.py` | 155 | Kill switch, drawdown, position limits |
| `modules/market.py` | 50 | Market snapshot persistence |

Plus a FastAPI app, a React dashboard, a test suite, CI, and a `render.yaml`.

**Licensing is fine.** It's MIT (© 2026 Priyansh Shah), so you may reuse it in your
own repo provided you keep the copyright notice and license text. Add a
`NOTICE`/attribution line to this repo when you port.

**But it has real bugs**, documented in [Known defects](#known-defects-in-the-existing-code).
Two of them will stop it from working at all against Kalshi, and two will make your
paper results wrong in the optimistic direction. Port it *and fix them* — porting it
unexamined would give you a system that looks like it works and reports profits it
cannot achieve.

## System design

The existing five-stage pipeline is sound. Keep it.

```
   ┌──────────────────────────────────────────────────────────┐
   │  SCOUT — ingestion.py                                    │
   │  RSS feeds, news APIs, social posts                      │
   │  Dedupe by URL. NO interpretation here.                  │
   └───────────────────────┬──────────────────────────────────┘
                           │ raw headlines
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │  QUANT — intelligence.py                                 │
   │  LLM labels sentiment (bullish/bearish/neutral + conf)   │
   │  Deterministic Bayes converts label → probability        │
   │  Fallback chain: Groq → OpenAI → Anthropic → heuristic   │
   └───────────────────────┬──────────────────────────────────┘
                           │ p_model (our probability)
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │  ORACLE — market.py / kalshi.py                          │
   │  Fetch live Kalshi prices + order book                   │
   │  Snapshot everything for audit                           │
   └───────────────────────┬──────────────────────────────────┘
                           │ p_market, bid, ask, depth
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │  EDGE — the decision                                     │
   │  edge = p_model − p_market − costs                       │
   │  ◄── THIS IS WHERE FEES MUST ENTER. They currently don't.│
   └───────────────────────┬──────────────────────────────────┘
                           │ candidate trade
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │  OVERSEER — risk.py                                      │
   │  Kill switch, position caps, daily drawdown              │
   │  Can reject or resize. Cannot be bypassed.               │
   └───────────────────────┬──────────────────────────────────┘
                           │ approved plan
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │  TRADER — execution.py                                   │
   │  PAPER (default) │ RECOMMEND │ LIVE                      │
   └───────────────────────┬──────────────────────────────────┘
                           │
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │  TRACK RECORD — track_record.py                          │
   │  On resolution: Brier, log-loss, calibration             │
   │  ◄── the thing that answers "am I making money"          │
   └──────────────────────────────────────────────────────────┘
```

**The key architectural property** is that the LLM is used *only as a text
classifier*, never as a probability estimator. It answers "does this headline make
YES more likely?" and the Bayesian update turns that label into a number
deterministically. This matters: LLMs are poorly calibrated probability estimators —
ask one for "the probability" and you get a confident-sounding number with no
frequency guarantee behind it. Keeping the math outside the model makes the system
auditable and tunable.

The existing `bayesian_update` is a clean log-odds update:

```python
lr = 1.0 + 4.0 * confidence          # bullish
lr = 1.0 / (1.0 + 4.0 * confidence)  # bearish
log_odds = log(prior/(1-prior)) + log(lr)
posterior = 1 / (1 + exp(-log_odds))
```

The `4.0` is a free parameter controlling how strongly one headline moves your
estimate. **It is currently unjustified** — it was chosen, not fitted. Calibration
data (from the track-record system) is what should set it.

## The pipeline stage by stage

### Scout — news ingestion

Current sources: RSS (Reuters, Politico, The Hill, AP, FT, CBS), CryptoPanic
(optional key), Truth Social (best-effort, frequently blocked).

**RSS is the right primary source** and this is a deliberate recommendation, not a
fallback. It is free, unlimited, has no API key, no quota, and no commercial-use
restriction. Most commercial news APIs' free tiers are unusable for an always-on
bot — NewsAPI.org's free tier is localhost-only with 24-hour-delayed articles and
forbids commercial use, which disqualifies it outright for live trading.

Full source comparison, quotas, and fallbacks: [`APIS.md`](APIS.md).

**Latency is the thing that matters here.** If your edge is "react to news faster
than the market reprices," RSS polling every 30s is likely too slow — Kalshi's
active markets have bots on them. If your edge is "the market overreacts to
sentiment and mean-reverts," latency matters much less. **Decide which claim you're
making**, because it changes the entire data-source budget. See
[`VALIDATION.md`](VALIDATION.md).

### Quant — sentiment → probability

Provider chain Groq → OpenAI → Anthropic → keyword heuristic. The heuristic fallback
means **the system runs with zero API keys**, which is genuinely valuable for testing
the plumbing without spending anything.

Groq first is a sensible default: it's fast and cheap for a classification task this
small. The task is a single JSON label — it does not need a frontier model.

**The weak link is market↔news matching.** Currently a headline is matched to markets
by keyword overlap. A headline about "Fed rate cut" and a market titled "Will the Fed
cut rates in September?" match on keywords, but so does "Will Powell resign?" —
which the same headline says nothing about. Bad matching produces confident signals
on unrelated markets, and that is a *silent* failure: it looks like a working
pipeline producing trades. Improving this matching is likely higher-value than any
model upgrade.

### Oracle — Kalshi market data

Public endpoints (markets, orderbook) need no auth. Portfolio and orders do.

For an always-on agent, **prefer the WebSocket** (`orderbook_delta`, `ticker`) over
REST polling once you're past prototyping — it's lower latency and far cheaper
against rate limits. REST polling is fine to start.

### Overseer — risk

Existing gates: kill switch (DB-backed, survives restart), edge threshold, max size
per trade, max open positions, daily drawdown auto-tripping the kill switch. This
layer is well-built and its design — reject or resize, never bypassable — is right.

### Trader — execution

Three modes. `RECOMMEND` (default in the existing config) writes a pending
recommendation you approve manually. That's a good default for building trust, but
**`PAPER` is what you want for the validation question**, since it runs unattended
and produces a continuous record.

## Edge math

This section is the most important one in this document.

### Kalshi's fee formula

Verified 2026-07-24:

```
taker fee per contract = 0.07 × C × (1 − C)     where C = price in dollars (0.01–0.99)
maker fee per contract = 25% of the taker fee
settlement fee         = none
```

Worked, at C = $0.50 (maximum uncertainty, where fees peak):

| | Per contract |
| --- | --- |
| Taker entry | 1.75¢ |
| Taker exit | 1.75¢ |
| **Round trip (taker both sides)** | **3.50¢** |
| Round trip (maker both sides) | 0.88¢ |
| **Hold to resolution (enter taker, no exit)** | **1.75¢** |

Two consequences that should shape the strategy:

1. **Holding to resolution roughly halves your fee cost.** You pay on entry and
   nothing at settlement. A strategy that enters and holds is structurally cheaper
   than one that trades in and out. Given fees peak at 50¢ — exactly where
   sentiment-driven uncertainty lives — this is a large effect.
2. **Maker orders cost 25% of taker.** But resting a limit order means uncertain
   fills, and a fill-or-kill "market order" (what the existing `create_order` does
   with `price_cents=99`) is always taker. There's a real tradeoff here, and the
   existing code silently takes the expensive side.

### The edge inequality

A trade is only worth making when:

```
|p_model − p_market|  >  fee + spread_cost + slippage + error_margin
```

Concretely, buying YES at the ask on a market trading around 50¢:

```
fee            = 1.75¢   (taker, entry only, hold to resolution)
half-spread    = 1–2¢    (Kalshi markets are often 2–4¢ wide)
slippage       = 0–1¢    (thin books; worse for larger size)
────────────────────────
minimum edge   ≈ 3–5¢  =  0.03–0.05 in probability terms
```

So a `p_model` of 0.55 against a market at 0.50 is **roughly break-even, not a
5-point edge.** The existing `edge_threshold = 0.08` is defensible in that light —
but it was set without this analysis, and it is applied to a raw
`p_model − p_market` difference with **no cost term at all**. It happens to be
approximately right by luck rather than construction.

**Required change:** make the edge calculation fee-aware and price-dependent, rather
than a flat constant:

```python
def net_edge(p_model: float, entry_price: float, is_taker: bool = True) -> float:
    fee = 0.07 * entry_price * (1 - entry_price)
    if not is_taker:
        fee *= 0.25
    return abs(p_model - entry_price) - fee - half_spread - slippage
```

## Known defects in the existing code

Found by reading `polymarket-sentiment-agent/` against the official docs. **Fix all
of these during the port.**

### 1. 🔴 Signature path omits `/trade-api/v2` — all authenticated calls will 401

`kalshi.py` `_headers()` signs `timestamp + METHOD + path`, where `path` is
`/markets`, `/portfolio/balance`, etc. But Kalshi requires signing the **full path
from the API root**, including the version prefix — the documented example is
`1703123456789GET/trade-api/v2/portfolio/balance`.

Because `self.base` already contains `/trade-api/v2`, the signed message is missing
it. Every authenticated request — balance, positions, order placement — will fail
signature verification.

**Why this hasn't been noticed:** market-data endpoints are public, so discovery and
orderbook reads work fine without auth. The failure only appears the first time you
try to place an order or read your portfolio.

**Fix:** sign the full path.

```python
# WRONG (current)
msg = (ts + method.upper() + path).encode()

# RIGHT
from urllib.parse import urlparse
prefix = urlparse(self.base).path        # "/trade-api/v2"
msg = (ts + method.upper() + prefix + path).encode()
```

### 2. 🔴 Demo base URL is wrong

`config.py` comments the sandbox as
`https://api.elections.demo.kalshi.com/trade-api/v2`. The current documented demo
host is:

```
https://external-api.demo.kalshi.co/trade-api/v2      # note: .co, not .com
```

Since demo is where all your paper validation should run, this matters immediately.

> Hostnames in this API have changed more than once and sources disagree
> (`api.elections.kalshi.com` vs `external-api.kalshi.com` both appear in the wild).
> **Verify both REST and WebSocket hosts against docs.kalshi.com at implementation
> time** rather than trusting any document, including this one.

### 3. 🔴 No fee modeling anywhere — paper P&L is systematically optimistic

`grep -i fee` across the entire backend returns **nothing**. Combined with defect 4,
paper results will overstate live performance by roughly 3–4¢ per contract per round
trip — which, on $1 contracts, is 3–4% of notional and larger than most plausible
sentiment edges.

**This defect alone can make a losing strategy look profitable.** It is the single
most important fix for your actual question.

### 4. 🟠 Paper fills at the midpoint, not the ask

`execution.py` `_record_paper_fill()` fills at `plan.price`, and the note string
claims `"Paper fill at best ask"` — but `plan.price` comes from `market.py`, which
sets it to the **midpoint** of bid and ask. So the simulator buys at mid and gets the
half-spread for free, on every trade.

Real taker orders cross the spread. On a 4¢-wide market that's a free 2¢ per
contract that will not exist live.

**Fix:** fill buys at `best_ask`, sells at `best_bid`, then subtract fees.

### 5. 🟠 Orderbook price units are inconsistent — one path is certainly wrong

Two functions in `kalshi.py` disagree about units:

- `fetch_all_open_markets()` treats `yes_bid` as **cents**: `float(m.get("yes_bid", 0)) / 100.0`
- `get_orderbook()` treats book prices as **dollars**: `yes_asks = [1.0 - float(p) for p, _ in no_rows]`

If the book returns cents (e.g. `62`), the second computes `1.0 - 62 = -61.0` — a
nonsense negative probability that would propagate into edge calculations.

> ### ⚠️ Correction (verified against the live API, 2026-07-24)
>
> An earlier revision of this document recommended standardizing on **integer
> cents**. That was also wrong. The current API returns **dollar-denominated
> strings** under `*_dollars` field names:
>
> ```jsonc
> "yes_bid_dollars": "0.1490",   // string, already dollars
> "yes_ask_dollars": "0.1750",
> "no_bid_dollars":  "0.8250",
> {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.6850","33000.00"]]}}
> ```
>
> Note the orderbook key is `orderbook_fp`, not `orderbook`, and sizes are
> strings too. Sanity check: `1 − 0.8250 = 0.1750` matches `yes_ask_dollars`. ✅
>
> This is exactly why this document says to verify against the API rather than
> trust any write-up — the verification caught an error in the fix itself.

**Fixed in the port:** a single `to_dollars()` parse point, `orderbook_fp` with a
legacy-key fallback. See [`PORT-MANIFEST.md`](PORT-MANIFEST.md) defect 5.

**Also discovered:** markets carry **no `category` field at all**, so the source's
category filter silently discarded 100% of markets. And a scan of 1,200 open
markets found exactly **one** with a two-sided quote — `status=open` is dominated
by unquoted provisional multivariate markets, so filtering those out is what makes
discovery work at all.

### 6. 🟡 The Bayesian likelihood-ratio constant is unjustified

`lr = 1.0 + 4.0 * confidence`. The `4.0` sets how strongly a single headline moves
your probability estimate, and it was chosen arbitrarily. Too high and every
headline produces a spurious "edge"; too low and nothing ever trades.

This should be **fitted from calibration data**, not guessed. Until you have
resolved-market data, treat every probability the system produces as having unknown
scale. See [`VALIDATION.md`](VALIDATION.md).

### 7. 🟡 Timezone crash in `/api/portfolio`

Already documented in the sibling repo's own `docs/PORT-PLAN.md` (item 1): comparing
a tz-aware `cutoff` against tz-naive `Trade.created_at` from SQLite raises
`TypeError`. The endpoint 500s as soon as any trade exists. Their `PORT-PLAN.md` is
worth reading in full — it's a genuinely good self-audit and items 1–5 there are all
real.

## Risk model

Keep the existing gates and add fee awareness:

| Gate | Existing | Change needed |
| --- | --- | --- |
| Kill switch (DB-backed) | ✅ | — |
| Max size per trade | ✅ | — |
| Max open positions | ✅ | — |
| Daily drawdown → auto kill | ✅ | — |
| Edge threshold | ✅ flat 0.08 | ➡️ make fee- and price-aware |
| Liquidity check | ❌ | ➡️ **add** — never take more than a fraction of book depth |
| Time-to-resolution filter | ❌ | ➡️ **add** — avoid markets resolving in minutes |
| Correlated exposure | ❌ | ➡️ **add** — 8 positions on one event isn't diversified |

**Correlation is the sneakiest of these.** `max_open_positions = 8` sounds
diversified, but eight Trump-related markets driven by one news cycle are effectively
one bet at 8× size. Sentiment strategies are *structurally* prone to this, because
one big story generates many correlated signals at once.

## What "does it make money" requires

Detailed methodology is in [`VALIDATION.md`](VALIDATION.md). The short version — you
need all four, and most bots skip the last two:

1. **Realistic paper fills** — ask/bid, not mid; fees included; capped by book depth.
2. **Resolution tracking** — join settled markets back to predictions. Brier score
   and calibration, not just P&L. *(`track_record.py` already does most of this.)*
3. **A benchmark** — "profitable" is meaningless alone. Compare against: always
   betting the market price (zero edge by construction), a random-entry baseline, and
   buy-and-hold-favorite. If you can't beat betting the market price, your signal is
   noise.
4. **A pre-registered sample size.** Decide *before* you start how many resolved
   trades constitute a verdict. Otherwise you will stop when the number looks good —
   which is how noise becomes a strategy.

Rough guide: **200+ resolved positions** before drawing conclusions, and be aware
that even then, a positive result at that sample size is weak evidence.

## Build order

| # | Step | Effort | Why this order |
| --: | --- | --- | --- |
| 1 | Port the repo; strip Polymarket/x402/frontend extras | M | Get a running skeleton |
| 2 | **Fix defects 1, 2, 5** (auth path, demo URL, price units) | S | Nothing authenticated works until then |
| 3 | Verify against demo: fetch a market, read balance | S | First proof the auth chain works end to end |
| 4 | **Fix defects 3, 4** (fees, fill-at-ask) | S | Everything downstream is fiction without this |
| 5 | Fee-aware `net_edge()` replacing the flat threshold | S | Makes the trade decision honest |
| 6 | Liquidity + time-to-resolution + correlation gates | M | Prevents the obvious blowups |
| 7 | Deploy always-on ([`OPERATIONS.md`](OPERATIONS.md)) | M | Start accumulating the sample |
| 8 | Wire `track_record.py` to Kalshi settlement | M | This is the answer machine |
| 9 | **Run and wait.** Collect 200+ resolutions | — | Unavoidable and unskippable |
| 10 | Evaluate vs. benchmarks; tune the `4.0`; only then consider live | — | Decision point |

Steps 2 and 4 are small in code and enormous in consequence. Step 9 cannot be
compressed — it is calendar time, not work, and it's the reason to get the always-on
deployment right early.

---

## Related documents

- [`APIS.md`](APIS.md) — every API, auth, quotas, costs, alternatives, workarounds
- [`VALIDATION.md`](VALIDATION.md) — the "am I making money" methodology
- [`OPERATIONS.md`](OPERATIONS.md) — running continuously
- [`SETUP.md`](SETUP.md) — dev environment
