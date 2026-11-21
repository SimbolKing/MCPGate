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
    root = logging.getLogger("mock_api")
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
    code: str,
    reason: str,
) -> None:
    logger.warning(
        "auth_failed",
        extra={
            "fields": {
                "event": "auth_failed",
                "request_id": request_id,
                "error_code": code,
                "reason": reason,
            }
        },
    )


def log_access_denied(
    logger: logging.Logger,
    *,
    request_id: str,
    sub: str,
    role: str,
    regions: frozenset[str],
    resource_type: str,
    resource_id: str | None,
    reason: str,
) -> None:
    logger.warning(
        "access_denied",
        extra={
            "fields": {
                "event": "access_denied",
                "request_id": request_id,
                "sub": sub,
                "role": role,
                "regions": sorted(regions),
                "resource_type": resource_type,
                "resource_id": resource_id,
                "reason": reason,
            }
        },
    )


def log_refund_created(
    logger: logging.Logger,
    *,
    request_id: str,
    refund_id: str,
    order_id: str,
    amount: float,
    created_by: str,
    created_by_role: str,
) -> None:
    logger.info(
        "refund_created",
        extra={
            "fields": {
                "event": "refund_created",
                "request_id": request_id,
                "refund_id": refund_id,
                "order_id": order_id,
                "amount": amount,
                "created_by": created_by,
                "created_by_role": created_by_role,
            }
        },
    )


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger("mock_api.http")

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
        except Exception:
            status_code = 500
            state["error_code"] = "internal_error"
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            fields: dict[str, Any] = {
                "event": "http_request",
                "request_id": request_id,
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
                "status": status_code,
                "duration_ms": duration_ms,
            }
            principal = state.get("principal")
            if principal is not None:
                fields["sub"] = principal.sub
                fields["role"] = principal.role
                fields["regions"] = sorted(principal.regions)
            error_code = state.get("error_code")
            if error_code is not None and status_code >= 400:
                fields["error_code"] = error_code
            self._logger.info("http_request", extra={"fields": fields})
