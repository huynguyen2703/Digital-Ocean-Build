# Repository Structure & Coding Standards

1. **Backend Package**: All runtime application logic MUST reside in `backend/app/`.
   - `database.py`: SQLModel engine initialization and session dependency generator.
   - `models.py`: Database tables (SQLModel) and API request/response schemas (Pydantic).
   - `service.py`: Domain logic, state management, and database mutations.
   - `main.py`: FastAPI endpoints using `Depends(get_session)` and lifespan startup.

2. **Testing Standards**: All test cases MUST reside in `backend/tests/`.
   - `conftest.py`: Shared fixtures for `TestClient` and in-memory database context.
   - `test_main.py`: Route integration and business logic validation.

3. **Deployment**: Maintain `Dockerfile` and `app.yaml` at root for DigitalOcean deployment.
