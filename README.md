# kalshi-bot

An autonomous trading bot for [Kalshi](https://kalshi.com), a CFTC-regulated
prediction-market exchange. It reads news + Trump's Truth Social posts, has
**Claude Opus decide the bets**, sizes them with fractional-Kelly, and trades
across all Kalshi categories except crypto — with special focus on the
"what will Trump say" speech/mention markets.

> **Status: production build, running in PAPER mode.** The full pipeline works
> end to end. It has **not** proven an edge yet — the paper week must show
> Claude's predictions beating the market (see
> [`docs/VALIDATION.md`](docs/VALIDATION.md)) before any real money moves. That
> gate is non-negotiable.

---

## How it works

```
Scout        RSS feeds + Truth Social (@realDonaldTrump)         → news
Pre-filter   cheap sentiment label + Bayesian prior + market match → candidates
Shortlist    rank by preliminary edge; keep the top N              → finalists
ANALYST      Claude Opus prices each finalist and decides          → {side, prob,
             side / probability / confidence / kelly / rationale       confidence…}
Overseer     fee-aware edge + fractional-Kelly + risk gates        → sized plan
Trader       PAPER │ RECOMMEND (Telegram approve) │ LIVE           → fill
Settlement   poll Kalshi resolutions → realized P&L + calibration  → Brier/log-loss
Notifier     Telegram cards, approvals, /status /pnl /calibration
```

The LLM does two jobs at two tiers: a **cheap classifier** on every headline, and
**Claude Opus as the actual decision-maker** on a small shortlist (which bounds
cost). Probability math stays deterministic and auditable; Claude supplies the
judgement and can veto a bad news→market match with `SKIP`.

## Layout

```
kalshi_bot/
├── config.py           all settings (env / .env)
├── database.py         SQLAlchemy engine, Postgres URL normalization
├── models.py           audit trail: news → signal → snapshot → trade → resolution
├── fees.py             Kalshi fee model + net-edge
├── orchestrator.py     the two-tier loop
└── modules/
    ├── kalshi.py       Trade API v2 client (RSA-PSS signing, books, orders, settle)
    ├── ingestion.py    RSS + Truth Social scout, source-health tracking
    ├── intelligence.py Tier-1 sentiment classifier + deterministic Bayes
    ├── analyst.py      Tier-2 Claude Opus decision (CLI / API / Groq fallback)
    ├── market.py       snapshots incl. book depth
    ├── risk.py         Kelly sizing, kill switch, liquidity, drawdown gates
    ├── execution.py    PAPER / RECOMMEND / LIVE + approval flow
    ├── notifier.py     Telegram alerts + control
    ├── settlement.py   resolution polling + Brier/log-loss calibration
    └── browser_exec.py optional Playwright fallback execution
tests/                  regression + new-module tests  (pytest → 20 passed)
```

## The Claude engine: CLI vs API

Claude makes the decisions three ways, set by `ANALYST_PROVIDER`:

| Provider | Auth | Best for |
| --- | --- | --- |
| `cli` | your Claude **subscription** (runs `claude -p`) | local dev on your Mac, no API bill |
| `claude` | **API key** (`ANTHROPIC_API_KEY`) | 24/7 servers — reliable, headless |
| `groq` | Groq key (free) | fallback / cheapest |

The chain always falls back Claude → Groq → deterministic heuristic, so an outage
never hard-stops the loop. Use `cli` locally; use `claude` when hosted.

## Quick start (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp example.env .env          # then fill in keys (see the file's inline notes)
python3 main.py --check      # verifies Kalshi, Truth Social, Telegram, Claude
```

Run it:

```bash
python3 main.py --check      # config + connectivity, no trading
python3 main.py --once       # a single loop tick, then exit
python3 main.py              # run continuously
```

**Minimum to start a paper run:** nothing paid. `ANALYST_PROVIDER=cli` uses your
subscription; Kalshi market data is public; Truth Social + RSS are free. Add a
free `GROQ_API_KEY` (fallback) and `TELEGRAM_*` (phone alerts) if you want them.

## Running it 24/7 (hosted)

You can't leave a laptop on all week. Deploy to a cheap/free always-on box with
Docker — the repo ships a `Dockerfile` and `docker-compose.yml` (auto-restart,
persistent SQLite volume). On a server, use `ANALYST_PROVIDER=claude` +
`ANTHROPIC_API_KEY`. **Full walkthrough (incl. free Oracle Cloud):**
[`docs/DEPLOY.md`](docs/DEPLOY.md).

## The paper week

Start here before anything else: [`docs/PAPER_WEEK.md`](docs/PAPER_WEEK.md) — what
you need, how to launch it, and how to read the calibration result that decides
whether the strategy is real.

## Safety defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `PAPER` | Simulated fills. No orders placed. |
| `KALSHI_BASE_URL` | **demo** | Sandbox. Production is an explicit change. |
| `ANALYST_PROVIDER` | `claude`→ set `cli` locally | Claude decides; Groq fallback |
| `MIN_NET_EDGE` | `0.02` | Applied *after* fees + spread |
| `MAX_TRADE_FRACTION` | `0.05` | ≤5% of deployable capital per trade |
| `MAX_BOOK_FRACTION` | `0.25` | Never take >¼ of resting depth |
| `DAILY_DRAWDOWN_USD` | `75` | Auto-trips the kill switch |

Reaching production with real money requires deliberately changing
`KALSHI_BASE_URL` **and** `TRADING_MODE`, plus adding Kalshi credentials. That is
intentional.

## Documentation

| Doc | Read it for |
| --- | --- |
| [`PAPER_WEEK.md`](docs/PAPER_WEEK.md) | **Start here** — running the paper week and reading the result |
| [`PRODUCTION.md`](docs/PRODUCTION.md) | The production build: analyst, Truth Social, Telegram, settlement |
| [`DEPLOY.md`](docs/DEPLOY.md) | 24/7 hosting on a VPS / free Oracle Cloud, Docker + systemd |
| [`VALIDATION.md`](docs/VALIDATION.md) | Telling a real edge from a measurement artifact |
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Pipeline design, fee-aware edge math, the fixed defects |
| [`APIS.md`](docs/APIS.md) | Kalshi auth/limits/fees, news sources, LLM providers |
| [`OPERATIONS.md`](docs/OPERATIONS.md) | Durable storage, alerting on silence |

## Configuration & secrets

All config lives in `.env` (copied from [`example.env`](example.env), which
documents every key with what it's for and how to get it). `.env`, `secrets/`,
and `data/` are gitignored.

**Never commit credentials.** This repository is public — anything pushed here is
world-readable, and a leaked key should be treated as compromised even after
deletion (git keeps history).

## A note on `polymarket-sentiment-agent/`

That directory is a separate upstream project
([`priyanshshahh/polymarket-sentiment-agent`](https://github.com/priyanshshahh/polymarket-sentiment-agent),
MIT) checked out inside this folder. It has its own git history and is
**excluded via `.gitignore`**. Do not `git add` it. Attribution is in
[`NOTICE`](NOTICE).

## License

MIT
