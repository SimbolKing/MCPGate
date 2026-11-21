from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("mock_api.errors")


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


class AuthUnavailableError(AppError):
    def __init__(self, message: str = "身份校验服务暂不可用，请稍后重试。") -> None:
        super().__init__(503, "auth_unavailable", message)


class ForbiddenError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(403, code, message)


class AccessDenied(ForbiddenError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.resource_type = resource_type
        self.resource_id = resource_id


class NotFound(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(404, code, message)


class ConflictError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


def _get_request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return str(existing)
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    rid = incoming or str(uuid.uuid4())
    request.state.request_id = rid
    return rid


def _format_validation_field(loc: tuple[Any, ...]) -> str:
    parts = [str(item) for item in loc]
    if len(parts) > 1 and parts[0] in ("body", "query", "path", "header"):
        parts = parts[1:]
    return ".".join(parts) if parts else "request"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = _get_request_id(request)
        request.state.error_code = exc.code

        if isinstance(exc, AccessDenied):
            principal = getattr(request.state, "principal", None)
            extra: dict[str, Any] = {
                "event": "access_denied",
                "request_id": request_id,
                "resource_type": exc.resource_type,
                "resource_id": exc.resource_id,
                "reason": exc.code,
            }
            if principal is not None:
                extra["sub"] = principal.sub
                extra["role"] = principal.role
                extra["regions"] = sorted(principal.regions)
            logger.warning("access_denied", extra={"fields": extra})

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
        request_id = _get_request_id(request)
        request.state.error_code = "validation_error"
        details = [
            {
                "field": _format_validation_field(tuple(err.get("loc", ()))),
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
        request_id = _get_request_id(request)
        if exc.status_code == 400 and exc.detail == "There was an error parsing the body":
            request.state.error_code = "validation_error"
            return JSONResponse(
                status_code=422,
                content={
                    "code": "validation_error",
                    "message": "请求参数不合法，请检查输入字段。",
                    "request_id": request_id,
                    "details": [{"field": "body", "message": "请求体无法解析"}],
                },
                headers={"X-Request-Id": request_id},
            )
        if exc.status_code == 404:
            code = "not_found"
            message = "请求的接口不存在。"
        elif exc.status_code == 405:
            code = "method_not_allowed"
            message = "当前接口不支持该请求方法。"
        else:
            code = f"http_{exc.status_code}"
            message = "请求处理失败。"
        request.state.error_code = code
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
        request_id = _get_request_id(request)
        request.state.error_code = "internal_error"
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
