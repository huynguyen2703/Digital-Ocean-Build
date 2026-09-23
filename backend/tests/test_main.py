"""HTTP API integration tests — routes, status codes, evaluate behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_flag_201_duplicate_409_bad_name_400(client: TestClient) -> None:
    created = client.post(
        "/flags",
        json={"name": "dark_mode", "description": "Dark theme", "enabled": False},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "dark_mode"
    assert body["enabled"] is False

    dup = client.post("/flags", json={"name": "dark_mode", "enabled": True})
    assert dup.status_code == 409

    bad = client.post("/flags", json={"name": "Bad-Name", "enabled": False})
    assert bad.status_code == 400


def test_get_and_patch_flag(client: TestClient) -> None:
    client.post("/flags", json={"name": "dark_mode", "enabled": False})

    got = client.get("/flags/dark_mode")
    assert got.status_code == 200
    assert got.json()["enabled"] is False

    patched = client.patch("/flags/dark_mode", json={"enabled": True})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is True

    missing = client.get("/flags/nope")
    assert missing.status_code == 404

    missing_patch = client.patch("/flags/nope", json={"enabled": True})
    assert missing_patch.status_code == 404


def test_targeting_and_evaluate_precedence(client: TestClient) -> None:
    client.post("/flags", json={"name": "dark_mode", "enabled": False})

    global_eval = client.get("/flags/dark_mode/evaluate", params={"user_id": "user-1"})
    assert global_eval.status_code == 200
    assert global_eval.json() == {
        "flag": "dark_mode",
        "user_id": "user-1",
        "enabled": False,
        "source": "global",
    }

    put = client.put(
        "/flags/dark_mode/users/user-1",
        json={"enabled": True},
    )
    assert put.status_code == 200
    assert put.json()["enabled"] is True

    override_eval = client.get(
        "/flags/dark_mode/evaluate", params={"user_id": "user-1"}
    )
    assert override_eval.status_code == 200
    assert override_eval.json()["enabled"] is True
    assert override_eval.json()["source"] == "override"

    deleted = client.delete("/flags/dark_mode/users/user-1")
    assert deleted.status_code == 204

    fallback = client.get("/flags/dark_mode/evaluate", params={"user_id": "user-1"})
    assert fallback.json()["source"] == "global"
    assert fallback.json()["enabled"] is False


def test_evaluate_missing_or_invalid_user_id_400(client: TestClient) -> None:
    client.post("/flags", json={"name": "dark_mode", "enabled": True})

    missing = client.get("/flags/dark_mode/evaluate")
    assert missing.status_code == 400

    blank = client.get("/flags/dark_mode/evaluate", params={"user_id": "   "})
    assert blank.status_code == 400


def test_burst_evaluate_returns_429_with_retry_after(
    rate_limited_client: TestClient,
) -> None:
    client = rate_limited_client
    client.post("/flags", json={"name": "dark_mode", "enabled": True})
    client.get("/flags/dark_mode/evaluate", params={"user_id": "u"})
    client.get("/flags/dark_mode/evaluate", params={"user_id": "u"})

    limited = client.get("/flags/dark_mode/evaluate", params={"user_id": "u"})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    assert int(limited.headers["Retry-After"]) >= 1


def test_x_trace_id_present_on_success(client: TestClient) -> None:
    response = client.post(
        "/flags",
        json={"name": "dark_mode", "enabled": False},
        headers={"X-Trace-Id": "api-trace-1"},
    )
    assert response.status_code == 201
    assert response.headers["X-Trace-Id"] == "api-trace-1"


def test_health_check(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
