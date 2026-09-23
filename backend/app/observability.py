import time
import uuid
import logging
from contextvars import ContextVar
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.observability")
trace_id_ctx: ContextVar[str] = ContextVar("trace_id_ctx", default="n/a")

class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4()))
        token = trace_id_ctx.set(trace_id)
        request.state.trace_id = trace_id
        
        start_time = time.perf_counter()
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000
            response.headers["X-Trace-Id"] = trace_id
            response.headers["X-Process-Time"] = f"{duration_ms:.2f}ms"
            return response
        finally:
            trace_id_ctx.reset(token)

async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = trace_id_ctx.get() or getattr(request.state, "trace_id", "unknown")
    status_code = 500
    message = "Internal Server Error"

    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        status_code = exc.status_code
        message = exc.detail

    return JSONResponse(
        status_code=status_code,
        content={"success": False, "trace_id": trace_id, "detail": message},
    )

def setup_observability(app: FastAPI) -> None:
    app.add_middleware(ObservabilityMiddleware)
    app.add_exception_handler(Exception, global_exception_handler)
    app.add_exception_handler(StarletteHTTPException, global_exception_handler)