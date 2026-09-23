"""Async SQLite engine and short-lived session dependency."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./feature_flags.db"

_engine: AsyncEngine | None = None


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def _normalize_async_sqlite_url(database_url: str) -> str:
    """Ensure SQLite URLs use the aiosqlite driver."""
    if database_url.startswith("sqlite+aiosqlite:"):
        return database_url
    if database_url.startswith("sqlite:"):
        return database_url.replace("sqlite:", "sqlite+aiosqlite:", 1)
    return database_url


def create_db_engine(database_url: str | None = None) -> AsyncEngine:
    url = _normalize_async_sqlite_url(database_url or get_database_url())
    return create_async_engine(url, echo=False)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def init_engine(database_url: str) -> AsyncEngine:
    """Replace the process engine (used by tests for in-memory / temp SQLite)."""
    global _engine
    _engine = create_db_engine(database_url)
    return _engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    session = AsyncSession(get_engine(), expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
