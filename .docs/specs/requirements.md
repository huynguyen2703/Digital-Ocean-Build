# Feature Flag Service — Requirements

## 1. Purpose

Build a production-ready, single-node REST API that stores feature flag definitions for **release targeting**: operators manage whether a feature is on globally or targeted to specific users, and the service evaluates availability for a given user. Flags belong to **management/release control**, not to users as owned objects. Evaluation must be cache-accelerated. The service must support **many concurrent users** via an **async I/O** request path (non-blocking DB and handlers). Users are assumed to exist externally; this service does not manage user identity (no user CRUD, no authentication in MVP).

## 2. Functional Requirements

### 2.1 Domain Model

A feature flag is a **first-class object**, not a bare boolean. It is a management artifact used to target releases.

| Entity | Responsibility |
|--------|----------------|
| **Flag** | Named feature definition under management control: `name`, `description`, `enabled` (global release state), `created_at`, `updated_at`. |
| **UserFlagOverride** | Optional **per-user targeting** rule on an existing Flag (not user ownership): `flag_name`, `user_id`, `enabled`, `updated_at`. |

- `user_id` is an opaque non-empty string identifying the release target. The service does not validate that the user exists in an identity system.
- Per-user records are **targeting overrides** managed by the flag service; flags do not “belong” to users.

### 2.2 Evaluation Precedence

When evaluating flag `name` for `user_id`:

1. If a **UserFlagOverride** (per-user targeting) exists for `(name, user_id)` → use `override.enabled`; `source = "override"`.
2. Else if the **Flag** exists → use `flag.enabled`; `source = "global"`.
3. Else → flag unknown → **404**.

Evaluation responses are structured objects, not bare booleans:

```json
{
  "flag": "dark_mode",
  "user_id": "user-123",
  "enabled": true,
  "source": "override"
}
```

### 2.3 API Contracts

| Method | Path | Purpose | Success |
|--------|------|---------|---------|
| `POST` | `/flags` | Create a flag (`name`, `description`, `enabled`) | `201` |
| `GET` | `/flags/{name}` | Fetch flag definition | `200` |
| `PATCH` | `/flags/{name}` | Enable or disable globally | `200` |
| `PUT` | `/flags/{name}/users/{user_id}` | Set or replace per-user targeting (`enabled`) | `200` |
| `DELETE` | `/flags/{name}/users/{user_id}` | Clear per-user targeting | `204` |
| `GET` | `/flags/{name}/evaluate?user_id=` | Evaluate availability for a targeted user (cached) | `200` |

#### Create (`POST /flags`)

- **Input**: `name` (required), `description` (optional, default `""`), `enabled` (optional, default `false`).
- **Output**: full Flag object.
- **Errors**: `400` validation; `409` if `name` already exists.

#### Get (`GET /flags/{name}`)

- **Output**: full Flag object.
- **Errors**: `404` if unknown.

#### Patch global (`PATCH /flags/{name}`)

- **Input**: `{ "enabled": bool }` (partial update; at least `enabled` required).
- **Output**: updated Flag object.
- **Side effect**: invalidate related evaluation cache entries for this flag.
- **Errors**: `400`, `404`.

#### Set per-user targeting (`PUT /flags/{name}/users/{user_id}`)

- **Input**: `{ "enabled": bool }`.
- **Output**: UserFlagOverride object (targeting rule).
- **Side effect**: invalidate evaluation cache for `(name, user_id)`.
- **Errors**: `400`, `404` if flag does not exist.

#### Clear per-user targeting (`DELETE /flags/{name}/users/{user_id}`)

- **Output**: empty body.
- **Side effect**: invalidate evaluation cache for `(name, user_id)`.
- **Errors**: `404` if flag or targeting rule does not exist.

#### Evaluate (`GET /flags/{name}/evaluate?user_id=`)

- **Query**: `user_id` required.
- **Output**: evaluation object (`flag`, `user_id`, `enabled`, `source`).
- **Behavior**: serve from cache on hit; on miss load from durable store, populate cache, return result.
- **Errors**: `400` missing/invalid `user_id`; `404` unknown flag; `429` rate limited; `503` when durable store unavailable and no usable cache entry.

### 2.3.1 State Pattern (mandatory)

Flag enablement must be modeled with the **State pattern** in domain logic (`service.py`), not ad-hoc booleans scattered across handlers:

- Explicit states for release control (at minimum **Enabled** and **Disabled**), applicable at global Flag scope and at per-user targeting scope.
- State transitions (`enable` / `disable`, and clear-targeting → fall back to global) are performed via state objects/handlers; API `PATCH`/`PUT`/`DELETE` drive transitions, they do not embed transition rules inline.
- Evaluation reads the effective state after applying targeting precedence (§2.2); it does not invent a parallel boolean path that bypasses the state model.
- Design.md will detail concrete state classes and transition tables; this requirement locks the pattern in for implementation.

### 2.4 Input Validation

Apply at the API boundary (Pydantic) and defend in service/repository layers:

| Field | Rules |
|-------|--------|
| `name` | Non-empty; slug pattern `^[a-z][a-z0-9_]{1,63}$` (lowercase, digits, underscore); unique. |
| `description` | Optional string; max length 512. |
| `enabled` | Boolean where present. |
| `user_id` | Non-empty string; max length 128; no leading/trailing whitespace after trim (reject empty after trim). |

Invalid input → **400** with a clear error body. Unhandled failures → **500** including `X-Trace-Id`.

### 2.5 HTTP Status Codes

| Code | When |
|------|------|
| `200` | Successful read/update/evaluate |
| `201` | Flag created |
| `204` | Override deleted |
| `400` | Validation failure |
| `404` | Unknown flag or missing override (delete) |
| `409` | Duplicate flag name |
| `429` | Rate limit exceeded |
| `500` | Unexpected internal error |
| `503` | Durable store unavailable and operation cannot complete (writes; evaluate on cache miss) |

### 2.6 Persistence & Cache Semantics

- **Durable store**: SQLite via SQLModel over **async** driver (`aiosqlite`). Flag definitions and overrides must survive process restart.
- **Hot path**: in-memory evaluation (and/or flag) cache with TTL and LRU eviction.
- **Mutations** (create, patch global, set/clear override) must persist to SQLite. If persistence fails → **503**; do not silently claim success.
- **Reads/evaluate**: prefer cache; on miss read SQLite. If SQLite fails but a valid (non-expired) cache entry exists → **fail-open** and return cached result. If SQLite fails and cache miss → **503**.
- Mutations must **invalidate** affected cache keys so subsequent evaluates are not stale.

### 2.7 Out of Scope (MVP)

- User registration, authentication, authorization
- Multi-node / shared cache (Redis), message brokers, Celery
- Percentage rollouts, cohort/percentage targeting beyond per-user rules, audit log UI
- Soft-delete / flag archival (may be a later extension)

---

## 3. Non-Functional Requirements (SLAs)

### 3.1 Runtime Constraints

- **Environment**: single-node Python 3.13 process; FastAPI + Uvicorn with **async** route handlers.
- **Concurrency model (must)**: asyncio end-to-end for request I/O — async sessions, async repository/service methods awaited from handlers. Do **not** call blocking SQLite APIs on the event loop.
- **Infrastructure**: no external brokers (no Redis, Kafka, Celery). Async improves many-user concurrency on one node; it is not multi-node scale-out.
- **Module layout** (mandatory):
  - `backend/app/database.py` — async engine and short-lived async session dependency
  - `backend/app/models.py` — Pydantic DTOs + SQLModel entities
  - `backend/app/observability.py` — `X-Trace-Id`, global error handling
  - `backend/app/repository.py` — async storage access with fail-open isolation
  - `backend/app/service.py` — domain logic (State pattern), cache, rate limiting, locks
  - `backend/app/main.py` — async routes, middleware, lifespan
  - `backend/tests/` — pytest (+ pytest-asyncio) unit/integration tests

### 3.2 Latency & Complexity

| Concern | Target |
|---------|--------|
| Evaluate path, cache hit | p99 **&lt; 10 ms** under local single-node load |
| Cache get / put / LRU touch | **O(1)** average |
| LRU eviction | **O(1)** via `collections.OrderedDict` |
| Override / flag lookup by key (DB) | indexed; expected **O(log n)** / constant via unique indexes |

### 3.3 Cache Bounds

- Implementation: `OrderedDict` (hash map + doubly linked list ordering) for LRU.
- Each entry carries an absolute **TTL** expiry timestamp.
- Maximum entry count **N** (configurable; default e.g. 10_000). Evict least-recently-used when full.
- Cache key for evaluation: `(flag_name, user_id)`.

### 3.4 Concurrency

- **Must** serve many concurrent evaluate/management requests without blocking the event loop on DB I/O.
- Shared cache is a critical section: protect with a single process-wide **`asyncio.Lock`**.
- HTTP handlers are `async def`; they await service/repository; no long CPU-bound work inline.
- Database sessions are **short-lived** per request (`Depends(get_session)` async generator); always closed after the request.

### 3.5 Observability & Engineering Bar

- Every response includes or correlates with `X-Trace-Id`.
- Sensible error handling and edge-case coverage (see §4).
- Unit/integration tests via pytest demonstrating create, override, evaluate precedence, cache invalidation, and error codes.
- CI pipeline (e.g. GitHub Actions) and README with setup, run, and test instructions (engineering expectation; tracked in later tasks).
- Architecture flow diagram in-repo (design/docs deliverable; not this file).

---

## 4. Failure & Edge Cases

### 4.1 Domain Edge Cases

| Case | Expected behavior |
|------|-------------------|
| Duplicate `POST /flags` name | `409` |
| Evaluate / get unknown flag | `404` |
| Override on unknown flag | `404` |
| Delete missing override | `404` |
| Global disable with per-user targeting `enabled=true` | Evaluate returns `enabled=true`, `source=override` |
| Clear targeting after global change | Evaluate falls back to current global `enabled` |
| Empty / whitespace `user_id` | `400` |
| Malformed flag `name` | `400` |

### 4.2 Cache & Consistency

- After any successful mutation affecting a flag or `(flag, user)`, related cache entries are invalidated **before** returning success (or atomically within the same critical section as the update).
- Expired TTL entries are treated as misses.
- Under lock contention, correctness &gt; micro-optimizing lock scope; prefer one global cache lock for MVP simplicity.

### 4.3 Rate Limiting & Backoff

- Apply a sliding-window (or equivalent) rate limit on hot endpoints, at minimum **evaluate** (and optionally all write routes).
- On exceed: **429** with `Retry-After` set to support **exponential backoff** on the client (e.g. base delay doubling per consecutive 429, capped).
- Rate-limit state is in-process only (single-node); reset on process restart is acceptable for MVP.

### 4.4 Storage Fault Tolerance

| Path | Durable store failure | Behavior |
|------|----------------------|----------|
| Write (create/patch/override) | Error | Surface **503**; do not update cache as if committed |
| Evaluate, cache hit (valid TTL) | Error | **Fail-open**: return cached evaluation |
| Evaluate, cache miss | Error | **503** |
| Repository layer | Any unexpected DB error | Catch at repository boundary; log with trace id; propagate typed failure to service (no raw driver leaks to clients) |

### 4.5 Degradation Rules

- Secondary persistence must never crash the process event loop; wrap DB I/O in `try/except` at the repository boundary.
- Process restart: durable flags/overrides reload from SQLite; cache starts cold.
- Memory pressure: LRU + max-N prevent unbounded cache growth; rate limiter windows must also be bounded (e.g. deque/sliding window with max tracked keys).

---

## 5. Acceptance Criteria (MVP Done When)

1. Flags can be created and retrieved with durable SQLite storage; they are management/release-targeting artifacts, not user-owned.
2. Global enable/disable and per-user targeting work; evaluate respects precedence in §2.2; domain transitions use the State pattern (§2.3.1).
3. Evaluate uses in-memory LRU+TTL cache; mutations invalidate correctly.
4. Validation and status codes match §2.4–§2.5.
5. Rate limiting returns `429` with `Retry-After` under abuse.
6. DB failures degrade per §4.4 without process crash.
7. Tests and modular layout match steering structure; CI and README exist as engineering deliverables (scheduled in `tasks.md` after design approval).

---

## 6. Orchestration Note

This document is **requirements only**. `design.md` and `tasks.md` are deferred until explicit developer approval. No implementation proceeds until those specs are approved.
