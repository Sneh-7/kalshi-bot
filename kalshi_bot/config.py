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

    # --- Trading mode ----------------------------------------------------
    # PAPER     = simulated fills at the ask, fees modeled. Default.
    # RECOMMEND = stage a pending recommendation for manual approval.
    # LIVE      = place real orders. Requires keys AND explicit opt-in.
    trading_mode: Literal["PAPER", "RECOMMEND", "LIVE"] = "PAPER"

    # --- Risk ------------------------------------------------------------
    max_usd_per_trade: float = 25.0
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

    # --- LLM (used ONLY as a text classifier) ----------------------------
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    # Strength of a single headline in the Bayesian update.
    # NOTE: unfitted. Calibration data should set this — see docs/VALIDATION.md.
    likelihood_strength: float = 4.0

    # --- Market focus ----------------------------------------------------
    watch_markets: str = ""
    market_keywords: str = (
        "trump, tariff, fed, rates, inflation, jobs, gdp, election, war, "
        "ukraine, israel, gaza, oil, gas, bitcoin, crypto, sec"
    )
    max_markets: int = 20

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
    def watch_list(self) -> List[str]:
        return [m.strip() for m in self.watch_markets.split(",") if m.strip()]

    @property
    def is_demo(self) -> bool:
        return "demo" in self.kalshi_base_url.lower()


settings = Settings()
