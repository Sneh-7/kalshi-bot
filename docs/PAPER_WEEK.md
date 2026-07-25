# The Paper Week — run it, prove it, before any real money

The paper week is the whole point of the project's first phase. You run the full
system with **simulated fills and zero money at risk**, let it accumulate real
predictions against real Kalshi markets, and then check one number: **does
Claude's probability beat the market's?** If yes, you have evidence of an edge and
can move to real money. If no, you've spent $0 learning that — which is the point.

Nothing here risks money. No Kalshi trading keys are needed.

---

## 1. What you need

| Thing | Required? | Cost | Notes |
| --- | --- | --- | --- |
| A place to run it 24/7 | ✅ | $0 | Your Mac (only while awake) or a free VPS — see [`DEPLOY.md`](DEPLOY.md) |
| Claude (decisions) | ✅ | $0 | Local: `ANALYST_PROVIDER=cli` (your subscription). Hosted: `claude` + `ANTHROPIC_API_KEY` (covered by promo credits) |
| Kalshi market data | ✅ | $0 | Public — no key |
| Truth Social + RSS | ✅ | $0 | Free scrape / feeds |
| Groq API key | optional | $0 | Free fallback if Claude is busy |
| Telegram bot | optional | $0 | Phone alerts + `/status`, `/pnl`, `/calibration` |
| Kalshi trading keys | ❌ **NOT yet** | — | Only after the calibration gate passes |

## 2. Recommended paper config (`.env`)

Point at **production market data** even in paper mode — that's where the real
political / "what will Trump say" markets live. PAPER only simulates fills, so
this needs no key and risks nothing.

```
TRADING_MODE=PAPER
KALSHI_BASE_URL=https://external-api.kalshi.com/trade-api/v2

# Local on your Mac:
ANALYST_PROVIDER=cli
# On a hosted server instead:
# ANALYST_PROVIDER=claude
# ANTHROPIC_API_KEY=sk-ant-...

GROQ_API_KEY=            # optional, free fallback
TELEGRAM_BOT_TOKEN=      # optional
TELEGRAM_CHAT_ID=        # optional

# Leave these BLANK for the paper week:
KALSHI_KEY_ID=
```

## 3. Launch

**Local (quick test / when your Mac is on):**
```bash
source .venv/bin/activate
python3 main.py --check          # expect ✅ Claude, ✅ markets, ✅ Truth Social
python3 main.py                  # runs continuously; Ctrl+C to stop
```

**Hosted 24/7 (recommended for the full week)** — Docker, per [`DEPLOY.md`](DEPLOY.md):
```bash
docker compose run --rm bot python main.py --check
docker compose up -d --build
docker compose logs -f
```

## 4. What "working" looks like

`--check` prints something like:
```
✅ exchange status: {...}
✅ fetched 30 markets
✅ orderbook: bid=0.44(10) ask=0.51(9) valid=True
✅ Truth Social: 17 recent posts (free)
✅ Claude CLI (opus): OK          # or "Claude analyst API (...)" when hosted
```

Once running, each cycle logs a tick summary:
```
tick done: 12 news, 12 signals, 30 markets, 3 considered, 1 traded, 2 rejected
```
- **considered** = candidates that reached Claude
- **traded** = simulated paper fills recorded
- **rejected** = failed a risk/edge gate (this is healthy — most candidates should not trade)

A quiet tick (`0 considered`) just means no fresh news matched a tradeable market.

## 5. Monitor it

- **Telegram:** `/status` (mode, positions, 24h P&L), `/pnl`, `/positions`,
  `/calibration`, `/pause`, `/resume`, `/kill`.
- **Logs:** `docker compose logs -f` (hosted) or the terminal (local).
- The watchdog sends a Telegram alert if the loop stalls beyond `HEARTBEAT_MINUTES`.

## 6. Daily / weekly checklist

- **Daily (30 sec):** `/status` — is it still ticking? Is P&L sane? Any error alerts?
- **Mid-week:** `/calibration` — once markets start resolving you'll see the first
  Brier/log-loss numbers (needs resolved markets, so early days may show "n=0").
- **End of week:** the gate below.

## 7. The gate — did it actually work?

Run `/calibration` (or read the settlement report). You get:
```
📈 Calibration (n=…)
Brier  model 0.18 vs market 0.22
LogLoss model 0.52 vs market 0.61
✅ model beats market
```
- **Brier / log-loss:** lower is better; they measure how well-calibrated the
  probabilities were against actual outcomes.
- **The decision:** proceed to real money **only if the model beats the market**
  on a meaningful sample (aim for dozens of resolved predictions, more is better —
  see [`VALIDATION.md`](VALIDATION.md)). If it doesn't beat the market, the signal
  is noise; do not risk money. Tune and re-run instead.

## 8. Common issues

| Symptom | Cause / fix |
| --- | --- |
| `❌ exchange status ... 503` on DEMO | Kalshi's demo exchange is momentarily inactive; harmless if markets still fetch. Using the prod URL (recommended) avoids it. |
| `Truth Social: 0 posts` for several cycles | Free scrape blocked; set `TRUTH_SOCIAL_PROVIDER=paid` + a scraper API, or leave it — RSS still feeds signals. |
| `Claude CLI ... not found` when hosted | Servers can't run the CLI; set `ANALYST_PROVIDER=claude` + `ANTHROPIC_API_KEY`. |
| `0 considered` every tick | Thin market match or quiet news. Broaden `MARKET_KEYWORDS`, or it's just a slow news period. |
| Calibration shows `n=0` | No markets have resolved yet — normal early in the week. |

## 9. What NOT to do during the paper week

- Don't set `TRADING_MODE` to `RECOMMEND`/`LIVE`.
- Don't add Kalshi trading keys.
- Don't judge the strategy on P&L alone — use calibration vs. the market benchmark.
- Don't stop early because the number looks good on a handful of trades.

When the gate passes, continue with the "after the paper week" section of
[`DEPLOY.md`](DEPLOY.md).
