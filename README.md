# kalshi-bot

A trading bot for [Kalshi](https://kalshi.com), a CFTC-regulated prediction market
exchange for event contracts.

> **Project status: running in PAPER mode against Kalshi's demo environment.**
>
> The pipeline works end to end — news ingestion, sentiment classification,
> Bayesian probability, live Kalshi market data, fee-aware edge, risk gates, and
> simulated fills. It has **not** been validated: no resolved-market sample
> exists yet, so there is no evidence of an edge. See
> [`docs/VALIDATION.md`](docs/VALIDATION.md).
>
> Ported from a Kalshi conversion of `polymarket-sentiment-agent` (MIT), with
> **eight defects fixed** on the way in — including three that would have made
> paper results systematically optimistic. Full accounting in
> [`docs/PORT-MANIFEST.md`](docs/PORT-MANIFEST.md).

---

## What exists today

```
kalshi_bot/
├── config.py           settings; DEMO + PAPER by default
├── database.py         SQLAlchemy engine, Postgres URL normalization
├── models.py           audit trail: news → signal → snapshot → trade
├── fees.py             Kalshi fee model + net-edge  ← did not exist in source
├── orchestrator.py     the loop
└── modules/
    ├── kalshi.py       Trade API v2 client (RSA-PSS signing, books, orders)
    ├── ingestion.py    RSS scout with source-health tracking
    ├── intelligence.py LLM classifier + deterministic Bayes
    ├── market.py       snapshots incl. book depth
    ├── risk.py         kill switch, liquidity, drawdown, time-to-close
    └── execution.py    PAPER / RECOMMEND / LIVE
tests/                  regression tests pinning all 8 fixes
```

Verified working:

```
$ pytest tests/ -q
12 passed

$ python3 main.py --once
tick done: 33 news, 33 signals, 20 markets, 0 considered, 0 traded, 0 rejected
```

## Requirements

- Python 3.9 or newer (3.11+ recommended)
- macOS, Linux, or WSL

Verify your interpreter:

```bash
python3 --version
```

## Setup

Clone and enter the repository:

```bash
git clone https://github.com/Sneh-7/kalshi-bot.git
cd kalshi-bot
```

Create and activate a virtual environment. This keeps project packages isolated
from your system Python:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

> `requirements.txt` is currently empty, so this is a no-op today. It will
> matter once real dependencies land.

## Running

```bash
python3 main.py --check    # verify config + connectivity, no trading
python3 main.py --once     # one loop tick, then exit
python3 main.py            # run continuously
```

`--check` is the fastest way to confirm your setup: it reports mode, environment,
whether credentials loaded, and exercises exchange status, market discovery, and
orderbook parsing.

**No API key is needed to start.** Kalshi's market-data endpoints are public, so
the whole pipeline runs unauthenticated — credentials are only required for
`LIVE` mode. Similarly, no LLM key is required: the sentiment classifier falls
back to a keyword heuristic, which also doubles as the baseline the LLM has to
beat to justify its cost.

## Safety defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `PAPER` | Simulated fills. No orders are placed. |
| `KALSHI_BASE_URL` | **demo** | Sandbox. Production requires an explicit change. |
| `MIN_NET_EDGE` | `0.02` | Applied *after* fees and spread |
| `MAX_BOOK_FRACTION` | `0.25` | Never take more than ¼ of resting depth |
| `KILL_SWITCH` | DB-backed | Survives restart; auto-trips on daily drawdown |

Reaching production with real money requires deliberately changing **two**
settings. That is intentional.

## Documentation

| Doc | Read it for |
| --- | --- |
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Pipeline design, fee-aware edge math, and **7 defects** found in the reference implementation |
| [`APIS.md`](docs/APIS.md) | Kalshi auth/limits/fees, news sources, LLM providers, and a workaround for every failure mode |
| [`VALIDATION.md`](docs/VALIDATION.md) | How to tell a real edge from a measurement artifact |
| [`OPERATIONS.md`](docs/OPERATIONS.md) | Always-on hosting, durable storage, alerting on silence |

**Start with [`VALIDATION.md`](docs/VALIDATION.md)** if your goal is deciding whether
the strategy actually works. It's the part most trading bots get wrong.

## Configuration

No configuration is required yet. When exchange integration is added, credentials
will be read from a `.env` file, which is already listed in `.gitignore`.

**Never commit credentials.** This repository is public. Anything committed here is
world-readable, and secrets pushed to a public repo should be treated as
compromised even after deletion — git retains history, and automated scrapers
index public pushes quickly.

A future `.env` will look roughly like:

```bash
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_key.pem
KALSHI_ENV=demo          # demo | prod
```

## A note on `polymarket-sentiment-agent/`

You may see a `polymarket-sentiment-agent/` directory in your working copy. It is
**deliberately excluded** from this repository via `.gitignore`.

It is a separate upstream project
([`priyanshshahh/polymarket-sentiment-agent`](https://github.com/priyanshshahh/polymarket-sentiment-agent))
that happens to be checked out inside this folder. It has its own git history and
its own remote. It was previously committed here by accident as a broken gitlink —
a submodule pointer with no `.gitmodules` file — which would have produced an empty,
uninitializable directory for anyone cloning this repo. That entry has been removed.

Do not `git add` it.

## Roadmap

Full detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The headline finding
from research: **a working implementation of this system already exists** in the
sibling `polymarket-sentiment-agent/` checkout, and despite its name it already
targets Kalshi (a 322-line Kalshi v2 client, `PAPER`/`RECOMMEND`/`LIVE` modes, Brier
scoring). It is MIT licensed.

The recommended path is therefore **port and fix, not rewrite**:

1. Port the reference implementation; strip Polymarket/x402/frontend extras
2. Fix the auth-signing bug (signed path omits `/trade-api/v2` → every authenticated call 401s)
3. Fix the demo base URL and the orderbook price-unit inconsistency
4. **Add fee modeling and fill-at-ask** — without these, paper P&L overstates reality by ~3–4¢/contract
5. Replace the flat edge threshold with fee-aware `net_edge()`
6. Add liquidity, time-to-resolution, and correlation gates
7. Deploy always-on with durable Postgres
8. Wire settlement tracking → Brier/calibration
9. Run to 200+ resolutions against pre-registered criteria
10. Live only if it beats the market-price benchmark after costs

Steps 2–4 are a few hours of work and determine whether every number after them means
anything.

## License

MIT
