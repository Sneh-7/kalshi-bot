"""Central configuration loaded from environment / .env.

Single contract between operator and runtime. Keep thin.
"""
from __future__ import annotations

from typing import List, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---------------------------------------------------------
    log_level: str = "INFO"
    # SQLite is fine locally. Use Postgres for any multi-week run — ephemeral
    # hosts wipe SQLite on restart and you lose the whole sample.
    database_url: str = "sqlite:///./data/kalshi.db"

    # --- Agent loop ------------------------------------------------------
    loop_interval_seconds: int = 30
    min_signal_confidence: float = 0.55
    # Emit a heartbeat / watchdog alert if a loop tick has not completed within
    # this many minutes. 0 disables.
    heartbeat_minutes: int = 30

    # --- Trading mode ----------------------------------------------------
    # PAPER     = simulated fills at the ask, fees modeled. Default.
    # RECOMMEND = stage a pending recommendation for manual approval.
    # LIVE      = place real orders. Requires keys AND explicit opt-in.
    trading_mode: Literal["PAPER", "RECOMMEND", "LIVE"] = "PAPER"

    # --- Risk (MODERATE profile: ~$500-700 of a $1k bankroll deployed) ----
    # Total account bankroll. Kelly sizing is computed against `deployable_capital`,
    # not the full bankroll, so a cash reserve is always held back.
    bankroll_usd: float = 1000.0
    deployable_capital_usd: float = 600.0
    # Never stake more than this fraction of deployable capital on one trade.
    max_trade_fraction: float = 0.05
    # Fractional-Kelly multiplier applied to the raw Kelly stake (0.5 = half-Kelly).
    # The analyst may suggest its own kelly_fraction; the smaller of the two wins.
    kelly_fraction: float = 0.5

    max_usd_per_trade: float = 30.0
    max_open_positions: int = 8
    daily_drawdown_usd: float = 75.0
    kill_switch: bool = False

    # Minimum NET edge (after fees and half-spread) required to trade.
    # This is compared against fee-adjusted edge, not raw model-minus-market.
    # See docs/ARCHITECTURE.md#edge-math.
    min_net_edge: float = 0.02

    # Never take more than this fraction of resting book depth. Guards against
    # "fills" that could not happen live in thin markets.
    max_book_fraction: float = 0.25

    # Skip markets resolving sooner than this — no time for a thesis to play out.
    min_minutes_to_close: int = 60

    # --- Kalshi Trade API v2 ---------------------------------------------
    # Keys: kalshi.com → Profile → API Keys. The private key PEM is shown ONCE.
    # Store it outside the repo (secrets/ is gitignored).
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = ""

    # Verified 2026-07-24 against docs.kalshi.com.
    #   production https://external-api.kalshi.com/trade-api/v2
    #   demo       https://external-api.demo.kalshi.co/trade-api/v2   (.co!)
    # Default to DEMO — reaching production must be a deliberate act.
    kalshi_base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"

    # --- Fees (Kalshi, verified 2026-07-24) ------------------------------
    # taker = fee_rate * C * (1 - C) per contract; maker = maker_multiplier * taker
    kalshi_fee_rate: float = 0.07
    kalshi_maker_multiplier: float = 0.25
    # Assumed cost of crossing the spread when book depth is unknown, in dollars.
    assumed_slippage: float = 0.005

    # --- News sources (Scout) --------------------------------------------
    rss_feeds: str = (
        "https://feeds.reuters.com/reuters/topNews,"
        "https://feeds.politico.com/politico/rss/politicopicks.xml,"
        "https://thehill.com/news/feed/,"
        "https://apnews.com/hub/politics.rss,"
        "https://www.cbsnews.com/latest/rss/politics,"
        "https://feeds.npr.org/1001/rss.xml"
    )
    # Alert if a feed returns zero items this many cycles running.
    source_dead_after_cycles: int = 3

    # --- Truth Social (Trump posts / "what will Trump say" markets) -------
    # Free path reads @realDonaldTrump's public statuses via Truth Social's
    # Mastodon-compatible API (no auth). Unofficial and can break; a paid
    # scraper fallback engages when `truth_social_provider = paid`.
    truth_social_enabled: bool = True
    truth_social_username: str = "realDonaldTrump"
    truth_social_base_url: str = "https://truthsocial.com/api/v1"
    truth_social_max_posts: int = 20
    truth_social_provider: Literal["free", "paid"] = "free"
    # Paid fallback (e.g. ScrapeCreators / Apify / SocialCrawl). Only used when
    # provider == "paid" (or the free path is flagged dead and one is configured).
    truth_social_api_url: str = ""
    truth_social_api_key: str = ""

    # --- Telegram (alerts + control) -------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Poll interval for inbound commands / approval callbacks.
    telegram_poll_seconds: int = 3

    # --- Settlement / calibration ----------------------------------------
    settlement_enabled: bool = True

    # --- Browser fallback execution (Claude-in-Chrome / Playwright) -------
    # A guarded safety-net execution channel used ONLY when the API order path
    # fails. Fragile and slow — never the primary path. Requires optional
    # `playwright` (see requirements.txt) and a saved login session.
    browser_exec_enabled: bool = False
    browser_headless: bool = True
    # Persisted Playwright storage state (log into Kalshi once, save cookies).
    browser_storage_state_path: str = "secrets/kalshi_storage_state.json"
    browser_base_url: str = "https://kalshi.com"
    # Safety: stop BEFORE clicking the final submit and just log the intended
    # action until selectors are verified. Set false only after you've confirmed
    # the flow works on your account.
    browser_dry_run: bool = True

    # --- LLM: two tiers ---------------------------------------------------
    # Tier 1 (cheap, runs on EVERY headline): sentiment label only. Provider
    # chain Groq -> OpenAI -> Anthropic(Haiku) -> keyword heuristic.
    # Tier 2 (the ANALYST): Claude Opus makes the actual bet decision on a small
    # shortlist of finalists. Groq is the analyst fallback; heuristic is last resort.
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    # Cheap classifier model (Tier 1).
    anthropic_model: str = "claude-haiku-4-5"

    # --- Analyst (Tier 2 — Claude Opus makes the bet decisions) -----------
    analyst_enabled: bool = True
    # Claude is the primary decision-maker; Groq is fallback only.
    analyst_model: str = "claude-opus-4-8"
    # cli    = run the Claude Code CLI (`claude -p`) on your subscription (no API bill)
    # claude = call the Anthropic API (needs ANTHROPIC_API_KEY); best for 24/7 servers
    # groq   = use Groq as primary
    analyst_provider: Literal["cli", "claude", "groq"] = "claude"

    # --- Claude Code CLI provider (subscription-based, no API key) --------
    # Used when analyst_provider = "cli". Runs the local `claude` binary headless.
    claude_cli_path: str = "claude"
    # Model alias the CLI understands: opus | sonnet | haiku (or a full model id).
    analyst_cli_model: str = "opus"
    # Hard timeout (seconds) per CLI decision — a spawned process is slower than
    # a direct API call, and must never hang the trading loop.
    analyst_cli_timeout: int = 90
    # Only the top-N candidates (by preliminary net edge) reach Claude, which
    # bounds Claude API spend. Raise for more coverage, lower to cut cost.
    analyst_max_finalists: int = 8
    # Preliminary net-edge gate applied BEFORE the analyst runs (cheaper than
    # min_net_edge, which is the final gate after the analyst re-prices).
    analyst_prelim_min_edge: float = 0.0
    # Claude effort level: low | medium | high | xhigh | max.
    analyst_effort: str = "high"

    # Strength of a single headline in the Bayesian update.
    # NOTE: unfitted. Calibration data should set this — see docs/VALIDATION.md.
    likelihood_strength: float = 4.0

    # --- Market focus ----------------------------------------------------
    # All Kalshi categories EXCEPT crypto: geopolitics/mentions, commodities,
    # sports, finance, culture, economics, tech & science, elections & climate,
    # with special emphasis on "what will Trump say" speech/mention markets.
    watch_markets: str = ""
    market_keywords: str = (
        "trump, say, mention, speech, address, sotu, meeting, press, "
        "tariff, fed, rates, inflation, jobs, gdp, cpi, election, war, "
        "ukraine, israel, gaza, china, iran, oil, gas, gold, wheat, "
        "nfl, nba, mlb, soccer, world cup, oscar, box office, "
        "ai, nvidia, apple, chip, space, nasa, "
        "hurricane, temperature, climate, weather"
    )
    # Markets whose title/tickers contain any of these are DROPPED entirely
    # (crypto is out of scope per the operator).
    exclude_keywords: str = (
        "bitcoin, btc, ethereum, eth, crypto, solana, sol, xrp, ripple, "
        "dogecoin, doge, cardano, ada, litecoin, memecoin, altcoin, stablecoin"
    )
    max_markets: int = 30

    # The markets endpoint returns NO category field, so category filtering is
    # impossible there — the source's category filter silently dropped 100% of
    # markets. These two filters replace it.
    #
    # Provisional multivariate markets dominate `status=open` and are almost
    # all unquoted: 1,200 open markets scanned on 2026-07-24 yielded exactly
    # one two-sided quote.
    exclude_provisional: bool = True
    require_two_sided_quote: bool = True

    # --- Derived ---------------------------------------------------------
    @property
    def rss_list(self) -> List[str]:
        return [u.strip() for u in self.rss_feeds.split(",") if u.strip()]

    @property
    def keyword_list(self) -> List[str]:
        return [k.strip().lower() for k in self.market_keywords.split(",") if k.strip()]

    @property
    def exclude_keyword_list(self) -> List[str]:
        return [k.strip().lower() for k in self.exclude_keywords.split(",") if k.strip()]

    @property
    def watch_list(self) -> List[str]:
        return [m.strip() for m in self.watch_markets.split(",") if m.strip()]

    @property
    def is_demo(self) -> bool:
        return "demo" in self.kalshi_base_url.lower()

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()
