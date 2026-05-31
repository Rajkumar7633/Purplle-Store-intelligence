"""
Structured logging with trace_id, store_id, endpoint, latency_ms.
Every request is logged consistently for ops observability.
"""
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    return trace_id_var.get()


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        trace_id = str(uuid.uuid4())[:8]
        trace_id_var.set(trace_id)
        start = time.perf_counter()

        store_id = request.path_params.get("store_id", "-")

        try:
            response = await call_next(request)
            latency_ms = int((time.perf_counter() - start) * 1000)

            logging.getLogger("store_intelligence.access").info(
                "request completed",
                extra={
                    "trace_id": trace_id,
                    "store_id": store_id,
                    "endpoint": request.url.path,
                    "method": request.method,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                },
            )
            response.headers["X-Trace-ID"] = trace_id
            return response
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logging.getLogger("store_intelligence.access").error(
                "request failed",
                extra={
                    "trace_id": trace_id,
                    "store_id": store_id,
                    "endpoint": request.url.path,
                    "method": request.method,
                    "status_code": 500,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                },
            )
            raise


def setup_logging(level: str = "INFO") -> None:
    import sys

    class StructuredFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            trace = getattr(record, "trace_id", get_trace_id() or "-")
            store = getattr(record, "store_id", "-")
            endpoint = getattr(record, "endpoint", "-")
            latency = getattr(record, "latency_ms", "-")
            event_count = getattr(record, "event_count", "-")
            status = getattr(record, "status_code", "-")

            base = (
                f"{self.formatTime(record)} [{record.levelname}] "
                f"trace={trace} store={store} endpoint={endpoint} "
                f"latency_ms={latency} events={event_count} status={status} "
                f"msg={record.getMessage()}"
            )
            return base

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)
