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
- [ ] Ensure `backend/tests/conftest.py` can host shared fixtures (placeholder OK until Task 1 wires DB)
- [ ] Confirm `pytest` discovers `backend/tests/`

### Tests (this task)
- [ ] Minimal smoke: `pytest` collects at least one placeholder or skips cleanly (replace in Task 1)

---

## Task 1 — Database configuration

**Primary files:** `backend/app/database.py`  
**Paired tests:** `backend/tests/test_database.py`  
**Prerequisites:** Task 0  
**Requirements satisfied:** short-lived sessions (req §3.4); SQLite engine (req §2.6); design §5.1

### Sub-tasks
- [ ] Create SQLite engine (`check_same_thread=False`)
- [ ] Implement `get_session()` generator (yield + close in `finally`)
- [ ] Expose a way for tests to point at in-memory / temp SQLite

### Tests (`test_database.py`)
- [ ] Session yields a usable `Session` and closes after use
- [ ] Engine can create metadata when models exist (light assert; full schema in Task 2)

---

## Task 2 — Persistence models (SQLModel)

**Primary files:** `backend/app/models.py` *(tables only in this task)*  
**Paired tests:** `backend/tests/test_models.py`  
**Prerequisites:** Task 1  
**Requirements satisfied:** Flag + UserFlagOverride durable shape (req §2.1); unique name / unique `(flag_name, user_id)` (design §4.1)

### Sub-tasks
- [ ] `Flag` table: `name` (unique), `description`, `enabled`, timestamps
- [ ] `UserFlagOverride` table: `flag_name`, `user_id`, `enabled`, `updated_at`, unique pair
- [ ] Do **not** add API Pydantic schemas yet (Task 3)

### Tests (`test_models.py`)
- [ ] Create tables via `SQLModel.metadata.create_all`
- [ ] Insert Flag; duplicate `name` raises integrity error
- [ ] Insert override; duplicate `(flag_name, user_id)` raises integrity error

---

## Task 3 — API DTOs (Pydantic) in models

**Primary files:** `backend/app/models.py` *(Pydantic schemas only)*  
**Paired tests:** `backend/tests/test_models.py` (extend)  
**Prerequisites:** Task 2  
**Requirements satisfied:** validation rules (req §2.4); design §4.2 DTOs

### Sub-tasks
- [ ] Add `FlagCreate`, `FlagUpdate`, `FlagRead`
- [ ] Add `UserOverrideUpsert`, `UserOverrideRead`
- [ ] Add `EvaluationResponse` (`source`: `override` | `global`)
- [ ] Add `ErrorBody` (`detail`, `trace_id`)
- [ ] Enforce name slug, description max 512, `user_id` 1..128

### Tests (`test_models.py`)
- [ ] Valid `FlagCreate` accepted
- [ ] Invalid flag `name` rejected
- [ ] Empty / whitespace `user_id` rejected where validated
- [ ] `EvaluationResponse` accepts only `override` | `global`

---

## Task 4 — Repository (storage boundary)

**Primary files:** `backend/app/repository.py`  
**Paired tests:** `backend/tests/test_repository.py`  
**Prerequisites:** Task 1–3  
**Requirements satisfied:** fail-open isolation / typed errors (req §4.4); no service→DB bypass (structure); design §5.2

### Sub-tasks
- [ ] Define typed errors: `StorageError`, `NotFoundError`, `ConflictError`
- [ ] `create_flag`, `get_flag_by_name`, `update_flag_enabled`
- [ ] `upsert_override`, `get_override`, `delete_override`
- [ ] Wrap DB failures in `try/except` → `StorageError` (no process crash)
- [ ] Repository must **not** import service

### Tests (`test_repository.py`)
- [ ] Create + get flag by name
- [ ] Duplicate create → `ConflictError`
- [ ] Missing get → `NotFoundError` (or `None` + documented; prefer explicit NotFound for get-or-raise helpers)
- [ ] Upsert override; get; delete; delete missing → `NotFoundError`
- [ ] Forced DB failure path maps to `StorageError` (mock/break session)

---

## Task 5 — Domain service (State, cache, rate limit + backoff)

**Primary files:** `backend/app/service.py`  
**Paired tests:** `backend/tests/test_service.py`  
**Prerequisites:** Task 4  
**Requirements satisfied:** State pattern (req §2.3.1); evaluate precedence (req §2.2); LRU+TTL cache O(1) (req §3.2–3.3); global lock (req §3.4); invalidation (req §4.2); rate limit + exponential `Retry-After` (req §4.3); fail-open evaluate (req §4.4); design §2–3, §5.3

### Sub-tasks
- [ ] `EnabledState` / `DisabledState` + `state_from_bool` / transitions
- [ ] Module-level service with `threading.Lock`, `OrderedDict` LRU, TTL, max size
- [ ] `create_flag`, `get_flag`, `set_global_state` (via State), `set_user_targeting`, `clear_user_targeting`, `evaluate`
- [ ] Cache get/put/invalidate under lock; invalidate on successful writes only
- [ ] Sliding-window rate limit; on exceed compute exponential backoff:
  - track consecutive 429 streak per client key
  - `Retry-After = min(cap, base * 2^(streak-1))`
  - reset streak after a successful (non-429) evaluate
- [ ] Evaluate: cache → repo → precedence → cache fill; fail-open on cache hit if repo errors
- [ ] Map/raise domain errors for later HTTP translation (keep FastAPI out of service if possible)

### Tests (`test_service.py`) — only what is needed
- [ ] State: enable/disable transitions + idempotent enable/disable
- [ ] Precedence: override wins over global; clear override falls back to global
- [ ] Unknown flag evaluate → not-found domain error
- [ ] Cache invalidation: after global/targeting change, evaluate result updates
- [ ] Rate limit: exceeding window raises rate-limit error with increasing `Retry-After` on consecutive hits
- [ ] Fail-open: seeded cache + repo `StorageError` on evaluate still returns cached result
- [ ] Write + repo `StorageError` → error surfaced; cache not treated as committed success

---

## Task 6 — Observability

**Primary files:** `backend/app/observability.py`  
**Paired tests:** `backend/tests/test_observability.py`  
**Prerequisites:** none blocking (wire in Task 7); prefer after Task 5  
**Requirements satisfied:** `X-Trace-Id` + 500 mapping (req §2.4, §3.5); design §5.4

### Sub-tasks
- [ ] Middleware: accept/propagate/generate `X-Trace-Id` on response
- [ ] Unhandled exception handler → `500` + `ErrorBody` with `trace_id`

### Tests (`test_observability.py`)
- [ ] Response includes `X-Trace-Id` (via minimal app mount or shared test app stub)
- [ ] Unhandled error returns `500` and includes `trace_id` in body

---

## Task 7 — HTTP API

**Primary files:** `backend/app/main.py`  
**Paired tests:** `backend/tests/test_main.py`  
**Prerequisites:** Tasks 1–6  
**Requirements satisfied:** all API contracts + status codes (req §2.3, §2.5); validation 400; wiring design §4.3–4.4, §5.5

### Sub-tasks
- [ ] Lifespan: `create_all` on startup
- [ ] Mount observability middleware/handlers
- [ ] Routes only: validate → service → map domain errors → HTTP
  - `POST /flags` → 201 / 409 / 400
  - `GET /flags/{name}` → 200 / 404
  - `PATCH /flags/{name}` → 200 / 400 / 404
  - `PUT /flags/{name}/users/{user_id}` → 200 / 400 / 404
  - `DELETE /flags/{name}/users/{user_id}` → 204 / 404
  - `GET /flags/{name}/evaluate?user_id=` → 200 / 400 / 404 / 429 / 503
- [ ] Do not put business rules or SQL in handlers

### Tests (`test_main.py`) — API behavior only
- [ ] Create flag `201`; duplicate `409`; bad name `400`
- [ ] Get / patch happy paths; unknown `404`
- [ ] Targeting PUT/DELETE; evaluate precedence end-to-end (`source` field)
- [ ] Evaluate missing `user_id` / invalid → `400`
- [ ] Burst evaluate → `429` + `Retry-After` header
- [ ] `X-Trace-Id` present on success response
- [ ] (Optional single case) force storage failure on write → `503` if easy via dependency override; skip if already covered in `test_service.py`

---

## Task 8 — Engineering wrap (docs/CI only)

**Primary files:** `README.md`, `.github/workflows/ci.yml` (existing OK to update)  
**Prerequisites:** Task 7 green  
**Requirements satisfied:** documentation + CI (req §3.5, §5 item 7); design §8  
**Note:** no new product features

### Sub-tasks
- [ ] README: setup, run uvicorn, run pytest
- [ ] CI runs pytest on `backend/`
- [ ] Point to design ASCII as architecture anchor (link to `design.md` §1.2)

### Tests
- [ ] CI job passes (no extra test types)

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
