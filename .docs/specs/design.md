# Feature Flag Service — Design

Source of truth for behavior: [`.docs/specs/requirements.md`](requirements.md).  
This document separates implementable components, contracts, primitives, failure boundaries, and **testing requirements**. No code in this step.

---

## 1. System Architecture

### 1.1 Component Map

| Component | File | Responsibility |
|-----------|------|----------------|
| HTTP edge | `backend/app/main.py` | **Async** routes, status codes, `Depends(get_session)`, lifespan (create tables), mount middleware |
| Observability | `backend/app/observability.py` | `X-Trace-Id` middleware; global exception → JSON error body |
| Domain service | `backend/app/service.py` | State pattern, evaluate precedence, LRU+TTL cache, rate limiter, cache invalidation (`asyncio.Lock`) |
| Repository | `backend/app/repository.py` | **Async** SQLite CRUD only; `try/except` fail-open isolation; typed errors upward |
| Models | `backend/app/models.py` | SQLModel tables + Pydantic request/response DTOs |
| Database | `backend/app/database.py` | Async engine (`aiosqlite`), `get_session()` async generator |
| Tests | `backend/tests/conftest.py`, `backend/tests/test_main.py` | Fixtures + integration/unit coverage (`pytest-asyncio`) |

**Dependency rule:** `main` → `service` → `repository` → async session/SQLModel.  
Handlers never talk to the DB. Repository never imports service. Cache/rate-limit/state live in service.

**Concurrency mandate:** many concurrent users on a single node via asyncio I/O (non-blocking DB). Not multi-node scale-out.

### 1.2 Request Lifecycle (ASCII)

```
Client
  |
  v
+------------------+     X-Trace-Id + error mapping
|  main.py (HTTP)  | <---------------------------- observability.py
+--------+---------+
         |  validate DTO (Pydantic 400)
         |  rate-limit check (service) --> 429 + Retry-After
         v
+------------------+
|   service.py     |
|  - State pattern |     +---------------------------+
|  - evaluate()    |---->| LRU cache (OrderedDict)   |
|  - mutations     |     | TTL + asyncio.Lock        |
+--------+---------+     +---------------------------+
         | miss / write (await)
         v
+------------------+     fail-open try/except
| repository.py    | ------------------------------> typed StorageError
| (async methods)  |
+--------+---------+
         |
         v
+------------------+
| SQLite+aiosqlite |  short-lived AsyncSession per request
+------------------+
```

### 1.3 Evaluate Data Flow

```
GET /flags/{name}/evaluate?user_id=U
  1. Rate limit (sliding window) --exceed--> 429
  2. Cache lookup key=(name,U) under asyncio.Lock
       HIT + not expired --> return EvaluationResponse (fail-open path if DB later unused)
       MISS --> await repository.get_flag(name)
                 missing --> 404
               await repository.get_override(name,U) optional
               State/precedence --> enabled + source
               Cache put (asyncio.Lock) --> return 200
  3. On DB error:
       cache HIT usable --> return cached (fail-open)
       else --> 503
```

### 1.4 Mutation Data Flow (create / patch / targeting)

```
Write request
  --> service applies State transition (Enabled/Disabled)
  --> await repository persist
        fail --> 503 (do not treat as committed; do not poison cache as success)
        ok   --> invalidate cache keys under asyncio.Lock --> 2xx response
```

Global patch: invalidate all evaluation keys for `flag_name` (scan keys with prefix or secondary index set).  
Targeting PUT/DELETE: invalidate only `(flag_name, user_id)`.

No background worker loops required for MVP. Lifespan only initializes schema (`run_sync(create_all)`).

---

## 2. Primitive Mapping

| Primitive | Role | Rationale |
|-----------|------|-----------|
| `collections.OrderedDict` | Evaluation LRU cache | O(1) get/move_to_end/evict from front (hash + DLL ordering) |
| `dict` value payload | `{enabled, source, expires_at}` per cache entry | Structured evaluate result + TTL |
| `asyncio.Lock` | Global lock around cache + rate-limit maps | Matches async request path; safe across concurrent coroutines on one event loop |
| `collections.deque` | Per-client sliding window of request timestamps | O(1) append/popleft; bound memory by dropping old stamps |
| `dict[str, deque]` | Rate-limit buckets keyed by client id (IP or `X-Forwarded-For` / fallback `"global"`) | In-process only; acceptable single-node |
| SQLModel `AsyncSession` | Short-lived unit of work | `Depends(get_session)` async yield/close per request |
| `aiosqlite` | Async SQLite driver | Non-blocking DB I/O for many concurrent users |
| Unique DB indexes | `Flag.name`; `(UserFlagOverride.flag_name, user_id)` | Enforce 409 / fast lookup |

**Not used (constraints):** Redis, Kafka, Celery, `threading.Lock` on the hot path, multiprocessing shared memory.

**Defaults (configurable constants in service):**
- `CACHE_MAX_SIZE = 10_000`
- `CACHE_TTL_SECONDS = 30`
- `RATE_LIMIT_WINDOW_SECONDS = 60`
- `RATE_LIMIT_MAX_REQUESTS = 120` (evaluate; stricter optional on writes)
- `RETRY_AFTER_BASE_SECONDS = 1`
- `RETRY_AFTER_CAP_SECONDS = 60` (exponential `Retry-After = min(cap, base * 2^(streak-1))`)

---

## 3. State Pattern Design

### 3.1 Types

```
FlagState (Protocol / ABC)
  + is_enabled() -> bool
  + enable(ctx) -> FlagState
  + disable(ctx) -> FlagState

EnabledState(FlagState)
  + is_enabled() -> True
  + enable -> self
  + disable -> DisabledState()

DisabledState(FlagState)
  + is_enabled() -> False
  + enable -> EnabledState()
  + disable -> self

def state_from_bool(enabled: bool) -> FlagState
def bool_from_state(state: FlagState) -> bool
```

- Persistence still stores `enabled: bool` on rows; service hydrates `FlagState` on read and writes bool after transition.
- Global PATCH and per-user PUT call `enable()` / `disable()` on the appropriate scope’s state object — handlers only pass the desired boolean into service methods that perform the transition.
- Evaluate: resolve effective bool via precedence, then expose through `state_from_bool(...).is_enabled()` so evaluation does not bypass the state model with a parallel ad-hoc path.

### 3.2 Transition Table

| Scope | API | Transition |
|-------|-----|------------|
| Global Flag | `PATCH` `enabled=true` | `-> EnabledState` |
| Global Flag | `PATCH` `enabled=false` | `-> DisabledState` |
| User targeting | `PUT` `enabled=true/false` | upsert override row in that state |
| User targeting | `DELETE` | remove override; effective state falls back to global Flag state |

---

## 4. API & Data Contracts

### 4.1 Persistence (SQLModel) — `models.py`

**Table `flags`**
- `id: Optional[int]` PK
- `name: str` unique indexed
- `description: str` default `""`
- `enabled: bool` default `False`
- `created_at: datetime`
- `updated_at: datetime`

**Table `user_flag_overrides`**
- `id: Optional[int]` PK
- `flag_name: str` FK → `flags.name` (or logical FK + index)
- `user_id: str`
- `enabled: bool`
- `updated_at: datetime`
- UniqueConstraint(`flag_name`, `user_id`)

### 4.2 Pydantic DTOs — `models.py`

| Schema | Fields |
|--------|--------|
| `FlagCreate` | `name`, `description=""`, `enabled=False` |
| `FlagUpdate` | `enabled: bool` |
| `FlagRead` | `name`, `description`, `enabled`, `created_at`, `updated_at` |
| `UserOverrideUpsert` | `enabled: bool` |
| `UserOverrideRead` | `flag_name`, `user_id`, `enabled`, `updated_at` |
| `EvaluationResponse` | `flag`, `user_id`, `enabled`, `source: Literal["override","global"]` |
| `ErrorBody` | `detail`, `trace_id` |

Validation mirrors requirements: name slug `^[a-z][a-z0-9_]{1,63}$`; description ≤ 512; `user_id` 1..128 after trim.

### 4.3 HTTP Status Map

| Code | Use |
|------|-----|
| `200` | GET flag, PATCH, PUT override, evaluate |
| `201` | POST create |
| `204` | DELETE override |
| `400` | Pydantic / empty user_id |
| `404` | Unknown flag; DELETE missing override |
| `409` | Duplicate flag name |
| `429` | Rate limited (`Retry-After` header) |
| `500` | Unhandled (observability) |
| `503` | Persistence failure on write; evaluate miss + DB down |

(`202` not used in MVP.)

### 4.4 Route → Service Methods

| Route | Service method |
|-------|----------------|
| `POST /flags` | `create_flag(session, body)` |
| `GET /flags/{name}` | `get_flag(session, name)` |
| `PATCH /flags/{name}` | `set_global_state(session, name, enabled)` |
| `PUT /flags/{name}/users/{user_id}` | `set_user_targeting(session, name, user_id, enabled)` |
| `DELETE /flags/{name}/users/{user_id}` | `clear_user_targeting(session, name, user_id)` |
| `GET /flags/{name}/evaluate` | `evaluate(session, name, user_id, client_key)` |

---

## 5. Component Specs (What to Implement)

### 5.1 `database.py`
- Async SQLite URL (`sqlite+aiosqlite:///...`; tests use `:memory:` or temp file via `init_engine`).
- `create_async_engine(...)`.
- `async def get_session()` yielding `AsyncSession`, closed after request.
- Schema create via lifespan: `await conn.run_sync(SQLModel.metadata.create_all)`.

### 5.2 `repository.py`
- **Async** methods: `create_flag`, `get_flag_by_name`, `update_flag_enabled`, `upsert_override`, `get_override`, `delete_override`.
- Catch SQLAlchemy/SQLModel exceptions → raise domain `StorageError` / `ConflictError` / `NotFoundError` (defined in service or shared errors module inside these files — prefer small exception classes in `repository.py` or `service.py`, not a new package unless needed).
- Never crash the process; no bare `except:` swallowing without log.

### 5.3 `service.py`
- Singleton-ish module-level `FlagService` holding: cache, **`asyncio.Lock`**, rate-limit state, TTL/max config.
- State classes + transitions (sync pure logic is fine inside async methods).
- Cache get/put/invalidate under `async with lock`.
- Rate limit before evaluate (and optionally writes); exponential `Retry-After`.
- `async` orchestration of repository; maps errors to HTTP-meaningful exceptions for `main` (prefer **main maps domain errors → HTTP** so service stays framework-light).

### 5.4 `observability.py`
- Middleware: ensure `X-Trace-Id` on request/response.
- Handler for uncaught exceptions → `500` + `ErrorBody`.

### 5.5 `main.py`
- Async lifespan: create tables.
- Wire **async** routes; inject session; `await` service; translate domain errors to status codes.

---

## 6. Concurrency & Failure Boundaries

| Boundary | Strategy |
|----------|----------|
| Cache races | Single `asyncio.Lock` for get/put/evict/invalidate |
| Rate-limit races | Same lock or dedicated `asyncio.Lock`; MVP may share global lock |
| Session leaks | `get_session` always closes / exits async context |
| Write + cache | Persist first; invalidate only after successful commit |
| Evaluate + DB down | Fail-open on valid cache hit; else 503 |
| Duplicate create | Unique constraint → `ConflictError` → 409 |
| Stale evaluate after global change | Invalidate all keys for flag name under lock |
| Event-loop blocking | No sync SQLite/`time.sleep` on hot path |

No distributed locking (single-node).

---

## 7. Testing Implementation Requirements

All tests live under `backend/tests/`, runnable with `pytest`. Prefer **TestClient** integration tests in `test_main.py` plus focused unit tests for state/cache/rate-limit (same file or `test_service.py` if it grows — MVP may keep one `test_main.py` + helpers in `conftest.py`).

### 7.1 Fixtures (`conftest.py`)

| Fixture | Requirement |
|---------|-------------|
| `engine` / `session` | Isolated in-memory async SQLite; create schema per test (function scope) |
| `client` | `httpx.AsyncClient` (or async TestClient) with overridden `get_session` |
| `service` reset | Clear LRU cache + rate-limit buckets between tests (autouse fixture or client factory) |

Tests must not share mutable cache/rate-limit state across cases.

### 7.2 Mandatory Test Cases

**Flag CRUD & validation**
- Create flag → `201` + body fields
- Duplicate name → `409`
- Invalid name / empty user_id → `400`
- Get unknown → `404`
- Patch global enable/disable → `200` + persisted value

**Targeting**
- PUT targeting on existing flag → `200`
- PUT targeting on missing flag → `404`
- DELETE targeting → `204`; second DELETE → `404`

**Evaluate precedence**
- No override → `source=global`, matches flag.enabled
- Override true while global false → `enabled=true`, `source=override`
- After DELETE override → falls back to global
- Unknown flag evaluate → `404`

**State pattern**
- Unit: `EnabledState.disable()` → disabled; `DisabledState.enable()` → enabled; idempotent enable/disable

**Cache**
- Evaluate twice: second call still correct (smoke)
- After PATCH global, evaluate reflects new global (invalidation)
- After PUT/DELETE targeting, evaluate reflects change (invalidation)

**Rate limit**
- Burst past limit on evaluate → `429` with `Retry-After` present

**Persistence failure / fail-open** (minimal)
- Mock repository raise `StorageError` on evaluate with pre-seeded cache entry → still `200` (fail-open)
- Mock repository raise on write → `503`

**Observability**
- Response includes `X-Trace-Id`

### 7.3 Out of Test Scope (MVP)
- Load/p99 latency benchmarks in CI
- Multi-process concurrency stress
- DigitalOcean live deploy smoke (manual / later)

---

## 8. Engineering Deliverables (Non-Runtime)

Tracked for later `tasks.md` (not implemented in this design step):
- README: setup, run uvicorn, pytest
- GitHub Actions CI running pytest
- Architecture diagram copy in README or `.docs/` (this §1.2 ASCII is the design anchor)
- Dockerfile / `.do/app.yaml` as per structure standards

---

## 9. Orchestration Note

This document is **design only**. `tasks.md` and all code changes require explicit developer approval before proceeding.
