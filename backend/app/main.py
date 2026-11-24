"""App factory: request-id + structlog, CSRF guard, routers, optional SPA mount, /healthz."""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import auth, documents, query
from app.config import Settings, get_settings
from app.db import Database
from app.errors import AppError, NotFoundError, install_error_handlers
from app.security import CSRFMiddleware
from app.seed import run_seed
from app.services.agent_graph import GraphDeps, build_graph
from app.services.llm_provider import LLMProvider, create_provider

log = structlog.get_logger()
MIN_PGVECTOR = (0, 8, 0)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=False,
    )


class RequestIdMiddleware:
    """Pure ASGI: assigns req_<id>, binds it to every log line, logs one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started, status = time.monotonic(), 0

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            ms = int((time.monotonic() - started) * 1000)
            log.info("request", method=scope["method"], path=scope["path"], status=status, ms=ms)


def _pgvector_ok(version: str) -> bool:
    parts = tuple(int(p) for p in version.split(".")[:3] if p.isdigit())
    return parts >= MIN_PGVECTOR


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database.create(cfg)
        llm = provider or create_provider(cfg)
        app.state.settings, app.state.db, app.state.provider = cfg, db, llm
        app.state.graph = build_graph(GraphDeps(provider=llm, db=db, settings=cfg))
        if cfg.run_seed_on_startup:
            try:
                await run_seed(db, llm, cfg)
            except (AppError, SQLAlchemyError) as exc:  # a seed failure must not take the app down
                log.error("seed_failed", error=repr(exc))
        yield
        await db.dispose()

    app = FastAPI(title="DocSQL", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CSRFMiddleware, allowed_origins=cfg.origins)
    app.add_middleware(RequestIdMiddleware)  # outermost: request id exists before CSRF rejects
    install_error_handlers(app)
    for router in (auth.router, documents.router, query.router):
        app.include_router(router, prefix="/api/v1")

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        db: Database = app.state.db
        checks: dict[str, str] = {}
        try:
            async with db.primary.connect() as conn:
                await conn.execute(text("SELECT 1"))
                version = (
                    await conn.execute(
                        text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                    )
                ).scalar_one_or_none()
            checks["primary"] = "ok"
            checks["pgvector"] = version if version and _pgvector_ok(version) else f"bad:{version}"
            async with db.readonly.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["readonly"] = "ok"
        except (SQLAlchemyError, OSError) as exc:
            checks.setdefault("error", type(exc).__name__)
        healthy = (
            checks.get("primary") == "ok"
            and checks.get("readonly") == "ok"
            and not checks.get("pgvector", "bad").startswith("bad")
        )
        return JSONResponse(
            {"status": "ok" if healthy else "unhealthy", "checks": checks},
            status_code=200 if healthy else 503,
        )

    _mount_frontend(app, cfg)
    return app


def _mount_frontend(app: FastAPI, cfg: Settings) -> None:
    dist = Path(cfg.frontend_dist_dir) if cfg.frontend_dist_dir else None
    if dist is None or not (dist / "index.html").is_file():
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith(("api/", "healthz", "livez")):
            raise NotFoundError("Not found")  # unknown API paths stay JSON 404s
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


app = create_app()
