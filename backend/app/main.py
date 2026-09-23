"""FastAPI application entrypoint — routes, lifespan, observability wiring.

Handlers stay thin: validate DTOs → await ``flag_service`` → map domain errors
to HTTP status codes. No SQL or business rules live here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app.database import get_engine, get_session
from backend.app.models import (
    EvaluationResponse,
    FlagCreate,
    FlagRead,
    FlagUpdate,
    UserOverrideRead,
    UserOverrideUpsert,
)
from backend.app.observability import setup_observability, trace_id_ctx
from backend.app.service import (
    ConflictError,
    NotFoundError,
    RateLimitError,
    StorageError,
    flag_service,
)


def _client_key(request: Request) -> str:
    """Rate-limit bucket from the peer address set by Uvicorn.

    Prefer ``request.client.host`` (populated via ``--proxy-headers`` from the
    trusted platform proxy). Do not read raw ``X-Forwarded-For`` here — that
    would let clients spoof buckets when forwarded headers are overly trusted.
    """
    if request.client and request.client.host:
        return request.client.host
    return "global"


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or trace_id_ctx.get() or "unknown"


def _http_error(status: int, detail: str, request: Request) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create SQLite schema on startup (async, non-blocking driver)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


app = FastAPI(
    title="Feature Flag Service",
    description="Release-targeting feature flags with cached evaluation",
    version="1.0.0",
    lifespan=lifespan,
)
setup_observability(app)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Specs use 400 for validation failures (not 422)."""
    trace_id = _trace_id(request)
    # Strip non-JSON ctx values (e.g. embedded ValueError instances).
    errors = []
    for err in exc.errors():
        clean = {k: v for k, v in err.items() if k != "ctx"}
        errors.append(clean)
    return JSONResponse(
        status_code=400,
        content={"detail": errors, "trace_id": trace_id},
        headers={"X-Trace-Id": trace_id},
    )


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.post("/flags", response_model=FlagRead, status_code=201)
async def create_flag(
    body: FlagCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FlagRead:
    try:
        flag = await flag_service.create_flag(session, body)
        return FlagRead.model_validate(flag)
    except ConflictError as exc:
        raise _http_error(409, exc.message, request) from exc
    except StorageError as exc:
        raise _http_error(503, exc.message, request) from exc


@app.get("/flags/{name}", response_model=FlagRead)
async def get_flag(
    name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FlagRead:
    try:
        flag = await flag_service.get_flag(session, name)
        return FlagRead.model_validate(flag)
    except NotFoundError as exc:
        raise _http_error(404, exc.message, request) from exc
    except StorageError as exc:
        raise _http_error(503, exc.message, request) from exc


@app.patch("/flags/{name}", response_model=FlagRead)
async def patch_flag(
    name: str,
    body: FlagUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FlagRead:
    try:
        flag = await flag_service.set_global_state(session, name, body.enabled)
        return FlagRead.model_validate(flag)
    except NotFoundError as exc:
        raise _http_error(404, exc.message, request) from exc
    except StorageError as exc:
        raise _http_error(503, exc.message, request) from exc


@app.put("/flags/{name}/users/{user_id}", response_model=UserOverrideRead)
async def put_user_targeting(
    name: str,
    user_id: str,
    body: UserOverrideUpsert,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> UserOverrideRead:
    try:
        override = await flag_service.set_user_targeting(
            session, name, user_id, body.enabled
        )
        return UserOverrideRead.model_validate(override)
    except ValueError as exc:
        raise _http_error(400, str(exc), request) from exc
    except NotFoundError as exc:
        raise _http_error(404, exc.message, request) from exc
    except StorageError as exc:
        raise _http_error(503, exc.message, request) from exc


@app.delete("/flags/{name}/users/{user_id}", status_code=204)
async def delete_user_targeting(
    name: str,
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await flag_service.clear_user_targeting(session, name, user_id)
        return Response(status_code=204)
    except ValueError as exc:
        raise _http_error(400, str(exc), request) from exc
    except NotFoundError as exc:
        raise _http_error(404, exc.message, request) from exc
    except StorageError as exc:
        raise _http_error(503, exc.message, request) from exc


@app.get("/flags/{name}/evaluate", response_model=EvaluationResponse)
async def evaluate_flag(
    name: str,
    request: Request,
    user_id: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> EvaluationResponse:
    if user_id is None or not str(user_id).strip():
        raise _http_error(400, "user_id is required", request)
    try:
        return await flag_service.evaluate(
            session,
            name,
            user_id,
            client_key=_client_key(request),
        )
    except ValueError as exc:
        raise _http_error(400, str(exc), request) from exc
    except NotFoundError as exc:
        raise _http_error(404, exc.message, request) from exc
    except RateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except StorageError as exc:
        raise _http_error(503, exc.message, request) from exc
