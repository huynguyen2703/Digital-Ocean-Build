"""Tests for async database engine and short-lived sessions."""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Field, SQLModel, select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app import database

pytestmark = pytest.mark.asyncio


async def test_get_session_yields_usable_session_and_closes() -> None:
    database.init_engine("sqlite+aiosqlite:///:memory:")
    gen = database.get_session()
    session = await gen.__anext__()
    session.close = AsyncMock(wraps=session.close)

    assert isinstance(session, AsyncSession)
    result = await session.exec(select(1))
    assert result.one() == 1

    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()

    session.close.assert_awaited_once()


async def test_engine_can_create_metadata_for_a_model() -> None:
    """Light schema check; full Flag tables arrive in Task 2."""

    class _SmokeTable(SQLModel, table=True):
        __tablename__ = "task1_smoke"
        id: Optional[int] = Field(default=None, primary_key=True)
        name: str

    engine = database.init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        session.add(_SmokeTable(name="ok"))
        await session.commit()
        row = (await session.exec(select(_SmokeTable))).first()
        assert row is not None
        assert row.name == "ok"

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT name FROM task1_smoke"))
        assert result.fetchone()[0] == "ok"
