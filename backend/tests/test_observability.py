"""Minimal observability tests — trace id propagation only."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.observability import setup_observability


def _stub_app() -> FastAPI:
    app = FastAPI()
    setup_observability(app)

    @app.get("/ok")
    def ok() -> dict:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("explode")

    return app


def test_response_includes_x_trace_id() -> None:
    client = TestClient(_stub_app(), raise_server_exceptions=False)
    response = client.get("/ok")
    assert response.status_code == 200
    assert "X-Trace-Id" in response.headers
    assert response.headers["X-Trace-Id"]


def test_client_supplied_trace_id_is_echoed() -> None:
    client = TestClient(_stub_app(), raise_server_exceptions=False)
    response = client.get("/ok", headers={"X-Trace-Id": "dev-trace-123"})
    assert response.headers["X-Trace-Id"] == "dev-trace-123"


def test_unhandled_error_returns_500_with_trace_id() -> None:
    client = TestClient(_stub_app(), raise_server_exceptions=False)
    response = client.get("/boom", headers={"X-Trace-Id": "err-trace"})
    assert response.status_code == 500
    body = response.json()
    assert body["trace_id"] == "err-trace"
    assert body["detail"] == "Internal Server Error"
    assert response.headers["X-Trace-Id"] == "err-trace"
