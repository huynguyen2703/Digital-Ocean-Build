"""Persistence model and API DTO tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app import database
from backend.app.models import (
    EvaluationResponse,
    Flag,
    FlagCreate,
    UserFlagOverride,
    UserOverrideRead,
)


async def _prepare_db() -> None:
    engine = database.init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


@pytest.mark.asyncio
async def test_create_tables_via_metadata() -> None:
    await _prepare_db()
    engine = database.get_engine()
    async with engine.connect() as conn:
        tables = await conn.run_sync(
            lambda sync_conn: set(sync_conn.dialect.get_table_names(sync_conn))
        )
    assert "flags" in tables
    assert "user_flag_overrides" in tables


@pytest.mark.asyncio
async def test_insert_flag_duplicate_name_raises_integrity_error() -> None:
    await _prepare_db()
    engine = database.get_engine()

    async with AsyncSession(engine) as session:
        session.add(Flag(name="dark_mode", description="Dark theme", enabled=False))
        await session.commit()

    async with AsyncSession(engine) as session:
        session.add(Flag(name="dark_mode", description="dup", enabled=True))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_insert_override_duplicate_pair_raises_integrity_error() -> None:
    await _prepare_db()
    engine = database.get_engine()

    async with AsyncSession(engine) as session:
        session.add(Flag(name="dark_mode", enabled=False))
        await session.commit()

    async with AsyncSession(engine) as session:
        session.add(
            UserFlagOverride(flag_name="dark_mode", user_id="user-1", enabled=True)
        )
        await session.commit()

    async with AsyncSession(engine) as session:
        session.add(
            UserFlagOverride(flag_name="dark_mode", user_id="user-1", enabled=False)
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with AsyncSession(engine) as session:
        rows = (await session.exec(select(UserFlagOverride))).all()
        assert len(rows) == 1
        assert rows[0].enabled is True


def test_valid_flag_create_accepted() -> None:
    body = FlagCreate(name="dark_mode", description="Dark theme", enabled=True)
    assert body.name == "dark_mode"
    assert body.enabled is True


def test_invalid_flag_name_rejected() -> None:
    with pytest.raises(ValidationError):
        FlagCreate(name="Dark-Mode")
    with pytest.raises(ValidationError):
        FlagCreate(name="a")


def test_empty_or_whitespace_user_id_rejected() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        UserOverrideRead(
            flag_name="dark_mode",
            user_id="   ",
            enabled=True,
            updated_at=now,
        )
    with pytest.raises(ValidationError):
        EvaluationResponse(
            flag="dark_mode",
            user_id="",
            enabled=False,
            source="global",
        )


def test_evaluation_response_source_literal() -> None:
    ok = EvaluationResponse(
        flag="dark_mode",
        user_id="user-1",
        enabled=True,
        source="override",
    )
    assert ok.source == "override"
    with pytest.raises(ValidationError):
        EvaluationResponse(
            flag="dark_mode",
            user_id="user-1",
            enabled=True,
            source="unknown",  # type: ignore[arg-type]
        )
