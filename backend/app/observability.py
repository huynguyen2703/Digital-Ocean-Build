"""Minimal request tracing for developers.

- Accept or generate ``X-Trace-Id``
- Echo it on every response
- Include ``trace_id`` on unhandled 500 bodies
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("app.observability")
trace_id_ctx: ContextVar[str] = ContextVar("trace_id_ctx", default="n/a")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Propagate ``X-Trace-Id`` so logs and clients can correlate a request."""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        token = trace_id_ctx.set(trace_id)
        request.state.trace_id = trace_id
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            trace_id_ctx.reset(token)


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map unexpected errors to JSON with the request trace id."""
    # Prefer request.state: middleware may reset the ContextVar in ``finally``
    # before this handler runs when the route raised.
    trace_id = getattr(request.state, "trace_id", None) or trace_id_ctx.get() or "unknown"
    headers = {"X-Trace-Id": trace_id}

    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        status_code = exc.status_code
        detail = exc.detail
        if getattr(exc, "headers", None):
            headers.update(exc.headers)
    else:
        status_code = 500
        detail = "Internal Server Error"
        logger.exception("unhandled error trace_id=%s", trace_id)

    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "trace_id": trace_id},
        headers=headers,
    )


def setup_observability(app: FastAPI) -> None:
    """Attach tracing middleware and the global exception handler."""
    app.add_middleware(ObservabilityMiddleware)
    app.add_exception_handler(Exception, global_exception_handler)
    app.add_exception_handler(StarletteHTTPException, global_exception_handler)
