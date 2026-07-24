# Validation — "am I actually making money?"

**Written 2026-07-24.** This document exists to answer one question rigorously:

> Does this algorithm have a real edge over the actual Kalshi market, or does it just
> look like it does?

That distinction is the whole game. A paper-trading bot that reports profit is
trivially easy to build. A paper-trading bot whose profit **predicts live profit** is
hard, and almost every retail trading system fails at precisely this point — not at
the engineering, at the measurement.

---

## Contents

1. [Why paper P&L lies](#why-paper-pl-lies)
2. [The measurement stack](#the-measurement-stack)
3. [Metrics that matter](#metrics-that-matter)
4. [Benchmarks — profit is meaningless alone](#benchmarks)
5. [Sample size and pre-registration](#sample-size-and-pre-registration)
6. [The pitfalls list](#the-pitfalls-list)
7. [Your decision rule](#your-decision-rule)
8. [Implementation checklist](#implementation-checklist)
9. [Sources](#sources)

---

## Why paper P&L lies

Your current code, if ported unfixed, will overstate returns by roughly **3–4¢ per
contract per round trip** — on $1 contracts, that's 3–4% of notional. For context,
realistic sentiment edges in liquid prediction markets are typically low single-digit
percentages, if they exist at all. **The measurement error is the same size as the
thing being measured.**

Three compounding sources:

### 1. Fills at the midpoint (~1–2¢)

`execution.py` fills paper trades at `plan.price` — the **midpoint**. Real taker
orders cross the spread and fill at the ask. Kalshi markets are frequently 2–4¢ wide,
so the simulator hands you the half-spread for free on every single trade.

### 2. Zero fees (~1.75¢ at 50¢)

`grep -i fee` across the backend returns nothing. Kalshi charges
`0.07 × C × (1−C)` per contract as taker — 1.75¢ at 50¢, and fees **peak exactly
where sentiment trading lives**.

### 3. Infinite liquidity (variable, sometimes huge)

Nothing caps order size against actual book depth. Paper "fills" 500 contracts
instantly in a market with 40 resting. Live, you'd move the price against yourself or
not fill at all — and thin markets are exactly where mispricings appear, so this
error is *correlated with* your apparent edge. The trades that look best are the ones
most likely to be unfillable.

> **These three are not conservatism issues. They are the difference between a
> strategy that makes money and one that doesn't.** Fix them before you run anything
> you intend to believe.

---

## The measurement stack

Four layers. Most people build the first and stop.

```
┌──────────────────────────────────────────────────────────┐
│ 4. DECISION RULE — pre-registered, before you look       │  ← almost nobody
├──────────────────────────────────────────────────────────┤
│ 3. BENCHMARKS — vs. market price, random, buy-favorite   │  ← rarely
├──────────────────────────────────────────────────────────┤
│ 2. CALIBRATION — Brier, log-loss, reliability bins       │  ← sometimes
├──────────────────────────────────────────────────────────┤
│ 1. P&L — realistic fills, fees, depth caps               │  ← everyone
└──────────────────────────────────────────────────────────┘
```

Layer 1 alone cannot distinguish skill from luck at any sample size you'll reach in
months. **Layers 2 and 3 are what make the result falsifiable**, and layer 4 is what
stops you fooling yourself after the fact.

The good news: `track_record.py` in the sibling repo already implements most of
layer 2 — Brier, log-loss, calibration bins, and an `insufficient_data` gate. It
needs rewiring from Polymarket's Gamma resolution API to Kalshi settlement.

---

## Metrics that matter

### Brier score — primary

Mean squared error of your probability forecasts:

```
Brier = (1/N) Σ (p_forecast − outcome)²        outcome ∈ {0, 1}
```

Lower is better. Reference points:

| Brier | Reading |
| --- | --- |
| 0.25 | Always guessing 0.50 — **the zero-information baseline** |
| < 0.15 | Good |
| < 0.10 | Excellent |
| > 0.25 | **Worse than useless** — you're actively anti-informative |

**Compare against the market's own Brier on the same markets**, not against these
absolute numbers. Prediction markets are well-calibrated; beating 0.25 is easy and
means nothing. Beating *the market price* is the only result that implies edge.

### Calibration — is 60% actually 60%?

Bucket predictions by forecast probability and check realized frequency:

| Forecast bucket | Predictions | Realized YES rate | Ideal |
| --- | --: | --: | --- |
| 0.0–0.1 | 40 | 0.05 | ~0.05 |
| 0.4–0.5 | 85 | 0.62 | ~0.45 ⚠️ |
| 0.9–1.0 | 30 | 0.93 | ~0.95 |

Systematic deviation reveals **fixable bias**. If your 40–50% bucket resolves YES 62%
of the time, you're systematically underconfident there — and that's correctable by
tuning the Bayesian likelihood ratio, not by abandoning the strategy.

**This is what should set the arbitrary `4.0` constant** in `bayesian_update()`.
Right now it's a guess; calibration data turns it into a fitted parameter.

### P&L — necessary, not sufficient

Track it, but never as the primary signal. Over 200 trades, a strategy with zero edge
produces positive P&L a large fraction of the time by chance. P&L is high-variance
and slow; calibration converges faster and tells you *why*.

### Where edge is plausible

Research consistently finds **mid-range markets (30–70%) are the best calibrated** —
which is where informational competition concentrates. That cuts both ways for you:

- It's where your sentiment signal is most likely to *matter* (genuine uncertainty).
- It's where you're **least likely to beat the market**, and where fees peak.

Extreme-priced markets (< 10%, > 90%) are more often mispriced but have terrible
risk/reward: at 95¢ you risk 95¢ to make 5¢, and one wrong call erases nineteen right
ones. **Favorite-longshot bias is real, but it is not free money.**

---

## Benchmarks

"The bot made $47" is not a result. Compare against all four:

| Benchmark | How | What it tests |
| --- | --- | --- |
| **Market price** | Forecast = current price, always | **The one that matters.** Zero edge by construction. If you can't beat it, your signal is noise. |
| **Random entry** | Same sizing, random side | Are you better than a coin flip after costs? |
| **Always-favorite** | Always buy the > 50% side | Captures the naive strategy most people accidentally implement |
| **Do nothing** | No trades | Fees and spread make this beat many active strategies |

**Beating "market price" is the bar.** Everything else is a sanity check.

The subtlety: predicting outcomes well is *not* the same as making money. You could
have a better Brier than the market and still lose, if your edge is smaller than
costs. Both must clear.

---

## Sample size and pre-registration

### How many resolutions?

**Minimum 200 resolved positions before drawing any conclusion.** Even then, treat a
positive result as weak evidence.

Why so many: to detect a 3% edge with reasonable confidence against binary outcome
noise, hundreds of independent observations are needed. And "independent" is doing
heavy lifting — see correlation below.

### The correlation problem

**Your 200 trades are not 200 independent samples.** If one news cycle generated 15
Trump-related positions, that's closer to one observation at 15× size. Sentiment
strategies are structurally prone to this: a big story creates many correlated
signals at once.

**Track effective sample size**, grouping by event/news-cycle, not just row count.
Your true N may be a small fraction of your trade count — and that's the number your
confidence should be based on.

### Pre-register your decision

Before running, write down and commit:

1. How many resolutions constitute a verdict (e.g. 200)
2. What result means "go live" (e.g. Brier beats market's by ≥ 0.01 **and** net P&L
   after modeled costs > 0)
3. What result means "abandon"
4. How long you'll run regardless of interim results

**Do this first.** Otherwise you will stop when the number looks good — that's not a
character flaw, it's the default behavior of anyone watching a live equity curve, and
it converts noise into a "strategy." Committing the criteria to git beforehand is the
cheapest guard that exists.

---

## The pitfalls list

Each of these has sunk real systems.

| Pitfall | Why it kills you | Guard |
| --- | --- | --- |
| **Optimistic fills** | Overstates by 1–2¢/contract | Fill at ask/bid, never mid |
| **Missing fees** | Overstates by ~1.75¢/contract | Model `0.07×C×(1−C)` |
| **Infinite liquidity** | Best-looking trades are least fillable | Cap size at a fraction of book depth |
| **Lookahead bias** | Using data unavailable at decision time | Record decision timestamps; only use data with earlier timestamps |
| **Survivorship** | Only counting resolved markets | Track voided/delisted too |
| **Correlated positions** | Inflates apparent N | Group by event; track effective N |
| **Stopping early on a win** | Turns noise into "edge" | Pre-register sample size |
| **Threshold tuning on the same data** | Overfitting — filtering harder always improves in-sample scores | Hold out data; re-tune only on fresh samples |
| **Ignoring the losing paths** | Rejections/no-fills are data | Log every rejected trade and why |
| **Silent source failure** | Degrades gradually, unnoticed | Alert on zero-item feeds |
| **Resolution ambiguity** | Some markets settle unexpectedly | Require decisive settlement before scoring |

That eighth one deserves emphasis: research on prediction-market forecasting notes
that **Brier scores improve under more aggressive filtering simply because filtering
retains better-calibrated rows** — not because the forecaster got better. If you tune
your edge threshold until backtest results look good, you have measured your tuning,
not your strategy.

---

## Your decision rule

A concrete, pre-registerable template. Adjust the numbers, but commit before running.

```
RUN:      Paper mode, demo environment, continuous
UNTIL:    200 resolved positions AND ≥ 6 weeks elapsed
          (both — time guards against one abnormal news cycle)

GO LIVE only if ALL of:
  ✅ Brier score < market's Brier on the same markets (by ≥ 0.01)
  ✅ Calibration: no bucket off by > 0.15 with ≥ 20 samples
  ✅ Net P&L after modeled fees + realistic fills > 0
  ✅ Beats all four benchmarks
  ✅ Effective sample size (event-grouped) ≥ 50
  ✅ Max drawdown tolerable at intended live size

ABANDON OR REWORK if:
  ❌ Brier ≥ market's
  ❌ Net P&L negative after costs
  ❌ Edge exists only in markets you couldn't actually fill

IF GOING LIVE: start at 10% of intended size for another 100 resolutions.
```

That last line matters. Paper→live always surprises: real slippage, real rejections,
real latency. Treat the first live phase as continued measurement, not deployment.

---

## Implementation checklist

Ordered. Each step gates the next.

- [ ] **Fix fills** — buy at `best_ask`, sell at `best_bid`
- [ ] **Model fees** — `0.07 × C × (1−C)`, maker at 25%
- [ ] **Cap size by depth** — never exceed a set fraction of resting liquidity
- [ ] **Log decision timestamps** — the guard against lookahead bias
- [ ] **Log rejected trades** — with the reason; rejections are data
- [ ] **Port `track_record.py`** — rewire Polymarket Gamma → Kalshi settlement
- [ ] **Add benchmark forecasters** — market-price, random, always-favorite, do-nothing
- [ ] **Add event grouping** — for effective sample size
- [ ] **Add a calibration report** — bucket table, exposed via API
- [ ] **Pre-register criteria** — commit to git before starting
- [ ] **Deploy always-on** with durable Postgres ([`OPERATIONS.md`](OPERATIONS.md))
- [ ] **Run. Wait. Don't touch it.** ← the hard part
- [ ] Evaluate against pre-registered criteria

**Steps 1–3 are a few hours of work and determine whether every subsequent number
means anything.**

---

## An honest expectation

Kalshi markets are reasonably efficient, and prediction markets are well-calibrated
in aggregate — that's the central finding of the academic literature on them. Bots
are increasingly active there. Your competition on any liquid, news-driven market
includes people doing this full-time with better latency.

The realistic outcome distribution:

- **Most likely:** no reliable edge after costs. This is the modal result, and
  discovering it in paper for $0 is a *success* — it's exactly what the exercise is
  for.
- **Possible:** a small edge in a narrow niche — under-followed markets, a specific
  category, a particular news type. Niches are where retail edges actually live.
- **Unlikely:** a broad edge across many markets. If you appear to find this, suspect
  a measurement bug first. Look hardest at fills and fees.

None of that is a reason not to build it. It's a reason to **build the measurement
layer as carefully as the trading layer** — because the valuable output here is a
trustworthy answer, and a trustworthy "no" is worth far more than an untrustworthy
"yes."

---

## Sources

- [Beyond Forecasting: The Belief-to-Trade Layer in Prediction-Market Agents (arXiv)](https://arxiv.org/html/2607.03015v1)
- [PolySwarm: A Multi-Agent LLM Framework for Prediction Market Trading (arXiv)](https://arxiv.org/html/2604.03888v1)
- [How Accurate Are Prediction Markets? — TradeAlgo](https://www.tradealgo.com/trading-guides/prediction-markets/prediction-market-accuracy)
- [Polymarket Prediction Accuracy: Track Record & Brier Score — Fensory](https://fensory.com/intelligence/predict/polymarket-accuracy-analysis-track-record-2026)
- [Everyone Measures Prediction Markets Wrong — Medium](https://medium.com/@numacodes/everyone-measures-prediction-markets-wrong-heres-what-they-re-missing-d8a477930d1c)
- [Kalshi Fees 2026 — pm.wiki](https://pm.wiki/learn/kalshi-fees-explained)
