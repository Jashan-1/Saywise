from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException

from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import log, request_context, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def problem(
    status: int, detail: Any, headers: Mapping[str, str] | None = None, **extra: Any
) -> JSONResponse:
    """RFC 9457 problem details."""
    body = {"type": "about:blank", "title": HTTPStatus(status).phrase, "status": status}
    body.update(detail=detail, **extra)
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


def create_app() -> FastAPI:
    setup_logging(get_settings().log_level)
    app = FastAPI(title="SaySo", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(request_context)
    app.include_router(v1_router)

    @app.exception_handler(HTTPException)
    async def http_problem(request: Request, exc: HTTPException) -> JSONResponse:
        return problem(exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "Request validation failed", errors=jsonable_encoder(exc.errors()))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception:
            log.exception("readyz: database unreachable")
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
