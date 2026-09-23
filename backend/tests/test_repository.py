"""Repository layer tests — async CRUD and typed error boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app import database
from backend.app.repository import (
    BaseRepository,
    ConflictError,
    FlagRepository,
    NotFoundError,
    StorageError,
)
from backend.app.models import Flag


pytestmark = pytest.mark.asyncio


async def _session() -> AsyncSession:
    engine = database.init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return AsyncSession(engine, expire_on_commit=False)


async def test_create_and_get_flag_by_name() -> None:
    session = await _session()
    try:
        repo = FlagRepository(session)
        created = await repo.create_flag(
            name="dark_mode",
            description="Dark theme",
            enabled=False,
        )
        assert created.name == "dark_mode"
        assert created.enabled is False

        loaded = await repo.get_flag_by_name("dark_mode")
        assert loaded.id == created.id
        assert loaded.description == "Dark theme"
    finally:
        await session.close()


async def test_duplicate_create_raises_conflict_error() -> None:
    session = await _session()
    try:
        repo = FlagRepository(session)
        await repo.create_flag(name="dark_mode")
        with pytest.raises(ConflictError, match="already exists"):
            await repo.create_flag(name="dark_mode")
    finally:
        await session.close()


async def test_missing_get_raises_not_found_error() -> None:
    session = await _session()
    try:
        repo = FlagRepository(session)
        with pytest.raises(NotFoundError, match="not found"):
            await repo.get_flag_by_name("missing_flag")
    finally:
        await session.close()


async def test_upsert_get_and_delete_override() -> None:
    session = await _session()
    try:
        repo = FlagRepository(session)
        await repo.create_flag(name="dark_mode", enabled=False)

        created = await repo.upsert_override(
            flag_name="dark_mode",
            user_id="user-1",
            enabled=True,
        )
        assert created.enabled is True

        loaded = await repo.get_override("dark_mode", "user-1")
        assert loaded is not None
        assert loaded.enabled is True

        updated = await repo.upsert_override(
            flag_name="dark_mode",
            user_id="user-1",
            enabled=False,
        )
        assert updated.enabled is False

        await repo.delete_override("dark_mode", "user-1")
        assert await repo.get_override("dark_mode", "user-1") is None

        with pytest.raises(NotFoundError, match="override"):
            await repo.delete_override("dark_mode", "user-1")
    finally:
        await session.close()


async def test_upsert_override_on_missing_flag_raises_not_found() -> None:
    session = await _session()
    try:
        repo = FlagRepository(session)
        with pytest.raises(NotFoundError, match="flag"):
            await repo.upsert_override(
                flag_name="nope",
                user_id="user-1",
                enabled=True,
            )
    finally:
        await session.close()


async def test_forced_db_failure_maps_to_storage_error() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    session = MagicMock()
    session.exec = AsyncMock(side_effect=SQLAlchemyError("db down"))

    repo = FlagRepository(session)
    with pytest.raises(StorageError, match="failed to read flag"):
        await repo.get_flag_by_name("dark_mode")


async def test_base_repository_create_and_get_by() -> None:
    """Standard CRUD stays on BaseRepository; FlagRepository composes it."""
    session = await _session()
    try:
        base = BaseRepository(session, Flag)
        created = await base.create(Flag(name="via_base", enabled=True))
        assert created.id is not None
        loaded = await base.get_by(name="via_base")
        assert loaded is not None
        assert loaded.enabled is True
    finally:
        await session.close()
