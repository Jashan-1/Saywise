import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import Request, Response

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Only these extras are emitted. Anything else passed via `extra=` is dropped, so request bodies,
# context, phone numbers and AI text cannot leak into logs by accident.
EXTRA_FIELDS = (
    "agency_id",
    "location_id",
    "decision_id",
    "latency_ms",
    "method",
    "path",
    "status",
    "channel",
    "error",  # an exception's type name only: provider messages can contain phone numbers
)

log = logging.getLogger("sayso")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
        }
        data.update({k: getattr(record, k) for k in EXTRA_FIELDS if hasattr(record, k)})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # request_context logs requests


def redact(path: str) -> str:
    """Approval links carry a bearer token in the path; never log it."""
    return "/v1/approve/{token}" if path.startswith("/v1/approve/") else path


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": redact(request.url.path),
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response
    finally:
        request_id.reset(token)
