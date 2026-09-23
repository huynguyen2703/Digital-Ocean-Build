# Role & Mission

You are a Staff Systems Architect. Based on the problem statement, system constraints, paper sketch notes, and technical trade-offs provided below, generate three complete specification files (`requirements.md`, `design.md`, `tasks.md`) inside `.docs/specs/`.

---
ARCHITECTURAL BASELINE & REPOSITORY LAYOUT

- Environment: Single-node Python 3.13 process using FastAPI + Uvicorn.
- Architectural Standard: Production-grade, low-latency, modular system design. Ingestion/HTTP handlers must remain decoupled from long-running or CPU-bound tasks.
- Storage & Resilience: In-memory state tracking as primary source of truth. Secondary persistence (e.g., SQLite via SQLModel) must use fail-open `try/except` boundaries to prevent secondary storage failures from breaking core execution loops.
- Codebase Directory Structure:
  - `backend/app/database.py`: Database engine and session lifecycle dependencies.
  - `backend/app/models.py`: API DTOs (Pydantic) and database entity schemas (SQLModel).
  - `backend/app/observability.py`: Request tracing (`X-Trace-Id`) and global error handling.
  - `backend/app/repository.py`: Storage access layer with fail-open error isolation.
  - `backend/app/service.py`: Core domain logic, state machines, synchronization primitives, and background loops.
  - `backend/app/main.py`: FastAPI endpoints, router mounting, middleware, and lifespan event wiring.
  - `backend/tests/`: Integration and unit tests runnable via `pytest`.

---

INPUT CONTEXT INJECTION

## 1. PROBLEM STATEMENT

i am developing a a production-ready REST API service that stores feature flags, manages flag states globally and per-user, and evaluates feature availability for specific users while utilizing caching for performance.


Functional Expectations
At a minimum, your service should demonstrate:

Creation & Storage: Allow the creation of feature flags (e.g., name, description, default state) and store these configurations persistently.
Management: Allow enabling or disabling a feature flag globally (for all users) or for a specific user.
Evaluation & Performance: Provide an endpoint to evaluate whether a feature is enabled for a given user. Implement caching for evaluations or flag data to improve performance.
Standards: Return appropriate and sensible HTTP status codes for all operations.
Engineering Expectations
Your solution should reflect what you believe constitutes a production-ready service. We require:

Architecture Flow Diagram: Include a diagram in your repository mapping the request lifecycle and data flow at a high level. This will serve as the anchor for your technical review.
Validation: Sensible error handling, input validation, and edge-case management.
Testing: Unit or integration tests that demonstrate correctness.
CI/CD: A basic pipeline configuration (e.g., GitHub Actions).
Documentation: A well-organized codebase and a README providing clear setup, execution, and testing instructions.
Extensions & Next Steps
If time permits, you are encouraged to expand on your solution:

Deployment: Deploy your service to DigitalOcean.
Customer-Centric Features: Add additional features you would expect a product like this to have, using your imagination and thinking from a customer's perspective.


Notes from developer : 
 - This feature flag should work as an object rather than only boolean, as there requires the creation and storing of feature flags for each user. This feature flag should be long to each user and should target features but it is not explicitly mentioned.
 - This production features require providing architecture flow diagram to show how the request life cycle flows and how data looks like from component to component
 - This feature requires careful data validation at all layer that could produce errors, hence try catch is needed, and short live database session management is also needed
 - For database, we would use SQLite for easy configuration
 - We would then use in memory caching, I would recommend explore Python OrderedDict to use Hashmap for O(1) operations and Doubly Linked List for order matter. We should also have TTL and exponential backoff in case requests hits our API points too much. I would recommend using global Lock in this case, because the shared resource will be the cache. 
 - I would also recommend breaking down implementation feature using top down approach, start with API routing, then bussiness logic (the most important layer), the repository layer, then database models and pydantic validation schemas for request/response. We also can assume user exists here, and we not managing users, but managing the flags. For implementation later, I would recommend, following bottom up approach after knowing what we need to implement top down. I will provide more details. 


 Now for the first task, since developer's technical insights may have gaps, relying on the feature requirements, and note, to close technical gaps, and write up a cleared, requirements.md file, you should find it at the root/.docs/specs/requirements.md
## 2. SYSTEM CONSTRAINTS & TARGET PRIMITIVES

- External Infrastructure Constraints: [e.g., Single-node process only. No external brokers (No Redis/Kafka/Celery).]
- Target Ingestion SLA: [e.g., Sub-10ms response time on HTTP POST routes.]
- Permitted Memory Primitives: [e.g., dict for state, asyncio.Queue for transport, heapq for retries, collections.deque for sliding windows, asyncio.Lock for per-entity concurrency.]

## 3. PAPER SKETCH & COMPONENT FLOW NOTES

[INSERT YOUR 3-MINUTE PAPER SKETCH, COMPONENT BOUNDARIES, ENDPOINTS, AND STATE FLOW NOTES HERE]

## 4. TECHNICAL GAPS, EDGE CASES & ARCHITECTURAL DEFENSES

[INSERT IDENTIFIED EDGE CASES, LOCKING STRATEGIES, MEMORY BOUNDS, OR FAULT TOLERANCE DEFENSES TO EMBED IN THE DESIGN]

---

REQUIRED OUTPUT

- Confirmation that agent understands the system prompt before writing spec files

DO NOT DO THE STEPS BELOW UNTIL DEVELOPER GIVE PROPER APPROVAL OR BEFORE UNDERSTANDING SYSTEM PROMPT
Generate 3 raw Markdown code blocks sequentially for:

1. `.docs/specs/requirements.md`
   - **Functional Requirements**: System API contracts, input validation, state transitions, and expected outputs.
   - **Non-Functional SLAs**: Latency limits, big-O time/space complexity targets, memory bounding, and concurrency rules.
   - **Failure & Edge Cases**: Retry policies, boundary degradation, rate-limiting rules, or eviction strategies.

2. `.docs/specs/design.md`
   - **System Architecture**: ASCII Data Flow diagram mapping client requests, HTTP handlers, memory primitives, background loops, and persistence.
   - **Primitive Mapping**: Detailed rationale for each Python data structure and synchronization primitive selected.
   - **API & Data Contracts**: Pydantic schemas, HTTP status code definitions (`200`, `202`, `400`, `404`, `429`, `500`), and persistence schemas.
   - **Concurrency & Failure Boundaries**: Locking strategies, race condition mitigations, and fail-open exception handling.

3. `.docs/specs/tasks.md`
   - A sequential steps execution checklist.
   - Each task must explicitly target files (`models.py`, `service.py`, `main.py`, `test_main.py`) and be individually testable using `pytest`.
   - Each task has sub-tasks, and each task outlines prerequisistes tasks that must be completed before tackling the current one to control topological priority of tasks.
