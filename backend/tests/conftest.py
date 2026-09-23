"""Shared pytest fixtures — isolated in-memory DB + fresh service runtime."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import database
from backend.app.main import app
from backend.app.service import RateLimiter, flag_service


@pytest.fixture
def client() -> TestClient:
    database.init_engine("sqlite+aiosqlite:///:memory:")
    flag_service.reset_runtime_state()

    with TestClient(app) as test_client:
        yield test_client

    flag_service.reset_runtime_state()
    app.dependency_overrides.clear()


@pytest.fixture
def rate_limited_client(client: TestClient) -> TestClient:
    """Tighten evaluate rate limits for 429 tests."""
    flag_service._rate_limiter = RateLimiter(
        window_seconds=60,
        max_requests=2,
        retry_after_base=1,
        retry_after_cap=60,
    )
    flag_service.reset_runtime_state()
    return client
