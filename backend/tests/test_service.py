"""Domain service tests — state, precedence, cache, rate limit, fail-open."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app import database
from backend.app.models import FlagCreate
from backend.app.repository import StorageError
from backend.app.service import (
    DisabledState,
    EnabledState,
    FlagService,
    RateLimitError,
    bool_from_state,
    state_from_bool,
    transition_to,
)


async def _session() -> AsyncSession:
    engine = database.init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return AsyncSession(engine, expire_on_commit=False)


def _service(**kwargs: object) -> FlagService:
    svc = FlagService(**kwargs)  # type: ignore[arg-type]
    svc.reset_runtime_state()
    return svc


#####
# State pattern
#####


def test_state_enable_disable_transitions_and_idempotency() -> None:
    disabled = DisabledState()
    enabled = disabled.enable()
    assert isinstance(enabled, EnabledState)
    assert enabled.is_enabled() is True
    assert enabled.enable() is enabled

    back = enabled.disable()
    assert isinstance(back, DisabledState)
    assert back.is_enabled() is False
    assert back.disable() is back

    assert bool_from_state(state_from_bool(True)) is True
    assert bool_from_state(transition_to(False, EnabledState())) is False


#####
# Precedence + unknown flag
#####


@pytest.mark.asyncio
async def test_evaluate_precedence_override_wins_then_clear_falls_back() -> None:
    session = await _session()
    svc = _service()
    try:
        await svc.create_flag(
            session,
            FlagCreate(name="dark_mode", enabled=False),
        )
        first = await svc.evaluate(session, "dark_mode", "user-1", client_key="t1")
        assert first.enabled is False
        assert first.source == "global"

        await svc.set_user_targeting(session, "dark_mode", "user-1", True)
        second = await svc.evaluate(session, "dark_mode", "user-1", client_key="t2")
        assert second.enabled is True
        assert second.source == "override"

        await svc.clear_user_targeting(session, "dark_mode", "user-1")
        third = await svc.evaluate(session, "dark_mode", "user-1", client_key="t3")
        assert third.enabled is False
        assert third.source == "global"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_evaluate_unknown_flag_raises_not_found() -> None:
    from backend.app.repository import NotFoundError

    session = await _session()
    svc = _service()
    try:
        with pytest.raises(NotFoundError):
            await svc.evaluate(session, "missing", "user-1", client_key="u")
    finally:
        await session.close()


#####
# Cache invalidation
#####


@pytest.mark.asyncio
async def test_cache_invalidation_after_global_and_targeting_change() -> None:
    session = await _session()
    svc = _service()
    try:
        await svc.create_flag(session, FlagCreate(name="dark_mode", enabled=False))
        await svc.evaluate(session, "dark_mode", "user-1", client_key="c1")

        await svc.set_global_state(session, "dark_mode", True)
        after_global = await svc.evaluate(
            session, "dark_mode", "user-1", client_key="c2"
        )
        assert after_global.enabled is True
        assert after_global.source == "global"

        await svc.set_user_targeting(session, "dark_mode", "user-1", False)
        after_target = await svc.evaluate(
            session, "dark_mode", "user-1", client_key="c3"
        )
        assert after_target.enabled is False
        assert after_target.source == "override"
    finally:
        await session.close()


#####
# Rate limit + exponential Retry-After
#####


@pytest.mark.asyncio
async def test_rate_limit_increases_retry_after_on_consecutive_hits() -> None:
    session = await _session()
    svc = _service(
        rate_limit_max_requests=2,
        rate_limit_window_seconds=60,
        retry_after_base=1,
        retry_after_cap=60,
    )
    try:
        await svc.create_flag(session, FlagCreate(name="dark_mode", enabled=True))
        await svc.evaluate(session, "dark_mode", "u", client_key="burst")
        await svc.evaluate(session, "dark_mode", "u", client_key="burst")

        with pytest.raises(RateLimitError) as first:
            await svc.evaluate(session, "dark_mode", "u", client_key="burst")
        assert first.value.retry_after == 1

        with pytest.raises(RateLimitError) as second:
            await svc.evaluate(session, "dark_mode", "u", client_key="burst")
        assert second.value.retry_after == 2

        with pytest.raises(RateLimitError) as third:
            await svc.evaluate(session, "dark_mode", "u", client_key="burst")
        assert third.value.retry_after == 4
    finally:
        await session.close()


#####
# Fail-open + write storage failure
#####


@pytest.mark.asyncio
async def test_evaluate_fail_open_serves_cache_when_repo_errors() -> None:
    session = await _session()
    svc = _service()
    try:
        key = ("dark_mode", "user-1")
        async with svc._lock:
            svc._cache.put(key, enabled=True, source="global")

        calls = {"n": 0}
        real_get = svc._cache.get

        def flaky_get(k):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_get(k)

        svc._cache.get = flaky_get  # type: ignore[method-assign]

        with patch.object(
            FlagService,
            "_load_evaluation",
            new=AsyncMock(side_effect=StorageError("db down")),
        ):
            result = await svc.evaluate(
                session, "dark_mode", "user-1", client_key="fo"
            )

        assert result.enabled is True
        assert result.source == "global"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_write_storage_error_does_not_invalidate_as_success() -> None:
    session = await _session()
    svc = _service()
    try:
        await svc.create_flag(session, FlagCreate(name="dark_mode", enabled=False))
        await svc.evaluate(session, "dark_mode", "user-1", client_key="w1")

        async with svc._lock:
            assert svc._cache.get(("dark_mode", "user-1")) is not None

        with patch(
            "backend.app.service.FlagRepository.update_flag_enabled",
            new=AsyncMock(side_effect=StorageError("db down")),
        ):
            with pytest.raises(StorageError):
                await svc.set_global_state(session, "dark_mode", True)

        # Cache still holds pre-failure evaluation (invalidation only on success).
        async with svc._lock:
            cached = svc._cache.get(("dark_mode", "user-1"))
        assert cached is not None
        assert cached.enabled is False
    finally:
        await session.close()
