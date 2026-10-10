"""FastAPI application: ``uvicorn acvm.api.app:app``."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from acvm import __version__
from acvm.api import account, ops, public, studio, v1
from acvm.api.gateway import build_gateway
from acvm.config import Settings, get_settings
from acvm.context import AppContext, build_context
from acvm.core.errors import ACVMError, BudgetExceeded, Conflict, IntegrityViolation, NotFound, PolicyViolation
from acvm.core.logging import configure_logging, get_logger
from acvm.orchestration.telemetry import setup_tracing

log = get_logger(__name__)
_STATUS = {NotFound: 404, PolicyViolation: 403, Conflict: 409, BudgetExceeded: 402, IntegrityViolation: 409}


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(json=settings.env != "dev")
        setup_tracing(settings, service="acvm-api")
        context = ctx or await build_context(settings, create_schema=settings.database_url.startswith("sqlite"))
        if "platform_clients" not in context.extras:
            try:
                from acvm.infra.platforms.registry import build_platform_clients

                context.extras["platform_clients"] = build_platform_clients(context)
            except ImportError:
                log.warning("platform_clients_unavailable")
        app.state.ctx = context
        app.state.gateway = build_gateway(context)
        async with context.uow() as repos:  # operator-chosen TTS engine survives restarts (Studio › 보이스)
            pref = await repos.kv.get_stale("studio:tts_provider")
        if pref and pref.get("provider") and pref["provider"] != "auto":
            context.extras["tts.provider"] = pref["provider"]
        if ctx is None:  # AI keys/pause switches entered in the console (Claude → Gemini chain)
            from acvm import integrations

            await integrations.sync_llm(context, force=True)
        await context.bus.start()
        try:
            yield
        finally:
            await app.state.gateway.close()
            if ctx is None:
                await context.close()

    app = FastAPI(title="ACVM", version=__version__, lifespan=lifespan,
                  # interactive docs only on developer machines — staging/prod expose no schema browser
                  docs_url="/api/docs" if settings.env in ("dev", "test") else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if settings.env in ("dev", "test") else None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.console_origins,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = response.headers.get("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        log.info("http", method=request.method, path=request.url.path, status=response.status_code,
                 ms=round((time.perf_counter() - started) * 1000, 1), rid=rid)
        return response

    @app.exception_handler(ACVMError)
    async def acvm_error(_: Request, exc: ACVMError) -> JSONResponse:
        code = next((c for cls, c in _STATUS.items() if isinstance(exc, cls)), 422 if not exc.retryable else 503)
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=code)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        c: AppContext = request.app.state.ctx
        try:
            async with c.db.session() as s:
                await s.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"fail: {type(exc).__name__}"
        redis = c.extras.get("redis")
        if redis is not None:
            try:
                await redis.ping()
                checks["redis"] = "ok"
            except Exception as exc:
                checks["redis"] = f"fail: {type(exc).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(v1.router)
    app.include_router(account.router)
    app.include_router(ops.router)
    app.include_router(studio.router)
    app.include_router(public.router)
    return app


app = create_app()  # uvicorn acvm.api.app:app — context is built in lifespan, not at import
