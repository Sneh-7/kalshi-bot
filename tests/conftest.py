"""Test setup: bind the whole suite to a throwaway SQLite DB.

This MUST run before kalshi_bot is imported so the config/database singletons
pick up the temp path instead of the real data/kalshi.db.
"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="kalshi_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmpdir}/test.db")

import pytest  # noqa: E402


@pytest.fixture
def db():
    """Fresh schema with all tables emptied before the test."""
    from kalshi_bot.database import init_db, session_scope
    from kalshi_bot import models

    init_db()
    with session_scope() as s:
        for model in (
            models.Trade, models.Recommendation, models.PredictionRecord,
            models.MarketResolution, models.MarketSnapshot, models.Signal,
            models.NewsItem, models.AgentState, models.LogEvent,
        ):
            s.query(model).delete()
    yield
