from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_ERROR_DETAIL_LENGTH = 500
_JWT_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_DIFY_KEY_RE = re.compile(r"\b(?:app|dataset)-[A-Za-z0-9_-]{8,}\b")


def sanitize_error_detail(
    raw: Any,
    *,
    max_length: int = MAX_ERROR_DETAIL_LENGTH,
) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        try:
            text = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(raw)

    cleaned = text.strip()
    if not cleaned:
        return None

    redacted = _JWT_LIKE_RE.sub("[REDACTED_JWT]", cleaned)
    redacted = _DIFY_KEY_RE.sub("[REDACTED_KEY]", redacted)
    if len(redacted) > max_length:
        if max_length > 3:
            return redacted[: max_length - 3] + "..."
        return redacted[:max_length]
    return redacted


class JsonFormatter(logging.Formatter):
    def __init__(self, tz_name: str = "Asia/Shanghai") -> None:
        super().__init__()
        self._tz = ZoneInfo(tz_name)

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).astimezone(self._tz)
        payload: dict[str, Any] = {
            "ts": ts.isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if value is not None:
                    payload[key] = value
        if "event" not in payload:
            payload["event"] = record.getMessage()
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", tz_name: str = "Asia/Shanghai") -> None:
    root = logging.getLogger("chat")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(tz_name=tz_name))
    root.addHandler(handler)


def log_auth_failed(
    logger: logging.Logger,
    *,
    request_id: str,
    reason: str,
) -> None:
    logger.warning(
        "auth_failed",
        extra={
            "fields": {
                "event": "auth_failed",
                "request_id": request_id,
                "reason": reason,
            }
        },
    )


def log_dify_call(
    logger: logging.Logger,
    *,
    request_id: str,
    status: int | None,
    duration_ms: int,
    conversation_id: str | None = None,
    path: str | None = None,
    error: str | None = None,
    error_detail: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "event": "dify_call",
        "request_id": request_id,
        "status": status,
        "duration_ms": duration_ms,
    }
    if conversation_id:
        fields["conversation_id"] = conversation_id
    if path:
        fields["path"] = path
    if error:
        fields["error"] = error
    safe_detail = sanitize_error_detail(error_detail)
    if safe_detail:
        fields["error_detail"] = safe_detail
    level = logging.WARNING if (error or status is None or status >= 400) else logging.INFO
    logger.log(level, "dify_call", extra={"fields": fields})


def log_conversation_reset(
    logger: logging.Logger,
    *,
    request_id: str,
    sub: str,
    old_conversation_id: str,
) -> None:
    logger.info(
        "conversation_reset",
        extra={
            "fields": {
                "event": "conversation_reset",
                "request_id": request_id,
                "sub": sub,
                "old_conversation_id": old_conversation_id,
            }
        },
    )


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger("chat.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_rid = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        request_id = raw_rid or str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                raw_headers = list(message.get("headers", []))
                has_rid = any(k.lower() == b"x-request-id" for k, _ in raw_headers)
                if not has_rid:
                    raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                    message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = str(scope.get("path", ""))
            if path != "/healthz" and path != "/" and not path.startswith("/static"):
                duration_ms = int(round((time.perf_counter() - start) * 1000))
                fields: dict[str, Any] = {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": scope.get("method", ""),
                    "path": path,
                    "status": status_code,
                    "duration_ms": duration_ms,
                }
                principal = state.get("principal")
                if principal is not None:
                    fields["sub"] = principal.sub
                self._logger.info("http_request", extra={"fields": fields})
