"""SQLAlchemy engine + session scope.

SQLite by default for local work. Point DATABASE_URL at Postgres for anything
long-running — ephemeral hosts wipe SQLite on restart, which silently destroys
a multi-week validation sample.

Schema is denormalized on purpose: every row captures the full reasoning
context at decision time, so a post-mortem is one SELECT.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .config import settings


def normalize_db_url(url: str) -> str:
    """Map provider-style Postgres URLs onto the psycopg2 dialect.

    Neon/Heroku/Render hand out `postgres://` (sometimes plain `postgresql://`);
    SQLAlchemy 2.x dropped the `postgres://` alias, so following the hosting
    provider's own connection string fails at create_engine without this.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _prepare_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix):
        raw = url[len(prefix):]
        if raw and raw != ":memory:":
            Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_db_url = normalize_db_url(settings.database_url)
_prepare_sqlite_dir(_db_url)

_is_sqlite = _db_url.startswith("sqlite")
engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
    future=True,
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False,
)
Base = declarative_base()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registers tables)
    Base.metadata.create_all(bind=engine)
