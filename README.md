# Feature Flag Service

Production-oriented REST API for **release targeting**: create and store feature flags, enable/disable them globally or per user, and evaluate whether a feature is on for a given user — with in-memory caching, rate limiting, and async I/O for many concurrent clients on a **single node**.

Flags are **management artifacts**, not user-owned objects. Per-user records are **targeting overrides** used to roll features out to specific users.

---

## What we built

| Area | Outcome |
|------|---------|
| **API** | Create / get / patch flags; set & clear per-user targeting; evaluate with `source: global \| override` |
| **Domain** | State pattern (`Enabled` / `Disabled`); evaluate precedence; fail-open evaluate on cache hit if SQLite fails |
| **Performance** | LRU + TTL evaluation cache (`OrderedDict`); sliding-window rate limit + exponential `Retry-After` |
| **Persistence** | SQLite via SQLModel + `aiosqlite`; `BaseRepository` + `FlagRepository`; typed storage errors |
| **Ops** | `X-Trace-Id`, `/health`, Docker (non-root), DigitalOcean App Platform `app.yaml` |
| **Quality** | **33 pytest tests** (unit + API), GitHub Actions CI |

---

## Testing

**Total: 33 tests** — layered from persistence → domain → HTTP (no property-based or load tests).

| Layer | File | Count | What they cover |
|-------|------|------:|-----------------|
| Database | `backend/tests/test_database.py` | 2 | Session open/close, schema create |
| Models | `backend/tests/test_models.py` | 7 | Tables, unique constraints, DTO validation |
| Repository | `backend/tests/test_repository.py` | 7 | CRUD, conflicts, not-found, storage errors, base repo |
| Service | `backend/tests/test_service.py` | 7 | State pattern, precedence, cache invalidation, rate limit/backoff, fail-open |
| Observability | `backend/tests/test_observability.py` | 3 | Trace id generate/echo, 500 + `trace_id` |
| HTTP API | `backend/tests/test_main.py` | 7 | Status codes, targeting/evaluate E2E, 429, health, trace header |

```bash
python3 -m pytest backend/tests/ -v
```

---

## High-level architecture

```
Client
  │
  ▼
main.py (async FastAPI)  ◄── observability (X-Trace-Id)
  │  validate → await service → HTTP status map
  ▼
service.py
  ├── State pattern (enable/disable)
  ├── EvaluationCache (OrderedDict LRU + TTL)
  ├── RateLimiter (sliding window + backoff)
  └── asyncio.Lock (cache + rate-limit maps)
  │
  ▼
repository.py  (BaseRepository + FlagRepository)
  │
  ▼
SQLite (aiosqlite)  — short-lived AsyncSession per request
```

**Dependency rule:** `main` → `service` → `repository` → DB. Handlers never run SQL. The repository never imports the service.

Full request/data-flow notes: [`.docs/specs/design.md`](.docs/specs/design.md) §1.2.

---

## Design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Concurrency | **Async** FastAPI + `aiosqlite` + `asyncio.Lock` | Many concurrent users on one process without blocking the event loop on sync SQLite |
| Scale-out | **Single instance** (`instance_count: 1`) | Cache and rate-limit state are in-process; multi-instance would diverge without Redis |
| Flag model | Object + **UserFlagOverride** | Global release state + optional per-user targeting (not “flags belong to users”) |
| Enablement | **State pattern** | Transition rules live in domain objects; DB still stores `bool` |
| Cache | `OrderedDict` LRU + TTL | O(1) get/put/evict; invalidate on successful writes only |
| Rate limit | Per-client window, **global lock** | Simple & correct for MVP; per-bucket locks are a later optimization |
| Failures | Typed repo errors; evaluate **fail-open** on warm cache | SQLite blips don’t crash the loop; writes never pretend to succeed |
| Deploy imports | `backend.app.*` | Matches `uvicorn backend.app.main:app` on App Platform |
| Container | Non-root UID 10001, `/data` writable only | Limit blast radius; no secrets in the image |
| Proxy trust | Private CIDRs for `--forwarded-allow-ips` | Avoid `*` spoofing of client IP used for rate limiting |

Specs (source of truth):

- [`.docs/specs/requirements.md`](.docs/specs/requirements.md)
- [`.docs/specs/design.md`](.docs/specs/design.md)
- [`.docs/specs/tasks.md`](.docs/specs/tasks.md)

---

## API overview

| Method | Path | Success |
|--------|------|---------|
| `POST` | `/flags` | `201` |
| `GET` | `/flags/{name}` | `200` |
| `PATCH` | `/flags/{name}` | `200` |
| `PUT` | `/flags/{name}/users/{user_id}` | `200` |
| `DELETE` | `/flags/{name}/users/{user_id}` | `204` |
| `GET` | `/flags/{name}/evaluate?user_id=` | `200` |
| `GET` | `/health` | `200` |

**Evaluate precedence:** user override → else global flag → else `404`.

**Common errors:** `400` validation, `409` duplicate name, `404` missing, `429` + `Retry-After`, `503` storage failure on writes / cold evaluate.

---

## Project layout

```
backend/
  app/
    main.py           # Routes, lifespan, HTTP mapping
    service.py        # State, cache, rate limit, evaluate
    repository.py     # BaseRepository + FlagRepository
    models.py         # SQLModel tables + Pydantic DTOs
    database.py       # Async engine / sessions
    observability.py  # X-Trace-Id + error body
  tests/              # pytest (per-layer + API)
.docs/specs/          # requirements → design → tasks
.do/app.yaml          # DigitalOcean App Platform
Dockerfile
```

---

## Local setup

**Requirements:** Python 3.11+ (3.13 preferred; CI/Docker use 3.13).

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest backend/tests/ -v
```

Run the API:

```bash
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8080
```

Optional env (also used on App Platform):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./feature_flags.db` | Async SQLite URL |
| `CACHE_MAX_SIZE` | `10000` | LRU capacity |
| `CACHE_TTL_SECONDS` | `30` | Cache entry TTL |
| `RATE_LIMIT_*` | see `service.py` | Window / max / backoff |

---

## Docker & DigitalOcean

```bash
docker build -t feature-flag-api .
docker run --rm -p 8080:8080 feature-flag-api
```

App Platform config: [`.do/app.yaml`](.do/app.yaml) — Dockerfile deploy, `instance_count: 1`, `/health` checks, SQLite under `/data`. Attach a DO volume later if you need durable data across redeploys (ephemeral disk resets by default).

**Live URL:** https://digital-ocean-feature-flag-buil-vtpmg.ondigitalocean.app

### Manual smoke tests (`curl`)

Use the same commands against local Docker/uvicorn or the live App Platform URL.

```bash
# Local
export BASE_URL=http://127.0.0.1:8080

# Live App Platform deploy
export BASE_URL=https://digital-ocean-feature-flag-buil-vtpmg.ondigitalocean.app
```

```bash
# Health (App Platform health check path)
curl -sS -D- "$BASE_URL/health"

# Create a flag → expect 201 and X-Trace-Id
curl -sS -D- -X POST "$BASE_URL/flags" \
  -H 'Content-Type: application/json' \
  -H 'X-Trace-Id: manual-1' \
  -d '{"name":"dark_mode","description":"test","enabled":false}'

# Get flag → 200
curl -sS "$BASE_URL/flags/dark_mode"

# Enable globally → 200
curl -sS -X PATCH "$BASE_URL/flags/dark_mode" \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true}'

# Per-user targeting → 200
curl -sS -X PUT "$BASE_URL/flags/dark_mode/users/user-1" \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}'

# Evaluate → 200, source should be "override", enabled false
curl -sS "$BASE_URL/flags/dark_mode/evaluate?user_id=user-1"

# Clear targeting → 204
curl -sS -D- -X DELETE "$BASE_URL/flags/dark_mode/users/user-1"

# Evaluate again → source "global", enabled true
curl -sS "$BASE_URL/flags/dark_mode/evaluate?user_id=user-1"

# Duplicate create → 409
curl -sS -D- -X POST "$BASE_URL/flags" \
  -H 'Content-Type: application/json' \
  -d '{"name":"dark_mode","enabled":false}'

# Bad name → 400
curl -sS -D- -X POST "$BASE_URL/flags" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Bad-Name","enabled":false}'

# Missing user_id → 400
curl -sS -D- "$BASE_URL/flags/dark_mode/evaluate"

# Burst evaluate → eventually 429 with Retry-After (default limit 120/min)
for i in $(seq 1 130); do
  curl -sS -o /dev/null -w "%{http_code}\n" \
    "$BASE_URL/flags/dark_mode/evaluate?user_id=burst"
done
```

**What “good” looks like after deploy**

- `/health` returns `{"status":"ok"}` and App Platform health checks stay green  
- Create → target → evaluate round-trip returns expected `source` / `enabled`  
- Responses include `X-Trace-Id`; `429` includes `Retry-After`  
- Optional: open `$BASE_URL/docs` for interactive Swagger  
- Watch **Runtime Logs** in the DO console while curling  

---

## How this project was built (specs + AI-assisted workflow)

This repo was developed with a **gated, top-down then bottom-up** workflow so the AI agent never jumped ahead of human approval.

### 1. Steering

- [`.docs/steering/system_prompt.md`](.docs/steering/system_prompt.md) — architect role, stack constraints, required spec files  
- [`.docs/steering/structure.md`](.docs/steering/structure.md) — module layout and test locations  

### 2. Specs before code

| Stage | Artifact | Gate |
|-------|----------|------|
| Requirements | `.docs/specs/requirements.md` | Closed gaps (targeting vs ownership, State pattern, async concurrency, SLAs) |
| Design | `.docs/specs/design.md` | Components, primitives, contracts, failure boundaries, test plan |
| Tasks | `.docs/specs/tasks.md` | Checkbox checklist, one primary file per task, paired tests |

**Rule:** no implementation until the current spec (or iteration) was approved.

### 3. Implementation iterations (AI coding with approval)

Bottom-up, one controlled slice per iteration:

0–1. Test harness + async `database.py`  
2. SQLModel tables  
3. Pydantic DTOs  
4. Repository (`BaseRepository` + `FlagRepository`)  
5. Service (state, cache, rate limit, evaluate)  
6. Observability (minimal tracing)  
7. `main.py` routes + HTTP mapping  
Deploy. Package path `backend.app`, Dockerfile, `app.yaml`  
8. This README + CI  

Each iteration: agent proposed the task set → human approved → implement + pytest → stop for the next approval.

### 4. Why that workflow

- Specs absorb ambiguity (e.g. “flags belong to users” → release targeting)  
- Bottom-up keeps failures local (models before API)  
- Explicit gates prevent drive-by refactors and scope creep  
- Tests are attached to each layer so regressions surface early  

---

## CI

GitHub Actions (`.github/workflows/ci.yml`) installs dependencies and runs `pytest` on push/PR to `main`.
