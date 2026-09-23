"""
Feature-flag domain service.

Owns business rules that must not leak into HTTP handlers or the repository:
- State-pattern transitions for enable/disable
- Evaluate precedence (per-user targeting → global)
- In-process LRU+TTL evaluation cache
- Sliding-window rate limiting with exponential Retry-After
- Fail-open evaluate when durable storage fails but a warm cache entry exists

FastAPI is intentionally not imported here. Callers map domain exceptions to HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Literal, Optional, Tuple

from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app.models import (
    EvaluationResponse,
    Flag,
    FlagCreate,
    UserFlagOverride,
    _validate_user_id,
)
from backend.app.repository import (
    ConflictError,
    FlagRepository,
    NotFoundError,
    StorageError,
)

logger = logging.getLogger("app.service")

# Re-export repository errors so HTTP layer can import domain failures from one place.
__all__ = [
    "ConflictError",
    "NotFoundError",
    "StorageError",
    "RateLimitError",
    "FlagService",
    "flag_service",
    "EnabledState",
    "DisabledState",
    "state_from_bool",
    "bool_from_state",
]


#####
# Configuration defaults (single-node process; overridable via env at deploy)
#####


def _env_int(name: str, default: int) -> int:
    """Parse a positive int from the environment; fall back on invalid/missing."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


CACHE_MAX_SIZE = _env_int("CACHE_MAX_SIZE", 10_000)
CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 30)
RATE_LIMIT_WINDOW_SECONDS = _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
RATE_LIMIT_MAX_REQUESTS = _env_int("RATE_LIMIT_MAX_REQUESTS", 120)
RETRY_AFTER_BASE_SECONDS = _env_int("RETRY_AFTER_BASE_SECONDS", 1)
RETRY_AFTER_CAP_SECONDS = _env_int("RETRY_AFTER_CAP_SECONDS", 60)

CacheKey = Tuple[str, str]  # (flag_name, user_id)
EvalSource = Literal["override", "global"]


#####
# Domain errors (HTTP layer maps these to status codes)
#####


class RateLimitError(Exception):
    """Client exceeded the sliding-window budget for a hot path (typically evaluate).

    Attributes:
        retry_after: Seconds the client should wait (exponential backoff hint).
        message: Human-readable detail for ErrorBody.
    """

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


#####
# State pattern — release enablement as behavior, not scattered booleans
#####
#
# Persistence still stores `enabled: bool`. The service hydrates a FlagState
# before transitions and writes the bool back after enable()/disable().
# One scope (global flag OR one user targeting row) is never both Enabled and
# Disabled at the same time.
#####


class FlagState(ABC):
    """Abstract enablement state for a single release-control scope."""

    @abstractmethod
    def is_enabled(self) -> bool:
        """Return whether this scope currently allows the feature."""

    @abstractmethod
    def enable(self) -> "FlagState":
        """Transition toward Enabled (idempotent if already enabled)."""

    @abstractmethod
    def disable(self) -> "FlagState":
        """Transition toward Disabled (idempotent if already disabled)."""


class EnabledState(FlagState):
    """Feature is on for this scope."""

    def is_enabled(self) -> bool:
        return True

    def enable(self) -> FlagState:
        return self

    def disable(self) -> FlagState:
        return DisabledState()


class DisabledState(FlagState):
    """Feature is off for this scope."""

    def is_enabled(self) -> bool:
        return False

    def enable(self) -> FlagState:
        return EnabledState()

    def disable(self) -> FlagState:
        return self


def state_from_bool(enabled: bool) -> FlagState:
    """Hydrate the State object that matches a persisted boolean."""
    return EnabledState() if enabled else DisabledState()


def bool_from_state(state: FlagState) -> bool:
    """Persistable boolean for the current State object."""
    return state.is_enabled()


def transition_to(desired_enabled: bool, current: FlagState) -> FlagState:
    """Apply enable() or disable() so routes never embed transition rules."""
    return current.enable() if desired_enabled else current.disable()


#####
# Evaluation cache — OrderedDict LRU + per-entry TTL (O(1) ops)
#####


@dataclass
class CacheEntry:
    """One cached evaluate result with absolute expiry time."""

    enabled: bool
    source: EvalSource
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at

    def to_response(self, flag: str, user_id: str) -> EvaluationResponse:
        return EvaluationResponse(
            flag=flag,
            user_id=user_id,
            enabled=self.enabled,
            source=self.source,
        )


class EvaluationCache:
    """Process-local LRU cache for evaluate(flag, user) results.

    Ordering: ``OrderedDict`` provides hash-map lookup plus doubly-linked-list
    order so move-to-end and evict-oldest are O(1).

    Concurrency: callers must hold ``FlagService._lock`` around get/put/invalidate.
    """

    def __init__(self, *, max_size: int = CACHE_MAX_SIZE, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._entries: "OrderedDict[CacheKey, CacheEntry]" = OrderedDict()

    def clear(self) -> None:
        """Drop all entries (tests / process warm reset)."""
        self._entries.clear()

    def get(self, key: CacheKey) -> Optional[CacheEntry]:
        """Return a non-expired entry and mark it most-recently-used, else None."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: CacheKey, *, enabled: bool, source: EvalSource) -> CacheEntry:
        """Insert/refresh an entry; evict LRU if over capacity."""
        entry = CacheEntry(
            enabled=enabled,
            source=source,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = entry
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
        return entry

    def invalidate_user(self, flag_name: str, user_id: str) -> None:
        """Drop one evaluate key after targeting PUT/DELETE."""
        self._entries.pop((flag_name, user_id), None)

    def invalidate_flag(self, flag_name: str) -> None:
        """Drop every evaluate key for a flag after global PATCH."""
        stale = [key for key in self._entries if key[0] == flag_name]
        for key in stale:
            del self._entries[key]


#####
# Rate limiter — sliding window + exponential Retry-After
#####


class RateLimiter:
    """In-process sliding-window limiter with consecutive-429 backoff streaks.

    For each client key:
    - Timestamps of recent accepts live in a ``deque`` (window seconds).
    - When a request would exceed ``max_requests``, raise ``RateLimitError`` with
      ``retry_after = min(cap, base * 2^(streak-1))`` and increment streak.
    - A successful (allowed) request resets that client's streak to 0.
    """

    def __init__(
        self,
        *,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        retry_after_base: int = RETRY_AFTER_BASE_SECONDS,
        retry_after_cap: int = RETRY_AFTER_CAP_SECONDS,
    ) -> None:
        self._window_seconds = window_seconds
        self._max_requests = max_requests
        self._retry_after_base = retry_after_base
        self._retry_after_cap = retry_after_cap
        self._windows: Dict[str, Deque[float]] = {}
        self._streaks: Dict[str, int] = {}

    def clear(self) -> None:
        """Reset all buckets and streaks (tests)."""
        self._windows.clear()
        self._streaks.clear()

    def check(self, client_key: str) -> None:
        """Allow the request or raise ``RateLimitError`` with Retry-After seconds.

        Must be called under ``FlagService._lock`` (shared with cache) so window
        mutations stay consistent under concurrent coroutines.
        """
        now = time.monotonic()
        bucket = self._windows.setdefault(client_key, deque())

        # Drop timestamps outside the sliding window (bounded memory).
        cutoff = now - self._window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self._max_requests:
            streak = self._streaks.get(client_key, 0) + 1
            self._streaks[client_key] = streak
            retry_after = min(
                self._retry_after_cap,
                self._retry_after_base * (2 ** (streak - 1)),
            )
            raise RateLimitError(
                "rate limit exceeded",
                retry_after=int(retry_after),
            )

        bucket.append(now)
        self._streaks[client_key] = 0


#####
# FlagService — orchestration (repository + state + cache + rate limit)
#####


class FlagService:
    """Application service for flag management and cached evaluation.

    Threading model: async methods; one ``asyncio.Lock`` protects cache and
    rate-limiter maps. Repository I/O is awaited **outside** the lock whenever
    possible so we do not hold the lock across SQLite round-trips.
    """

    def __init__(
        self,
        *,
        cache_max_size: int = CACHE_MAX_SIZE,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
        rate_limit_window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
        rate_limit_max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        retry_after_base: int = RETRY_AFTER_BASE_SECONDS,
        retry_after_cap: int = RETRY_AFTER_CAP_SECONDS,
    ) -> None:
        self._lock = asyncio.Lock()
        self._cache = EvaluationCache(
            max_size=cache_max_size,
            ttl_seconds=cache_ttl_seconds,
        )
        self._rate_limiter = RateLimiter(
            window_seconds=rate_limit_window_seconds,
            max_requests=rate_limit_max_requests,
            retry_after_base=retry_after_base,
            retry_after_cap=retry_after_cap,
        )

    def reset_runtime_state(self) -> None:
        """Clear cache and rate-limit state between tests."""
        self._cache.clear()
        self._rate_limiter.clear()

    def _repo(self, session: AsyncSession) -> FlagRepository:
        return FlagRepository(session)

    #####
    # Mutations
    #####

    async def create_flag(self, session: AsyncSession, body: FlagCreate) -> Flag:
        """Create a flag; initial enabled value goes through the State pattern."""
        initial = transition_to(body.enabled, DisabledState())
        return await self._repo(session).create_flag(
            name=body.name,
            description=body.description,
            enabled=bool_from_state(initial),
        )

    async def get_flag(self, session: AsyncSession, name: str) -> Flag:
        """Fetch a flag definition or raise ``NotFoundError``."""
        return await self._repo(session).get_flag_by_name(name)

    async def set_global_state(
        self,
        session: AsyncSession,
        name: str,
        enabled: bool,
    ) -> Flag:
        """PATCH global enablement via State transitions, then invalidate cache.

        Persist succeeds first; cache keys for this flag are dropped only after
        a successful write so we never advertise a commit that did not land.
        """
        repo = self._repo(session)
        flag = await repo.get_flag_by_name(name)
        new_state = transition_to(enabled, state_from_bool(flag.enabled))
        updated = await repo.update_flag_enabled(name, bool_from_state(new_state))
        async with self._lock:
            self._cache.invalidate_flag(name)
        return updated

    async def set_user_targeting(
        self,
        session: AsyncSession,
        name: str,
        user_id: str,
        enabled: bool,
    ) -> UserFlagOverride:
        """PUT per-user targeting via State, then invalidate that evaluate key."""
        user_id = _validate_user_id(user_id)
        target_state = transition_to(enabled, DisabledState())
        override = await self._repo(session).upsert_override(
            flag_name=name,
            user_id=user_id,
            enabled=bool_from_state(target_state),
        )
        async with self._lock:
            self._cache.invalidate_user(name, user_id)
        return override

    async def clear_user_targeting(
        self,
        session: AsyncSession,
        name: str,
        user_id: str,
    ) -> None:
        """DELETE per-user targeting; evaluate will fall back to global state."""
        user_id = _validate_user_id(user_id)
        await self._repo(session).delete_override(name, user_id)
        async with self._lock:
            self._cache.invalidate_user(name, user_id)

    #####
    # Evaluate (cache → repo → precedence → cache fill; fail-open on StorageError)
    #####

    async def evaluate(
        self,
        session: AsyncSession,
        name: str,
        user_id: str,
        *,
        client_key: str = "global",
    ) -> EvaluationResponse:
        """Decide if ``name`` is enabled for ``user_id``.

        Precedence:
            1. UserFlagOverride for (name, user_id) → source=\"override\"
            2. Else Flag.global enabled → source=\"global\"
            3. Else NotFoundError

        Effective boolean is always read through ``state_from_bool(...).is_enabled()``
        so evaluation does not bypass the State model.

        Caching:
            Hot path serves LRU+TTL entries under ``asyncio.Lock``.
            Successful loads populate the cache. Mutations invalidate keys.

        Fail-open:
            On ``StorageError`` after a cache miss, re-check the cache; if a
            still-valid entry exists (e.g. filled concurrently), return it
            instead of failing the request.
        """
        user_id = _validate_user_id(user_id)
        key: CacheKey = (name, user_id)

        async with self._lock:
            self._rate_limiter.check(client_key)
            cached = self._cache.get(key)

        if cached is not None:
            return cached.to_response(name, user_id)

        try:
            response = await self._load_evaluation(session, name, user_id)
        except StorageError:
            # Fail-open: durable store failed — serve warm cache if present.
            async with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                logger.warning(
                    "evaluate fail-open flag=%s user=%s (serving cache)",
                    name,
                    user_id,
                )
                return cached.to_response(name, user_id)
            raise

        async with self._lock:
            self._cache.put(key, enabled=response.enabled, source=response.source)
        return response

    async def _load_evaluation(
        self,
        session: AsyncSession,
        name: str,
        user_id: str,
    ) -> EvaluationResponse:
        """Load flag + optional override from the repository and apply precedence."""
        repo = self._repo(session)
        flag = await repo.get_flag_by_name(name)
        override = await repo.get_override(name, user_id)

        if override is not None:
            enabled = state_from_bool(override.enabled).is_enabled()
            source: EvalSource = "override"
        else:
            enabled = state_from_bool(flag.enabled).is_enabled()
            source = "global"

        return EvaluationResponse(
            flag=name,
            user_id=user_id,
            enabled=enabled,
            source=source,
        )


#####
# Process singleton (tests may construct FlagService with tighter limits)
#####

flag_service = FlagService()
