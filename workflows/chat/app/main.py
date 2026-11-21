from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import (
    JwksKeyResolver,
    Principal,
    SigningKeyResolver,
    get_authenticated_user,
)
from app.config import PROJECT_ROOT, Settings
from app.dify import DifyClient, DifyStreamError, DifyUpstreamError
from app.errors import (
    AuthUnavailableError,
    DifyError,
    LoginFailedError,
    NotFoundError,
    ValidationAppError,
    get_request_id,
    register_exception_handlers,
)
from app.events import DIFY_ERROR_EVENT_PAYLOAD, DifyStreamProcessor, format_sse
from app.log import (
    RequestLoggingMiddleware,
    configure_logging,
    log_conversation_reset,
    log_dify_call,
)

WEB_DIR = PROJECT_ROOT / "web"

dify_logger = logging.getLogger("chat.dify")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_uuid_str(value: str) -> str:
    if not _UUID_RE.match(value):
        raise ValueError("conversation_id 必须为合法的 UUID 格式。")
    return str(uuid.UUID(value))


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("用户名不能为空。")
        return stripped


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    conversation_id: str | None = Field(default=None, max_length=128)
    stream: bool = True

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("消息内容不能为空。")
        if len(stripped) > 2000:
            raise ValueError("消息内容不能超过 2000 字。")
        return stripped

    @field_validator("conversation_id")
    @classmethod
    def _normalize_conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return _validate_uuid_str(stripped)


def create_app(
    settings: Settings | None = None,
    signing_key_resolver: SigningKeyResolver | None = None,
    dify_client: DifyClient | None = None,
    login_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    configure_logging(level=cfg.log_level, tz_name=cfg.timezone)

    owns_dify = dify_client is None
    active_dify = dify_client or DifyClient(
        base_url=cfg.dify_base_url,
        api_key=cfg.dify_api_key,
        timeout_seconds=cfg.dify_timeout_seconds,
        tz_name=cfg.timezone,
    )

    owns_login = login_client is None
    active_login = login_client or httpx.AsyncClient(timeout=10.0)

    active_resolver = signing_key_resolver or JwksKeyResolver(
        jwks_url=cfg.jwt_jwks_url,
        cache_seconds=cfg.jwks_cache_seconds,
        refresh_cooldown_seconds=cfg.jwks_refresh_cooldown_seconds,
        timeout_seconds=cfg.jwks_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_dify:
                await active_dify.aclose()
            if owns_login:
                await active_login.aclose()

    app = FastAPI(
        title="MCPGate Chat",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.signing_key_resolver = active_resolver
    app.state.dify_client = active_dify
    app.state.login_client = active_login

    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/chat/api/config")
    async def chat_config(request: Request) -> JSONResponse:
        request_id = get_request_id(request)
        return JSONResponse(
            status_code=200,
            content={
                "dev_login_enabled": bool(cfg.dev_login_url.strip()),
                "approval_console_url": cfg.approval_console_url,
            },
            headers={"X-Request-Id": request_id, "Cache-Control": "no-store"},
        )

    @app.post("/chat/api/login")
    async def chat_login(body: LoginRequest, request: Request) -> JSONResponse:
        request_id = get_request_id(request)
        login_url = cfg.dev_login_url.strip()
        if not login_url:
            raise NotFoundError("login_not_configured", "未配置开发登录，请使用粘贴令牌方式登录。")

        try:
            resp = await active_login.post(
                login_url,
                json={"username": body.username, "password": body.password},
                headers={"Accept": "application/json", "X-Request-Id": request_id},
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailableError("身份认证服务暂不可用，请稍后重试。") from exc

        if resp.status_code in (400, 401):
            raise LoginFailedError("用户名或密码错误。")
        if resp.status_code != 200:
            raise AuthUnavailableError("身份认证服务暂不可用，请稍后重试。")

        try:
            data = resp.json()
        except ValueError as exc:
            raise AuthUnavailableError("身份认证服务返回异常。") from exc

        if not isinstance(data, dict):
            raise AuthUnavailableError("身份认证服务返回异常。")

        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise AuthUnavailableError("身份认证服务返回异常。")

        raw_expires = data.get("expires_in", 0)
        expires_in = (
            raw_expires if isinstance(raw_expires, int) and not isinstance(raw_expires, bool) else 0
        )
        token_type = data.get("token_type")
        if not isinstance(token_type, str) or not token_type.strip():
            token_type = "Bearer"

        return JSONResponse(
            status_code=200,
            content={
                "access_token": access_token,
                "token_type": token_type,
                "expires_in": expires_in,
            },
            headers={"X-Request-Id": request_id, "Cache-Control": "no-store"},
        )

    @app.post("/chat/api/messages")
    async def send_message(
        body: MessageRequest,
        request: Request,
        auth: tuple[Principal, str] = Depends(get_authenticated_user),
    ) -> Response:
        principal, raw_token = auth
        request_id = get_request_id(request)
        deadline = time.monotonic() + cfg.dify_timeout_seconds
        effective_conversation_id = body.conversation_id

        try:
            opened = await active_dify.open_chat_stream(
                query=body.query,
                user=principal.sub,
                user_token=raw_token,
                conversation_id=effective_conversation_id,
                request_id=request_id,
                deadline=deadline,
            )
        except DifyUpstreamError as exc:
            if effective_conversation_id and exc.is_conversation_not_found():
                log_conversation_reset(
                    dify_logger,
                    request_id=request_id,
                    sub=principal.sub,
                    old_conversation_id=effective_conversation_id,
                )
                effective_conversation_id = None
                try:
                    opened = await active_dify.open_chat_stream(
                        query=body.query,
                        user=principal.sub,
                        user_token=raw_token,
                        conversation_id=None,
                        request_id=request_id,
                        deadline=deadline,
                    )
                except DifyUpstreamError as retry_exc:
                    raise DifyError() from retry_exc
            else:
                raise DifyError() from exc

        if not body.stream:
            processor = DifyStreamProcessor()
            stream_error: str | None = None
            stream_error_detail: str | None = None
            try:
                async for dify_event in opened.iter_events():
                    processor.process_event(dify_event)
                if not processor.failed and processor.done_payload is None:
                    stream_error = "incomplete_stream"
            except DifyStreamError as exc:
                stream_error = exc.reason
                stream_error_detail = exc.detail
                raise DifyError() from exc
            finally:
                await opened.aclose()
                log_dify_call(
                    dify_logger,
                    request_id=request_id,
                    status=opened.status_code,
                    duration_ms=opened.elapsed_ms(),
                    conversation_id=processor.conversation_id or effective_conversation_id,
                    path="/v1/chat-messages",
                    error=stream_error or processor.error_reason,
                    error_detail=stream_error_detail or processor.error_detail,
                )

            if processor.failed or processor.done_payload is None:
                raise DifyError()

            return JSONResponse(
                status_code=200,
                content=processor.done_payload,
                headers={"X-Request-Id": request_id},
            )

        async def event_stream() -> AsyncIterator[str]:
            processor = DifyStreamProcessor()
            stream_error: str | None = None
            stream_error_detail: str | None = None
            try:
                async for dify_event in opened.iter_events():
                    for out_event, out_data in processor.process_event(dify_event):
                        yield format_sse(out_event, out_data)
                if not processor.failed and processor.done_payload is None:
                    stream_error = "incomplete_stream"
                    yield format_sse("error", dict(DIFY_ERROR_EVENT_PAYLOAD))
            except DifyStreamError as exc:
                stream_error = exc.reason
                stream_error_detail = exc.detail
                if not processor.failed:
                    yield format_sse("error", dict(DIFY_ERROR_EVENT_PAYLOAD))
            finally:
                await opened.aclose()
                log_dify_call(
                    dify_logger,
                    request_id=request_id,
                    status=opened.status_code,
                    duration_ms=opened.elapsed_ms(),
                    conversation_id=processor.conversation_id or effective_conversation_id,
                    path="/v1/chat-messages",
                    error=stream_error or processor.error_reason,
                    error_detail=stream_error_detail or processor.error_detail,
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-Id": request_id,
            },
        )

    @app.get("/chat/api/conversations/{conversation_id}/messages")
    async def list_conversation_messages(
        conversation_id: str,
        request: Request,
        auth: tuple[Principal, str] = Depends(get_authenticated_user),
    ) -> JSONResponse:
        principal, _raw_token = auth
        request_id = get_request_id(request)
        cleaned_id = conversation_id.strip()
        if not cleaned_id:
            raise ValidationAppError(
                [{"field": "conversation_id", "message": "会话 ID 不能为空。"}]
            )
        try:
            canonical_id = _validate_uuid_str(cleaned_id)
        except ValueError as exc:
            raise ValidationAppError(
                [{"field": "conversation_id", "message": "会话 ID 必须为合法的 UUID 格式。"}]
            ) from exc

        try:
            messages: list[dict[str, Any]] = await active_dify.get_conversation_messages(
                conversation_id=canonical_id,
                user=principal.sub,
                request_id=request_id,
            )
        except DifyUpstreamError as exc:
            if exc.is_conversation_not_found():
                raise NotFoundError(
                    "conversation_not_found",
                    "会话不存在或不属于当前登录人。",
                ) from exc
            raise DifyError() from exc

        return JSONResponse(
            status_code=200,
            content={"conversation_id": canonical_id, "messages": messages},
            headers={"X-Request-Id": request_id},
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app


def get_web_dir() -> Path:
    return WEB_DIR
