from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("chat.errors")


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.details = details


class AuthError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            401,
            code,
            message,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )


class LoginFailedError(AppError):
    def __init__(self, message: str = "用户名或密码错误。") -> None:
        super().__init__(401, "login_failed", message)


class AuthUnavailableError(AppError):
    def __init__(self, message: str = "身份校验服务暂不可用，请稍后重试。") -> None:
        super().__init__(503, "auth_unavailable", message)


class ForbiddenError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(403, code, message)


class NotFoundError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(404, code, message)


class ValidationAppError(AppError):
    def __init__(
        self,
        details: list[dict[str, str]],
        message: str = "请求参数不合法，请检查输入字段。",
    ) -> None:
        super().__init__(422, "validation_error", message, details=details)


class DifyError(AppError):
    def __init__(self, message: str = "助手暂时不可用，请稍后再试。") -> None:
        super().__init__(502, "dify_error", message)


class DifyNotConfiguredError(AppError):
    def __init__(self, message: str = "对话服务尚未配置应用密钥，请联系管理员。") -> None:
        super().__init__(503, "dify_not_configured", message)


class InternalError(AppError):
    def __init__(self, message: str = "服务内部错误，请稍后重试。") -> None:
        super().__init__(500, "internal_error", message)


def get_request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return str(existing)
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    rid = incoming or str(uuid.uuid4())
    request.state.request_id = rid
    return rid


def format_validation_field(loc: tuple[Any, ...]) -> str:
    parts = [str(item) for item in loc]
    if len(parts) > 1 and parts[0] in ("body", "query", "path", "header"):
        parts = parts[1:]
    return ".".join(parts) if parts else "request"


def validation_details_from_pydantic(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": format_validation_field(tuple(err.get("loc", ()))),
            "message": str(err.get("msg", "参数校验失败")),
        }
        for err in exc.errors()
    ]


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = get_request_id(request)
        body: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
        }
        if exc.details is not None:
            body["details"] = exc.details
        headers = {"X-Request-Id": request_id}
        if exc.headers:
            headers.update(exc.headers)
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = get_request_id(request)
        details = [
            {
                "field": format_validation_field(tuple(err.get("loc", ()))),
                "message": str(err.get("msg", "参数校验失败")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "请求参数不合法，请检查输入字段。",
                "request_id": request_id,
                "details": details,
            },
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = get_request_id(request)
        if exc.status_code == 404:
            code = "route_not_found"
            message = "请求的接口不存在或未对外开放。"
        elif exc.status_code == 405:
            code = "method_not_allowed"
            message = "当前接口不支持该请求方法。"
        else:
            code = f"http_{exc.status_code}"
            message = "请求处理失败。"
        headers = {"X-Request-Id": request_id}
        if exc.headers:
            headers.update(exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": code,
                "message": message,
                "request_id": request_id,
            },
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = get_request_id(request)
        logger.error(
            "internal_error",
            extra={
                "fields": {
                    "event": "internal_error",
                    "request_id": request_id,
                    "error_type": type(exc).__name__,
                }
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "服务内部错误，请稍后重试。",
                "request_id": request_id,
            },
            headers={"X-Request-Id": request_id},
        )
