from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from starlette.types import ASGIApp, Message, Receive, Scope, Send


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
    root = logging.getLogger("guard")
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


def log_upstream_call(
    logger: logging.Logger,
    *,
    request_id: str,
    method: str,
    path: str,
    status: int | None,
    duration_ms: int,
    purpose: str,
) -> None:
    logger.info(
        "upstream_call",
        extra={
            "fields": {
                "event": "upstream_call",
                "request_id": request_id,
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                "purpose": purpose,
            }
        },
    )


def log_approval_state(
    logger: logging.Logger,
    *,
    approval_id: str,
    from_status: str,
    to_status: str,
    by: str,
) -> None:
    logger.info(
        "approval_state",
        extra={
            "fields": {
                "event": "approval_state",
                "approval_id": approval_id,
                "from": from_status,
                "to": to_status,
                "by": by,
            }
        },
    )


def log_audit_event(logger: logging.Logger, fields: dict[str, Any]) -> None:
    payload = {"event": "audit"}
    payload.update(fields)
    logger.info("audit", extra={"fields": payload})


def log_execution_result_conflict(
    logger: logging.Logger,
    *,
    approval_id: str,
    refund_id: str,
    request_id: str,
) -> None:
    logger.error(
        "execution_result_conflict",
        extra={
            "fields": {
                "event": "execution_result_conflict",
                "approval_id": approval_id,
                "refund_id": refund_id,
                "request_id": request_id,
            }
        },
    )


def log_internal_error(
    logger: logging.Logger,
    *,
    request_id: str,
    operation: str,
    error_type: str,
) -> None:
    logger.error(
        "internal_error",
        extra={
            "fields": {
                "event": "internal_error",
                "request_id": request_id,
                "operation": operation,
                "error_type": error_type,
            }
        },
    )


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger("guard.http")

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
            if path != "/healthz" and not path.startswith("/console"):
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
                    fields["role"] = principal.role
                self._logger.info("http_request", extra={"fields": fields})
