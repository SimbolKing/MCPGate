from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.auth import JwksKeyResolver, SigningKeyResolver
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.errors import register_exception_handlers
from app.log import RequestLoggingMiddleware, configure_logging
from app.openapi import API_TITLE, API_VERSION, build_openapi
from app.routers import orders, refunds
from app.seed import seed_database


def create_app(
    settings: Settings | None = None,
    *,
    init_database: bool = True,
    clock: Clock | None = None,
    signing_key_resolver: SigningKeyResolver | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    configure_logging(level=cfg.log_level, tz_name=cfg.timezone)

    engine = create_db_engine(cfg.database_url) if init_database else None
    session_factory = create_session_factory(engine) if engine is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if engine is not None:
            seed_database(
                engine,
                cfg.resolve_seed_path(),
                reset=cfg.reset_on_startup,
                now=clock() if clock is not None else None,
                tz_name=cfg.timezone,
            )
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.signing_key_resolver = signing_key_resolver or JwksKeyResolver(
        jwks_url=cfg.jwt_jwks_url,
        cache_seconds=cfg.jwks_cache_seconds,
        refresh_cooldown_seconds=cfg.jwks_refresh_cooldown_seconds,
        timeout_seconds=cfg.jwks_timeout_seconds,
    )

    if clock is not None:
        app.dependency_overrides[get_clock] = lambda: clock

    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)

    app.include_router(orders.router)
    app.include_router(refunds.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
