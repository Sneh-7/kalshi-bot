# Operations — running continuously

**Written 2026-07-24.**

You want the agent running always, accumulating a paper-trading sample. That's a
multi-week unattended run, which makes this less about deployment and more about
**not silently losing your data or your sample.**

---

## Contents

1. [What "always on" actually requires](#what-always-on-actually-requires)
2. [Hosting options](#hosting-options)
3. [The loop](#the-loop)
4. [Durable storage — the #1 way to lose weeks](#durable-storage)
5. [Monitoring and alerting](#monitoring-and-alerting)
6. [Safety controls](#safety-controls)
7. [Secrets](#secrets)
8. [Recommended setup](#recommended-setup)

---

## What "always on" actually requires

Five properties. Missing any one wastes the run.

| Property | Why | Failure if missing |
| --- | --- | --- |
| **Durable storage** | Sample accumulates over weeks | Restart wipes weeks of data |
| **Restart resilience** | Hosts restart; processes crash | Silent stop; you find out days later |
| **Idempotency** | Retries must not double-trade | Duplicate positions |
| **Observability** | Silent failure is the norm | Bot "runs" for a week doing nothing |
| **Kill switch** | You must be able to stop it now | No way to intervene |

The existing codebase has idempotency (`make_idem_key`) and a DB-backed kill switch
that survives restart. The gaps are storage durability and observability.

---

## Hosting options

| Option | Cost | Always-on | Durable | Verdict |
| --- | --- | :---: | :---: | --- |
| **Local + `launchd`** | $0 | 🟡 when Mac is awake | ✅ | Good for first weeks |
| **Fly.io** | $0–5/mo | ✅ | ✅ w/ volume | ✅ **Recommended** |
| **Render free** | $0 | ❌ **sleeps ~15min idle** | ❌ ephemeral | ❌ Unsuitable |
| **Render paid** | $7/mo | ✅ | 🟡 needs Postgres | 🟡 Fine |
| **Railway** | ~$5/mo | ✅ | ✅ | 🟡 Fine |
| **VPS** (Hetzner/DO) | $4–6/mo | ✅ | ✅ | ✅ Most control |

### Render free tier is disqualified

The sibling repo ships a `render.yaml` and its own notes admit: *"instance sleeps
after ~15 min idle; SQLite is ephemeral."*

Both halves are fatal here. A sleeping instance stops ingesting news and misses
market moves — and your sample silently develops holes correlated with time of day.
Ephemeral SQLite means a restart erases your accumulated resolutions. **Do not run a
multi-week validation on it.**

### Local + `launchd` is a legitimate start

You have a Mac. For the first weeks, running locally is free, fully durable, and
easy to inspect. Caveat: it stops when the Mac sleeps. Either disable sleep
(`caffeinate`) or accept gaps — but **log the gaps**, because unrecorded downtime
biases your sample toward whenever your laptop happened to be open.

```xml
<!-- ~/Library/LaunchAgents/com.sneh.kalshibot.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.sneh.kalshibot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/sneh/Desktop/kalshi/.venv/bin/python</string>
        <string>-m</string>
        <string>kalshi_bot.main</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/sneh/Desktop/kalshi</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/sneh/Desktop/kalshi/logs/agent.log</string>
    <key>StandardErrorPath</key><string>/Users/sneh/Desktop/kalshi/logs/agent.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.sneh.kalshibot.plist
launchctl list | grep kalshibot
```

`KeepAlive` restarts the process if it dies — which pairs with idempotency to make
crashes non-destructive.

---

## The loop

The existing orchestrator uses APScheduler at `loop_interval_seconds = 30`.

**30s is reasonable for a sentiment strategy.** You are not competing on
microseconds; you're betting that news implies mispricing that persists for minutes
or hours. If it doesn't persist that long, latency isn't your fix — the premise is.

Rate-limit headroom at 30s intervals is enormous: Basic tier allows ~20 reads/sec,
and you need roughly 20 reads per 30s. **You are using about 3% of your budget.**

One cycle:

```
1. Ingest news        (RSS, GDELT — dedupe by URL)
2. Extract sentiment  (LLM or heuristic)
3. Fetch markets      (Kalshi REST; snapshot for audit)
4. Match news→markets ← weakest link; see ARCHITECTURE.md
5. Compute net edge   (MUST include fees)
6. Risk gates         (reject or resize)
7. Execute            (PAPER)
8. Log the decision   (including rejections)
9. Resolve settled markets → track record
```

Step 9 doesn't need to run every cycle — hourly is fine, and it's what turns raw
trades into the answer you care about.

---

## Durable storage

**This is the single most likely way to waste a month.**

Use Postgres for any run you intend to believe. Neon's free tier is sufficient.

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@host/dbname
```

> ⚠️ The sibling repo's `database.py` does **not** normalize the `postgres://` or
> `postgresql://` scheme that Neon and Render hand out into the
> `postgresql+psycopg2://` form SQLAlchemy 2.x requires. Following its own README
> advice fails at `create_engine`. Fix during the port — its `PORT-PLAN.md` item 5
> has the code.

Back up regardless:

```bash
pg_dump "$DATABASE_URL" | gzip > "backup-$(date +%F).sql.gz"
```

A weekly cron for this costs nothing and protects a sample that takes weeks to
rebuild.

---

## Monitoring and alerting

**Silent failure is the default outcome of an unattended bot.** The existing code
catches exceptions per source and logs a warning — good for resilience, dangerous for
observability. A dead RSS feed, an expired LLM key, or a changed Kalshi schema all
produce a bot that runs happily and does nothing.

Alert on **absence**, not just errors:

| Check | Threshold | Why |
| --- | --- | --- |
| News items ingested | 0 for 3 cycles | Feed died |
| Markets fetched | 0 for 3 cycles | API/schema change |
| Signals generated | 0 for 1 hour | LLM chain broken |
| Trades + rejections | 0 for 6 hours | Pipeline stalled *or* thresholds too tight |
| Heartbeat | no cycle in 5 min | Process dead |
| Per-source health | any feed 0 for 24h | Silent source rot |

That fourth row is subtle: **zero trades is ambiguous.** It could mean a healthy bot
correctly finding no edge, or a broken one. Logging *rejections with reasons*
disambiguates — "47 candidates, all below edge threshold" is healthy; "0 candidates"
is broken.

`notifier.py` already has Twilio SMS wiring. A daily summary (cycles run, items
ingested, trades placed, rejections by reason, current P&L, resolutions scored) is
more useful than per-event alerts and won't train you to ignore it.

---

## Safety controls

| Control | State | Notes |
| --- | --- | --- |
| Kill switch | ✅ DB-backed, survives restart | Auto-trips on daily drawdown |
| `TRADING_MODE` | ✅ `PAPER`/`RECOMMEND`/`LIVE` | **Keep `PAPER`** |
| Admin auth | 🟡 exists in one variant | Port `ADMIN_TOKEN`; control routes are otherwise open to anyone who can reach the port |
| Idempotency | ✅ `make_idem_key` | Prevents double-fills on retry |

> **Port the `ADMIN_TOKEN` bearer auth** (`PORT-PLAN.md` item 2) before exposing this
> anywhere public. Without it, `/api/kill-switch` and `/api/loop/*` are reachable by
> anyone who can hit the port — CORS restricts browsers, not `curl`. The good version
> fails *closed* (503) when the token is unset.

**Deployment order:** `PAPER` → (long validation) → `RECOMMEND` → `LIVE` at 10% size.
Never jump to `LIVE`.

---

## Secrets

- `.env` and `secrets/` are gitignored. **This repo is public** — a committed key
  should be rotated, not scrubbed, since git retains history and public pushes are
  indexed quickly.
- Kalshi's private key PEM is displayed **once**. Save it outside the repo.
- On hosted platforms use their secret store, not files.
- Never log credential values, even at DEBUG. Log *whether* a key loaded.

---

## Recommended setup

For your specific goal — validate a paper-trading edge over weeks, spend nothing:

```
Phase 1 (weeks 1–2)   Local + launchd, SQLite
                      Verify the pipeline actually works end to end.
                      Cheap to inspect, cheap to restart, zero cost.

Phase 2 (weeks 3+)    Fly.io free tier + Neon Postgres
                      Real always-on. Durable sample. Still $0.
                      Daily SMS/email summary.

Phase 3               Evaluate against pre-registered criteria
                      (see VALIDATION.md)
```

**Don't over-engineer phase 1.** The valuable output is a trustworthy answer, and
that comes from correct fill/fee modeling and a long enough sample — not from
infrastructure. Deploy simply, then leave it alone: the hardest operational
discipline here is not touching a running experiment because the equity curve looks
interesting.

---

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design and known defects
- [`APIS.md`](APIS.md) — API details, quotas, fallbacks
- [`VALIDATION.md`](VALIDATION.md) — the measurement methodology
