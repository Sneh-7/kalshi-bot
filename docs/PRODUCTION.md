# Production build — Claude analyst, Truth Social, Telegram, settlement

This layer turns the headless paper bot into a production, 24/7 system where
**Claude Opus makes the actual bet decisions**. It is additive — the original
Scout → Quant → Oracle → Edge → Overseer → Trader pipeline is intact.

## What was added

| Area | Module | Notes |
|---|---|---|
| Claude decision stage | `kalshi_bot/modules/analyst.py` | Claude Opus (`claude-opus-4-8`) prices each finalist and returns a structured decision (side / probability / confidence / kelly_fraction / rationale). Groq fallback, heuristic last resort. Prompt-cached system prompt. |
| Two-tier loop | `orchestrator.py` | Cheap sentiment + Bayesian pre-filter ranks candidates; only the top `ANALYST_MAX_FINALISTS` reach Claude — bounds cost. |
| Truth Social | `modules/ingestion.py` | Free Mastodon-compatible scrape of `@realDonaldTrump`, paid fallback if it breaks. Feeds "what will Trump say" markets. |
| Telegram | `modules/notifier.py` | Trade cards, approval buttons, `/status /pnl /calibration /positions /pause /resume /kill`. Free. |
| Settlement + calibration | `modules/settlement.py` | Polls Kalshi resolutions, settles positions, scores Brier/log-loss vs the market. The paper-week success metric. |
| Kelly sizing | `modules/risk.py::kelly_contracts` | Fractional-Kelly, MODERATE profile: half-Kelly × analyst conviction, capped by `MAX_TRADE_FRACTION` and `MAX_USD_PER_TRADE`. |
| Browser fallback | `modules/browser_exec.py` | Optional Playwright ("Claude-in-Chrome") channel used only when the API order path fails. Dry-run by default. |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then fill in keys
python3 main.py --check      # verifies Kalshi, Truth Social, Telegram, Claude
```

Keys you need (all free except Claude): `ANTHROPIC_API_KEY` (Claude decisions),
`GROQ_API_KEY` (free fallback), `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
(alerts/control), and — for real trading — `KALSHI_KEY_ID` + a private-key PEM.

## Run phases (staged for capital safety)

1. **Paper (~1 week), DEMO.** `TRADING_MODE=PAPER`, `KALSHI_BASE_URL` = demo.
   Let it run; then `/calibration` in Telegram. **Only proceed if the model's
   Brier/log-loss beats the market.** No edge → do not risk real money.
2. **Approval-gated real money.** `TRADING_MODE=RECOMMEND`, `KALSHI_BASE_URL` =
   production, real credentials. Each bet arrives in Telegram with Approve/Reject;
   approving places the live order.
3. **Optional full-auto.** `TRADING_MODE=LIVE` once you trust the system.

Browser fallback is opt-in: `BROWSER_EXEC_ENABLED=true`, save a login session
(`python -m kalshi_bot.modules.browser_exec --login`), keep `BROWSER_DRY_RUN=true`
until you've confirmed the click-through, then set it false.

## 24/7 hosting

Use Postgres (`DATABASE_URL=postgresql+psycopg2://…`) so restarts don't wipe the
sample. Run `python3 main.py` under a supervisor — launchd on Mac now, systemd or
a `--restart` container on a VPS later. The watchdog alerts over Telegram if a
loop tick stalls beyond `HEARTBEAT_MINUTES`.

## Cost

Free: Kalshi API, Groq, Truth Social, RSS, Telegram. Variable: Claude Opus, kept
to a few dollars/day by only sending `ANALYST_MAX_FINALISTS` candidates per tick
and prompt-caching the system prompt. Lower `ANALYST_MAX_FINALISTS` to cut spend.
