# Architecture

> ## ⚠️ This document describes a PLAN, not existing software.
>
> **Nothing in this document is implemented.** As of the latest commit, the entire
> codebase is `main.py` (9 lines, prints `Hello, World!`) and an empty
> `requirements.txt`.
>
> Every module, class, and file path below is **proposed**. Treat this as a design
> sketch to build against and revise — not as a description of how the system
> currently works. Sections are marked ❌ (not built) throughout so this stays
> unambiguous as the project grows. Update the markers to ✅ as things land.

---

## Table of contents

1. [Goal](#goal)
2. [Background: what Kalshi is](#background-what-kalshi-is)
3. [Design principles](#design-principles)
4. [Proposed structure](#proposed-structure)
5. [Component design](#component-design)
6. [Data flow](#data-flow)
7. [Configuration & secrets](#configuration--secrets)
8. [Risk management](#risk-management)
9. [Testing strategy](#testing-strategy)
10. [Build order](#build-order)
11. [Open questions](#open-questions)

---

## Goal

Build a bot that trades event contracts on Kalshi programmatically: ingest market
data, evaluate it against a strategy, and place orders — with risk limits that make
it safe to leave running.

**Scope boundary.** The first milestone is *paper trading against Kalshi's demo
environment*. Live capital is explicitly out of scope until a strategy has been
validated on paper over a meaningful sample.

## Background: what Kalshi is

Kalshi is a CFTC-regulated exchange for **event contracts** — binary contracts that
settle to $1.00 if an event occurs and $0.00 if it does not. A contract quoted at
$0.62 implies roughly a 62% market-assigned probability.

This differs from conventional asset trading in ways that shape the design:

- **Bounded payoff.** Every position settles at exactly $0 or $1. Maximum loss per
  contract is known upfront, which makes position sizing tractable.
- **Defined expiry.** Every market resolves at a known time. There is no
  indefinite holding.
- **Probability-native pricing.** Price *is* an implied probability, so strategy
  logic reduces to: does my probability estimate differ from the market's by enough
  to cover fees and spread?
- **Thin liquidity.** Many markets have wide spreads and shallow books. Slippage and
  fill probability matter far more than in liquid equity markets.

> **Verify before implementing.** API endpoints, authentication scheme, rate limits,
> and fee schedule must be read from Kalshi's official documentation at
> <https://trading-api.readme.io/> rather than assumed from this document. API
> details change, and specifics stated here should be treated as unverified.

## Design principles

1. **Paper first.** Live trading stays behind an explicit config flag that defaults
   to off. A misconfigured bot should lose nothing.
2. **Fail closed.** On any ambiguity — unreachable API, unparseable response,
   breached risk limit — stop trading rather than guess. A bot that halts is
   recoverable; one that trades on bad state is not.
3. **Strategy is pluggable.** Strategies implement a narrow interface and know
   nothing about HTTP, auth, or order plumbing. This keeps them unit-testable
   without network access.
4. **Every decision is logged.** For each order, record the inputs that produced it.
   Without this, a losing week is unattributable and therefore unfixable.
5. **No secrets in the repo.** The repository is public. Credentials live in `.env`
   and in `.gitignore`, permanently.

## Proposed structure

❌ *None of this exists yet.*

```
kalshi-bot/
├── main.py                    # CLI entry point
├── requirements.txt
├── .env                       # secrets — gitignored, never committed
│
├── kalshi_bot/
│   ├── __init__.py
│   ├── config.py              # env loading + validation
│   ├── client.py              # Kalshi REST client (auth, retries, rate limits)
│   ├── models.py              # typed Market, Order, Position, Fill
│   │
│   ├── data/
│   │   ├── ingestion.py       # fetch + normalize market data
│   │   └── store.py           # local persistence / caching
│   │
│   ├── strategy/
│   │   ├── base.py            # Strategy ABC — the pluggable interface
│   │   └── baseline.py        # first concrete strategy
│   │
│   ├── execution/
│   │   ├── broker.py          # Broker ABC
│   │   ├── paper.py           # simulated fills — DEFAULT
│   │   └── live.py            # real orders — gated behind a flag
│   │
│   ├── risk/
│   │   └── limits.py          # position sizing + kill switches
│   │
│   └── util/
│       └── logging.py         # structured logging
│
├── tests/
└── docs/
```

## Component design

### `config.py` ❌

Loads configuration from environment/`.env` and **validates it at startup**, failing
loudly on anything missing or malformed.

Validation belongs here, not scattered across call sites. A bot that starts with a
missing API key and discovers it mid-session has already wasted the session; one
that refuses to start has cost nothing.

```python
@dataclass(frozen=True)
class Config:
    api_key_id: str
    private_key_path: Path
    env: Literal["demo", "prod"]     # defaults to "demo"
    max_position_size: int
    max_daily_loss: float
    dry_run: bool = True             # safe default
```

**`env` defaults to `demo` and `dry_run` defaults to `True`.** Reaching production
must require deliberate action; it must never be the fallback when config is absent.

### `client.py` ❌

The only module that speaks HTTP to Kalshi. Responsibilities:

- Request signing / authentication
- Retry with exponential backoff on 5xx and connection errors
- Respect rate limits; back off on 429 rather than hammering
- Raise typed exceptions (`AuthError`, `RateLimitError`, `MarketClosedError`)
  instead of leaking raw HTTP details upward

Isolating HTTP here means strategies can be tested against a fake client with no
network. If auth or transport concerns leak into strategy code, that testability is
lost — this boundary is the point.

### `models.py` ❌

Typed representations of exchange objects — `Market`, `Order`, `Position`, `Fill` —
as frozen dataclasses. Parse raw API dictionaries into these at the client boundary
so that untyped dicts never propagate into business logic, where a renamed upstream
field would otherwise surface as a `KeyError` deep in a strategy at runtime.

Represent prices as **integer cents**, not floats. Binary contract prices are
exactly representable as integers 1–99, and float arithmetic invites rounding
errors in P&L accumulation.

### `strategy/base.py` ❌

```python
class Strategy(ABC):
    @abstractmethod
    def evaluate(self, market: Market, position: Position | None) -> Signal | None:
        """Return a Signal to act, or None to do nothing."""
```

Deliberately narrow. A strategy receives market state and current position, and
returns intent. It does not place orders, call the network, or manage risk — those
belong to the broker and risk layers. This is what makes strategies unit-testable
as pure functions.

### `execution/` ❌

A `Broker` interface with two implementations:

- **`paper.py`** — simulates fills against observed book state. The default.
  Must model spread and fill probability; a paper broker that assumes instant
  mid-price fills will report profits that evaporate on contact with a real,
  thin order book.
- **`live.py`** — places real orders. Gated behind `dry_run=False`.

Identical interface, so promoting a validated strategy to live is a config change
rather than a rewrite.

### `risk/limits.py` ❌

Enforced **before** any order reaches the broker:

- Max contracts per market
- Max total capital deployed
- Max daily loss → kill switch halting all trading
- Sanity bounds (reject orders priced ≤0 or ≥100 cents)

This layer's job is to be the thing that saves you from your own strategy bug. It
must be impossible for a strategy to bypass.

## Data flow

❌ *Proposed.*

```
   ┌─────────────┐
   │ Kalshi API  │
   └──────┬──────┘
          │  raw JSON
          ▼
   ┌─────────────┐
   │  client.py  │  auth, retry, rate limit
   └──────┬──────┘
          │  typed models
          ▼
   ┌─────────────┐
   │  ingestion  │  normalize, filter tradeable markets
   └──────┬──────┘
          │  Market
          ▼
   ┌─────────────┐
   │  strategy   │  pure logic — no I/O
   └──────┬──────┘
          │  Signal | None
          ▼
   ┌─────────────┐
   │    risk     │  ◄── rejects or resizes
   └──────┬──────┘
          │  approved Order
          ▼
   ┌─────────────┐
   │   broker    │  paper (default) | live (gated)
   └──────┬──────┘
          │
          ▼
      structured log  ── every decision, with its inputs
```

The one-way flow matters: strategy sits between ingestion and risk and cannot reach
around either side.

## Configuration & secrets

Runtime configuration comes from `.env` (gitignored):

```bash
KALSHI_API_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_key.pem
KALSHI_ENV=demo
MAX_POSITION_SIZE=10
MAX_DAILY_LOSS=25.00
DRY_RUN=true
```

Rules:

- `.env` and `secrets/` are gitignored and stay that way.
- Commit a `.env.example` with **placeholder** values as documentation.
- Never log credential values, even at DEBUG. Log whether a key loaded, never what
  it contains.
- **This repository is public.** A key committed here should be considered
  compromised the moment it is pushed — rotate it rather than trying to scrub
  history.

## Risk management

Bounded payoffs make sizing tractable, but the failure modes are real:

| Risk | Mitigation |
| --- | --- |
| Strategy bug placing runaway orders | Hard caps in `risk/limits.py`, enforced pre-broker |
| Thin book → bad fills | Model spread in paper broker; cap order size vs. book depth |
| API outage mid-position | Fail closed; alert; never blind-retry order placement |
| Correlated positions | Track aggregate exposure across related markets, not per-market only |
| Accidental live trading | `dry_run=True` and `env=demo` as defaults |
| Overfitting to backtest | Require forward paper-trading before live |

**Duplicate order placement deserves specific care.** If a request times out, the
order may or may not have been accepted. Naive retry can double a position. Use
client-side order IDs for idempotency and reconcile against open orders before
retrying.

## Testing strategy

❌ *No tests exist.*

- **Unit** — strategies against synthetic `Market` fixtures, no network. Should be
  the bulk of the suite.
- **Client** — against recorded/mocked HTTP; cover 429, 5xx, malformed payloads.
- **Risk** — explicitly assert that limit breaches *reject*. Test the denial path,
  not just the happy path.
- **Integration** — against the demo environment, never production.

## Build order

Each step should be usable before starting the next.

| # | Step | Status |
| --: | --- | --- |
| 1 | `config.py` + `.env.example`, validated at startup | ❌ |
| 2 | `client.py` — auth against demo, fetch one market | ❌ |
| 3 | `models.py` — typed Market/Order/Position | ❌ |
| 4 | `ingestion.py` — list and filter tradeable markets | ❌ |
| 5 | `Strategy` ABC + a trivial strategy, unit-tested | ❌ |
| 6 | `paper.py` broker with realistic fill modeling | ❌ |
| 7 | `risk/limits.py` wired in ahead of the broker | ❌ |
| 8 | Structured logging of every decision | ❌ |
| 9 | Run on demo; measure | ❌ |
| 10 | Live — only after sustained paper validation | ❌ |

Step 2 is the real unknown. Authentication is where most exchange integrations
stall; do it early and in isolation.

## Open questions

Unresolved, and worth deciding before building far:

1. **Which markets?** Weather, economics, politics? Strategy design depends heavily
   on the domain and on where genuine edge might exist.
2. **Where does edge come from?** Mispricing vs. a model? Faster information? Market
   making on spread? This is the actual hard question — the plumbing above is
   comparatively routine, and no amount of clean architecture substitutes for
   having an answer here.
3. **Does the sibling `polymarket-sentiment-agent` work feed this?** Sentiment
   signals could inform probability estimates, but that repo is separate and
   currently unpublished.
4. **Polling or streaming?** Depends on whether Kalshi offers a websocket feed and
   on strategy latency needs.
5. **Backtesting?** Requires historical data. Availability needs checking before
   committing to a backtest-driven approach.
