# Feature Flag Service — Implementation Tasks

Checklist derived from [requirements.md](requirements.md) and [design.md](design.md).  
**Rule:** each task touches **one primary app file** (+ its paired test file). Do not start a task until prerequisites are checked done.  
**Testing:** behavior only — no property-based tests, no latency benchmarks, no deploy smoke.

Legend: `[ ]` not done · `[x]` done

---

## Task 0 — Test harness baseline

**Primary files:** `backend/tests/conftest.py`  
**Prerequisites:** none  
**Requirements satisfied:** eng bar for isolated pytest runs (req §3.5); design §7.1 fixtures foundation

### Sub-tasks
- [x] Ensure `backend/tests/conftest.py` can host shared fixtures (placeholder OK until Task 1 wires DB)
- [x] Confirm `pytest` discovers `backend/tests/`

### Tests (this task)
- [x] Minimal smoke: `pytest` collects at least one placeholder or skips cleanly (replace in Task 1)

---

## Task 1 — Database configuration

**Primary files:** `backend/app/database.py`  
**Paired tests:** `backend/tests/test_database.py`  
**Prerequisites:** Task 0  
**Requirements satisfied:** short-lived **async** sessions (req §3.4); SQLite + `aiosqlite` (req §2.6); many-user concurrency must (req §3.1); design §5.1  

**Revision:** Task 1 retargeted from sync → async after concurrency mandate approval.

### Sub-tasks
- [x] Create **async** SQLite engine (`sqlite+aiosqlite`, `create_async_engine`)
- [x] Implement `async def get_session()` generator (yield `AsyncSession` + close)
- [x] Expose `init_engine()` for tests (in-memory / temp async SQLite)

### Tests (`test_database.py`)
- [x] Async session yields usable session and closes after use
- [x] Engine can create metadata when models exist (light assert; full schema in Task 2)

---

## Task 2 — Persistence models (SQLModel)

**Primary files:** `backend/app/models.py` *(tables only in this task)*  
**Paired tests:** `backend/tests/test_models.py`  
**Prerequisites:** Task 1  
**Requirements satisfied:** Flag + UserFlagOverride durable shape (req §2.1); unique name / unique `(flag_name, user_id)` (design §4.1)

### Sub-tasks
- [x] `Flag` table: `name` (unique), `description`, `enabled`, timestamps
- [x] `UserFlagOverride` table: `flag_name`, `user_id`, `enabled`, `updated_at`, unique pair
- [x] Do **not** add API Pydantic schemas yet (Task 3)

### Tests (`test_models.py`)
- [x] Create tables via `SQLModel.metadata.create_all`
- [x] Insert Flag; duplicate `name` raises integrity error
- [x] Insert override; duplicate `(flag_name, user_id)` raises integrity error

---

## Task 3 — API DTOs (Pydantic) in models

**Primary files:** `backend/app/models.py` *(Pydantic schemas only)*  
**Paired tests:** `backend/tests/test_models.py` (extend)  
**Prerequisites:** Task 2  
**Requirements satisfied:** validation rules (req §2.4); design §4.2 DTOs

### Sub-tasks
- [x] Add `FlagCreate`, `FlagUpdate`, `FlagRead`
- [x] Add `UserOverrideUpsert`, `UserOverrideRead`
- [x] Add `EvaluationResponse` (`source`: `override` | `global`)
- [x] Add `ErrorBody` (`detail`, `trace_id`)
- [x] Enforce name slug, description max 512, `user_id` 1..128

### Tests (`test_models.py`)
- [x] Valid `FlagCreate` accepted
- [x] Invalid flag `name` rejected
- [x] Empty / whitespace `user_id` rejected where validated
- [x] `EvaluationResponse` accepts only `override` | `global`

---

## Task 4 — Repository (storage boundary)

**Primary files:** `backend/app/repository.py`  
**Paired tests:** `backend/tests/test_repository.py`  
**Prerequisites:** Task 1–3  
**Requirements satisfied:** fail-open isolation / typed errors (req §4.4); no service→DB bypass (structure); design §5.2

### Sub-tasks
- [x] Define typed errors: `StorageError`, `NotFoundError`, `ConflictError`
- [x] **Async** `create_flag`, `get_flag_by_name`, `update_flag_enabled`
- [x] **Async** `upsert_override`, `get_override`, `delete_override`
- [x] Wrap DB failures in `try/except` → `StorageError` (no process crash / no event-loop kill)
- [x] Repository must **not** import service

### Tests (`test_repository.py`)
- [x] Create + get flag by name
- [x] Duplicate create → `ConflictError`
- [x] Missing get → `NotFoundError` (or `None` + documented; prefer explicit NotFound for get-or-raise helpers)
- [x] Upsert override; get; delete; delete missing → `NotFoundError`
- [x] Forced DB failure path maps to `StorageError` (mock/break session)

---

## Task 5 — Domain service (State, cache, rate limit + backoff)

**Primary files:** `backend/app/service.py`  
**Paired tests:** `backend/tests/test_service.py`  
**Prerequisites:** Task 4  
**Requirements satisfied:** State pattern (req §2.3.1); evaluate precedence (req §2.2); LRU+TTL cache O(1) (req §3.2–3.3); global lock (req §3.4); invalidation (req §4.2); rate limit + exponential `Retry-After` (req §4.3); fail-open evaluate (req §4.4); design §2–3, §5.3

### Sub-tasks
- [x] `EnabledState` / `DisabledState` + `state_from_bool` / transitions
- [x] Module-level service with **`asyncio.Lock`**, `OrderedDict` LRU, TTL, max size
- [x] **Async** `create_flag`, `get_flag`, `set_global_state` (via State), `set_user_targeting`, `clear_user_targeting`, `evaluate`
- [x] Cache get/put/invalidate under `async with lock`; invalidate on successful writes only
- [x] Sliding-window rate limit; on exceed compute exponential backoff:
  - track consecutive 429 streak per client key
  - `Retry-After = min(cap, base * 2^(streak-1))`
  - reset streak after a successful (non-429) evaluate
- [x] Evaluate: cache → await repo → precedence → cache fill; fail-open on cache hit if repo errors
- [x] Map/raise domain errors for later HTTP translation (keep FastAPI out of service if possible)

### Tests (`test_service.py`) — only what is needed
- [x] State: enable/disable transitions + idempotent enable/disable
- [x] Precedence: override wins over global; clear override falls back to global
- [x] Unknown flag evaluate → not-found domain error
- [x] Cache invalidation: after global/targeting change, evaluate result updates
- [x] Rate limit: exceeding window raises rate-limit error with increasing `Retry-After` on consecutive hits
- [x] Fail-open: seeded cache + repo `StorageError` on evaluate still returns cached result
- [x] Write + repo `StorageError` → error surfaced; cache not treated as committed success

---

## Task 6 — Observability

**Primary files:** `backend/app/observability.py`  
**Paired tests:** `backend/tests/test_observability.py`  
**Prerequisites:** none blocking (wire in Task 7); prefer after Task 5  
**Requirements satisfied:** `X-Trace-Id` + 500 mapping (req §2.4, §3.5); design §5.4

### Sub-tasks
- [x] Middleware: accept/propagate/generate `X-Trace-Id` on response
- [x] Unhandled exception handler → `500` + `ErrorBody` with `trace_id`

### Tests (`test_observability.py`)
- [x] Response includes `X-Trace-Id` (via minimal app mount or shared test app stub)
- [x] Unhandled error returns `500` and includes `trace_id` in body

---

## Task 7 — HTTP API

**Primary files:** `backend/app/main.py`  
**Paired tests:** `backend/tests/test_main.py`  
**Prerequisites:** Tasks 1–6  
**Requirements satisfied:** all API contracts + status codes (req §2.3, §2.5); validation 400; wiring design §4.3–4.4, §5.5

### Sub-tasks
- [x] Lifespan: async `create_all` on startup
- [x] Mount observability middleware/handlers
- [x] **Async** routes only: validate → await service → map domain errors → HTTP
  - `POST /flags` → 201 / 409 / 400
  - `GET /flags/{name}` → 200 / 404
  - `PATCH /flags/{name}` → 200 / 400 / 404
  - `PUT /flags/{name}/users/{user_id}` → 200 / 400 / 404
  - `DELETE /flags/{name}/users/{user_id}` → 204 / 404
  - `GET /flags/{name}/evaluate?user_id=` → 200 / 400 / 404 / 429 / 503
- [x] Do not put business rules or SQL in handlers; do not block the event loop on sync DB I/O

### Tests (`test_main.py`) — API behavior only
- [x] Create flag `201`; duplicate `409`; bad name `400`
- [x] Get / patch happy paths; unknown `404`
- [x] Targeting PUT/DELETE; evaluate precedence end-to-end (`source` field)
- [x] Evaluate missing `user_id` / invalid → `400`
- [x] Burst evaluate → `429` + `Retry-After` header
- [x] `X-Trace-Id` present on success response
- [x] (Optional single case) force storage failure on write → `503` if easy via dependency override; skip if already covered in `test_service.py`

---

## Task 8 — Engineering wrap (docs/CI only)

**Primary files:** `README.md`, `.github/workflows/ci.yml` (existing OK to update)  
**Prerequisites:** Task 7 green  
**Requirements satisfied:** documentation + CI (req §3.5, §5 item 7); design §8  
**Note:** no new product features

### Sub-tasks
- [x] README: setup, run uvicorn, run pytest
- [x] CI runs pytest on `backend/`
- [x] Point to design ASCII as architecture anchor (link to `design.md` §1.2)

### Tests
- [x] CI job passes (no extra test types)

---

## Execution order (topology)

```
Task 0 → Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8
         (database) (SQLModel) (DTO)  (repo)  (service) (obs)   (API)  (docs/CI)
```

Task 6 may start after Task 3 in parallel with 4–5 if needed, but must be done before Task 7.

---

## Explicitly out of scope for all tasks

- Property-based tests
- p99 latency / load tests
- Auth, Redis, multi-node
- DigitalOcean live deploy verification
- Refactors across multiple app files in one task

---

## Orchestration Note

Implement **one task at a time** after developer approval. Check boxes as work completes. Do not skip ahead without green tests for the current task.
