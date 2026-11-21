from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import approvals, audit
from app.auth import JwksKeyResolver, SigningKeyResolver
from app.clock import Clock, utc_now
from app.config import PROJECT_ROOT, Settings
from app.db import create_db_engine, create_session_factory, init_db
from app.errors import register_exception_handlers
from app.log import RequestLoggingMiddleware, configure_logging
from app.manifest import ToolManifest
from app.routers import agent_api, console_api, proxy
from app.upstream import UpstreamClient

CONSOLE_DIR = PROJECT_ROOT / "console"


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    signing_key_resolver: SigningKeyResolver | None = None,
    upstream_transport: httpx.BaseTransport | None = None,
    manifest: ToolManifest | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    configure_logging(level=cfg.log_level, tz_name=cfg.timezone)

    resolved_manifest = manifest or ToolManifest.from_file(cfg.resolve_manifest_path())
    engine = create_db_engine(cfg.database_url)
    session_factory = create_session_factory(engine)
    active_clock = clock or utc_now
    upstream_client = UpstreamClient(
        base_url=cfg.upstream_base_url,
        timeout_seconds=cfg.upstream_timeout_seconds,
        transport=upstream_transport,
    )
    retention_state = audit.AuditRetentionState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_db(engine)
        with approvals.WRITE_LOCK, session_factory() as session:
            now_dt = active_clock()
            audit.maybe_cleanup_expired_events(
                session,
                now=now_dt,
                settings=cfg,
                retention_state=retention_state,
            )
            session.commit()
        try:
            yield
        finally:
            upstream_client.close()
            engine.dispose()

    app = FastAPI(
        title="MCPGate Guard",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = active_clock
    app.state.manifest = resolved_manifest
    app.state.upstream = upstream_client
    app.state.audit_retention_state = retention_state
    app.state.signing_key_resolver = signing_key_resolver or JwksKeyResolver(
        jwks_url=cfg.jwt_jwks_url,
        cache_seconds=cfg.jwks_cache_seconds,
        refresh_cooldown_seconds=cfg.jwks_refresh_cooldown_seconds,
        timeout_seconds=cfg.jwks_timeout_seconds,
    )

    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/console/", status_code=307)

    @app.get("/console/config.js")
    def console_config_js() -> PlainTextResponse:
        enabled = "true" if cfg.dev_login_url.strip() else "false"
        return PlainTextResponse(
            content=f"export const DEV_LOGIN_ENABLED = {enabled};\n",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(agent_api.router)
    app.include_router(console_api.router)

    if CONSOLE_DIR.exists():
        app.mount("/console", StaticFiles(directory=str(CONSOLE_DIR), html=True), name="console")

    # Proxy router must be registered last to catch all remaining paths
    app.include_router(proxy.router)

    return app


def get_console_dir() -> Path:
    return CONSOLE_DIR
