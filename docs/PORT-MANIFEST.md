# Port manifest — polymarket-sentiment-agent → kalshi-bot

**Completed 2026-07-24.** Records exactly what was extracted, what was dropped,
and what was fixed on the way in.

**Source:** your Kalshi conversion of
[`priyanshshahh/polymarket-sentiment-agent`](https://github.com/priyanshshahh/polymarket-sentiment-agent)
(MIT, © 2026 Priyansh Shah), preserved at
[`Sneh-7/polymarket-sentiment-agent@kalshi-conversion`](https://github.com/Sneh-7/polymarket-sentiment-agent/tree/kalshi-conversion).

---

## What was extracted

Core trading logic only — enough to run a headless paper-trading agent.

| Source | Destination | Change |
| --- | --- | --- |
| `modules/kalshi.py` | `kalshi_bot/modules/kalshi.py` | **Heavily rewritten** — see fixes 1, 2, 5 |
| `modules/intelligence.py` | `kalshi_bot/modules/intelligence.py` | Ported; `4.0` constant moved to config |
| `modules/ingestion.py` | `kalshi_bot/modules/ingestion.py` | RSS only; **added source-health tracking** |
| `modules/market.py` | `kalshi_bot/modules/market.py` | Rewritten to capture **book depth** |
| `modules/risk.py` | `kalshi_bot/modules/risk.py` | Ported; **+3 gates**, net-edge threshold |
| `modules/execution.py` | `kalshi_bot/modules/execution.py` | **Fixed fills + fees** (fixes 3, 4) |
| `orchestrator.py` | `kalshi_bot/orchestrator.py` | Ported; logs *all* predictions, not just traded |
| `config.py` | `kalshi_bot/config.py` | Ported; fee params, demo default, filter rework |
| `database.py` | `kalshi_bot/database.py` | Ported as-is (already had URL normalization) |
| `models.py` | `kalshi_bot/models.py` | Reworked: fee columns, depth, tz-aware timestamps |
| — | `kalshi_bot/fees.py` | **NEW** — did not exist in source |
| — | `main.py` | **NEW** — headless entry with `--check` / `--once` |
| — | `tests/test_defect_regressions.py` | **NEW** — pins every fix below |

## What was deliberately dropped

| Dropped | Why |
| --- | --- |
| `api/routes.py`, `main.py` (FastAPI), `schemas.py`, `auth.py` | Paper validation runs headless. HTTP surface is weight without benefit, and unauthenticated control routes are a liability. |
| Entire React frontend | A dashboard doesn't make the numbers more true. Query SQLite directly. |
| `x402_setup.py`, `test_paywall.py` | Crypto micropayments are unrelated to trading on Kalshi. You'd already deleted these. |
| `modules/notifier.py` (Twilio) | Add back for always-on alerting (see `OPERATIONS.md`); not needed to validate. |
| `modules/track_record.py` | **Deferred, not rejected.** Its scoring math is sound but joins Polymarket's Gamma resolution API. Needs rewiring to Kalshi settlement — the highest-value next task. |
| `render.yaml`, `Dockerfile` | Render's free tier sleeps and has ephemeral disk — unsuitable for a multi-week run. |
| CryptoPanic + Truth Social sources | Crypto-specific / frequently blocked. RSS covers the signal. |

---

## Defects fixed during the port

Every one was invisible in normal operation. All are now pinned by tests.

### 1. 🔴 Auth signed the wrong path

Source signed `timestamp + METHOD + "/portfolio/balance"`. Kalshi requires the
full path from the API root, including the version prefix:

```
1703123456789GET/trade-api/v2/portfolio/balance
```

Every authenticated request — balance, positions, order placement — would have
401'd. Invisible because all market-data endpoints are public, so discovery and
orderbook reads work fine unauthenticated.

**Fixed:** `KalshiClient.path_prefix` derived from the base URL and prepended
when signing. Test: `test_signed_message_includes_api_prefix` verifies the
signature against the correct message *and* asserts it fails against the old one.

### 2. 🔴 Wrong demo host

Source: `api.elections.demo.kalshi.com`. Current: `external-api.demo.kalshi.co`
(`.co`, not `.com`). Demo is where all validation should run.

**Fixed:** and demo is now the *default* — reaching production requires a
deliberate config change.

### 3. 🔴 No fee modeling at all

`grep -i fee` across the source backend returned nothing; `Trade.fees_usdc` was
hardcoded `0.0`.

**Fixed:** new `kalshi_bot/fees.py` implementing the verified schedule —
`taker = 0.07 × C × (1−C)`, maker at 25%, no settlement fee. Position sizing,
P&L, and the edge threshold all now run through it.

### 4. 🟠 Paper fills at the midpoint

`_record_paper_fill()` filled at `plan.price` — the **mid** — while its own note
string claimed `"Paper fill at best ask"`. Real taker orders cross the spread.

**Fixed:** fills at `best_ask` for buys; mark-to-market uses `best_bid`, since
that's where you could actually sell.

> **Defects 3 and 4 together overstated paper P&L by roughly 3–4¢ per contract
> per round trip** — on $1 contracts, larger than most plausible sentiment
> edges. A losing strategy could have reported a profit. This was the single
> most important reason not to port the code unexamined.

### 5. 🔴 Price units and field names were wrong — *including in my own first attempt*

The source read `yes_bid` / `yes_ask` and divided by 100 in one function while
treating the same field as dollars in another (`1.0 - 62 = -61.0`).

I initially "fixed" this by standardizing on integer cents. **That was also
wrong.** Verified against the live API on 2026-07-24:

```jsonc
// market
"yes_bid_dollars": "0.1490",   // STRING, already dollars — not cents
"yes_ask_dollars": "0.1750",
"no_bid_dollars":  "0.8250",
"liquidity_dollars": "0.0000",
"volume_24h_fp": "0.00",

// orderbook — note the key name
{"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.6850","33000.00"], ...]}}
```

Sanity check: `1 − 0.8250 = 0.1750`, matching the reported `yes_ask_dollars`. ✅

**Fixed:** single `to_dollars()` parse point, `orderbook_fp` with legacy-key
fallback, all sizes parsed from strings.

**Lesson:** the docs said "verify at implementation time." That was right, and
it caught an error in the fix itself.

### 6. 🔴 Category filter silently dropped 100% of markets

Source filtered on `m["category"]` against a configured list. **The markets
endpoint returns no `category` field at all.** Every market was discarded while
the agent looked healthy — the exact silent-failure mode the docs warn about.

**Fixed:** replaced with `exclude_provisional` + `require_two_sided_quote`, and
a warning when a scan yields zero tradeable markets.

> A scan of **1,200 open markets returned exactly one** with a two-sided quote.
> `status=open` is dominated by unquoted provisional multivariate markets, so
> this filtering is what makes the list usable at all.

### 7. 🟡 Unjustified Bayesian constant

`lr = 1.0 + 4.0 * confidence`. The `4.0` was chosen, not fitted.

**Fixed (partially):** moved to `settings.likelihood_strength` with an explicit
warning that it is unfitted. It should be set from calibration data — see
`VALIDATION.md`.

### 8. 🟡 Timezone crash

Source compared a tz-aware cutoff against tz-naive SQLite values, raising
`TypeError` as soon as any trade existed.

**Fixed:** `DateTime(timezone=True)` throughout.

---

## New capability not in the source

| Addition | Why |
| --- | --- |
| `fees.py` with `net_edge()` | Trade decisions clear real costs, not a flat 0.08 constant |
| Book depth in snapshots | Position size capped by liquidity that actually existed |
| Liquidity gate | Never take more than 25% of resting depth |
| Time-to-close gate | Skip markets resolving within the hour |
| **All** predictions logged | Logging only traded ones biases calibration upward |
| `event_group` on predictions | Effective sample size — 15 positions from one story aren't 15 samples |
| Rejection logging with reasons | "47 candidates, all below threshold" ≠ "0 candidates" |
| Source-health tracking | Dead RSS feeds are loud instead of silent |
| `main.py --check` | Verifies auth, connectivity, and book parsing in one command |

---

## Verified working

```
$ .venv/bin/python -m pytest tests/ -q
12 passed

$ .venv/bin/python main.py --check
✅ exchange status: exchange_active True, trading_active True
✅ fetched 1 markets
✅ orderbook: bid=0.02(75) ask=0.98(75) valid=True

$ .venv/bin/python main.py --once
tick done: 33 news, 33 signals, 20 markets, 0 considered, 0 traded, 0 rejected
```

Zero trades on that tick is **correct**: the demo environment lists MLB/sports
markets while the RSS feeds carry politics, so nothing matches. That is the
market-matching weakness documented in `ARCHITECTURE.md` showing up in practice
rather than a failure.

---

## Next tasks, in order

1. **Improve market↔news matching.** Keyword overlap is the weakest link in the
   pipeline and its failures are silent. Worth more than any model upgrade.
2. **Port `track_record.py`**, rewired to Kalshi settlement — this is the module
   that actually answers "am I making money."
3. **Add benchmark forecasters** (market-price, random, always-favorite).
4. **Deploy always-on** with Postgres (`OPERATIONS.md`).
5. **Pre-register** decision criteria, then run to 200+ resolutions
   (`VALIDATION.md`).

---

## Attribution

Derived from `priyanshshahh/polymarket-sentiment-agent`, MIT licensed,
© 2026 Priyansh Shah. See `NOTICE`.
