from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.errors import DifyError, DifyNotConfiguredError
from app.events import parse_citations
from app.log import log_dify_call, sanitize_error_detail

logger = logging.getLogger("chat.dify")

DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


class DifyUpstreamError(Exception):
    """Raised when Dify returns a non-2xx HTTP response."""

    def __init__(
        self,
        *,
        status_code: int,
        dify_code: str | None = None,
        dify_message: str | None = None,
        raw_body: str = "",
    ) -> None:
        super().__init__(dify_message or dify_code or f"http_{status_code}")
        self.status_code = status_code
        self.dify_code = dify_code
        self.dify_message = dify_message
        self.raw_body = raw_body

    def is_conversation_not_found(self) -> bool:
        if self.status_code != 404:
            return False
        message = (self.dify_message or "").strip().lower()
        return "conversation" in message


class DifyStreamError(Exception):
    """Raised when an established Dify SSE stream fails mid-transfer."""

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _parse_dify_error_body(raw_body: str) -> tuple[str | None, str | None]:
    if not raw_body.strip():
        return None, None
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    raw_code = parsed.get("code")
    raw_msg = parsed.get("message")
    dify_code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else None
    dify_message = raw_msg.strip() if isinstance(raw_msg, str) and raw_msg.strip() else None
    return dify_code, dify_message


class DifyOpenedStream:
    def __init__(
        self,
        response: httpx.Response,
        *,
        start_time: float,
        deadline: float | None = None,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        self._response = response
        self._start_time = start_time
        self._deadline = deadline
        self._read_timeout_seconds = read_timeout_seconds
        self._closed = False

    @property
    def status_code(self) -> int:
        return self._response.status_code

    def elapsed_ms(self) -> int:
        return int(round((time.monotonic() - self._start_time) * 1000))

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        line_iter = self._response.aiter_lines()
        try:
            while True:
                if self._deadline is not None:
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        await self.aclose()
                        raise DifyStreamError("timeout")
                    wait_timeout = min(self._read_timeout_seconds, remaining)
                else:
                    wait_timeout = self._read_timeout_seconds

                try:
                    raw_line = await asyncio.wait_for(
                        line_iter.__anext__(),
                        timeout=wait_timeout,
                    )
                except StopAsyncIteration:
                    break
                except (TimeoutError, httpx.TimeoutException) as exc:
                    await self.aclose()
                    raise DifyStreamError("timeout") from exc

                if self._deadline is not None and time.monotonic() >= self._deadline:
                    await self.aclose()
                    raise DifyStreamError("timeout")

                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload_str = line[5:].strip()
                if not payload_str or payload_str in ("[DONE]", "ping"):
                    continue
                try:
                    event_obj = json.loads(payload_str)
                except json.JSONDecodeError as exc:
                    raise DifyStreamError("invalid_sse_json") from exc
                if not isinstance(event_obj, dict):
                    raise DifyStreamError("invalid_sse_event_type")
                yield event_obj
        except httpx.HTTPError as exc:
            raise DifyStreamError(type(exc).__name__) from exc

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._response.aclose()


def _format_created_at(value: Any, tz: ZoneInfo) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC).astimezone(tz).isoformat(timespec="seconds")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class DifyClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        tz_name: str = "Asia/Shanghai",
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._timeout_seconds = max(0.01, float(timeout_seconds))
        self._read_timeout_seconds = min(float(read_timeout_seconds), self._timeout_seconds)
        self._tz = ZoneInfo(tz_name)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self._timeout_seconds,
                connect=min(DEFAULT_CONNECT_TIMEOUT_SECONDS, self._timeout_seconds),
                read=self._read_timeout_seconds,
            ),
            transport=transport,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _require_configured(self) -> None:
        if not self._api_key:
            raise DifyNotConfiguredError()

    def _auth_headers(self, request_id: str, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": accept,
            "X-Request-Id": request_id,
        }

    async def open_chat_stream(
        self,
        *,
        query: str,
        user: str,
        user_token: str,
        conversation_id: str | None,
        request_id: str,
        deadline: float | None = None,
    ) -> DifyOpenedStream:
        self._require_configured()
        start = time.monotonic()
        effective_deadline = deadline if deadline is not None else start + self._timeout_seconds
        remaining = effective_deadline - start
        if remaining <= 0:
            log_dify_call(
                logger,
                request_id=request_id,
                status=None,
                duration_ms=0,
                conversation_id=conversation_id,
                path="/v1/chat-messages",
                error="timeout",
            )
            raise DifyError()

        url = f"{self._base_url}/v1/chat-messages"
        body: dict[str, Any] = {
            "inputs": {"user_token": user_token},
            "query": query,
            "user": user,
            "response_mode": "streaming",
            "auto_generate_name": False,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id

        headers = self._auth_headers(request_id, accept="text/event-stream")
        headers["Content-Type"] = "application/json"

        per_read_timeout = min(self._read_timeout_seconds, remaining)
        req_timeout = httpx.Timeout(
            remaining,
            connect=min(DEFAULT_CONNECT_TIMEOUT_SECONDS, remaining),
            read=per_read_timeout,
        )

        try:
            req = self._client.build_request(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=req_timeout,
            )
            resp = await asyncio.wait_for(
                self._client.send(req, stream=True),
                timeout=remaining,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            duration_ms = int(round((time.monotonic() - start) * 1000))
            log_dify_call(
                logger,
                request_id=request_id,
                status=None,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/chat-messages",
                error="timeout",
            )
            raise DifyError() from exc
        except httpx.HTTPError as exc:
            duration_ms = int(round((time.monotonic() - start) * 1000))
            log_dify_call(
                logger,
                request_id=request_id,
                status=None,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/chat-messages",
                error=type(exc).__name__,
            )
            raise DifyError() from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            status = resp.status_code
            try:
                read_remaining = effective_deadline - time.monotonic()
                if read_remaining <= 0:
                    raise TimeoutError("deadline exceeded while reading error body")
                raw_bytes = await asyncio.wait_for(resp.aread(), timeout=read_remaining)
            except (TimeoutError, httpx.TimeoutException) as exc:
                duration_ms = int(round((time.monotonic() - start) * 1000))
                log_dify_call(
                    logger,
                    request_id=request_id,
                    status=status,
                    duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    path="/v1/chat-messages",
                    error="timeout",
                )
                raise DifyError() from exc
            except httpx.HTTPError as exc:
                duration_ms = int(round((time.monotonic() - start) * 1000))
                log_dify_call(
                    logger,
                    request_id=request_id,
                    status=status,
                    duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    path="/v1/chat-messages",
                    error=type(exc).__name__,
                )
                raise DifyError() from exc
            finally:
                await resp.aclose()

            raw_body = raw_bytes.decode("utf-8", errors="replace")
            dify_code, dify_message = _parse_dify_error_body(raw_body)
            duration_ms = int(round((time.monotonic() - start) * 1000))
            log_dify_call(
                logger,
                request_id=request_id,
                status=status,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/chat-messages",
                error=dify_code or f"http_{status}",
                error_detail=sanitize_error_detail(raw_body),
            )
            raise DifyUpstreamError(
                status_code=status,
                dify_code=dify_code,
                dify_message=dify_message,
                raw_body=raw_body,
            )

        return DifyOpenedStream(
            resp,
            start_time=start,
            deadline=effective_deadline,
            read_timeout_seconds=self._read_timeout_seconds,
        )

    async def get_conversation_messages(
        self,
        *,
        conversation_id: str,
        user: str,
        request_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._require_configured()
        start = time.monotonic()
        url = f"{self._base_url}/v1/messages"
        params = {
            "conversation_id": conversation_id,
            "user": user,
            "limit": limit,
        }
        headers = self._auth_headers(request_id, accept="application/json")

        try:
            resp = await asyncio.wait_for(
                self._client.get(url, params=params, headers=headers),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            duration_ms = int(round((time.monotonic() - start) * 1000))
            log_dify_call(
                logger,
                request_id=request_id,
                status=None,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/messages",
                error="timeout",
            )
            raise DifyError() from exc
        except httpx.HTTPError as exc:
            duration_ms = int(round((time.monotonic() - start) * 1000))
            log_dify_call(
                logger,
                request_id=request_id,
                status=None,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/messages",
                error=type(exc).__name__,
            )
            raise DifyError() from exc

        duration_ms = int(round((time.monotonic() - start) * 1000))

        if resp.status_code < 200 or resp.status_code >= 300:
            raw_body = resp.text
            dify_code, dify_message = _parse_dify_error_body(raw_body)
            error_label = dify_code or (
                "not_found" if resp.status_code == 404 else f"http_{resp.status_code}"
            )
            log_dify_call(
                logger,
                request_id=request_id,
                status=resp.status_code,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/messages",
                error=error_label,
                error_detail=sanitize_error_detail(raw_body),
            )
            raise DifyUpstreamError(
                status_code=resp.status_code,
                dify_code=dify_code,
                dify_message=dify_message,
                raw_body=raw_body,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            log_dify_call(
                logger,
                request_id=request_id,
                status=resp.status_code,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/messages",
                error="invalid_json",
                error_detail=sanitize_error_detail(resp.text),
            )
            raise DifyError() from exc

        raw_items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            log_dify_call(
                logger,
                request_id=request_id,
                status=resp.status_code,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                path="/v1/messages",
                error="unexpected_payload",
            )
            raise DifyError()

        log_dify_call(
            logger,
            request_id=request_id,
            status=resp.status_code,
            duration_ms=duration_ms,
            conversation_id=conversation_id,
            path="/v1/messages",
        )

        messages: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            msg: dict[str, Any] = {}
            raw_id = item.get("id")
            if isinstance(raw_id, str) and raw_id.strip():
                msg["id"] = raw_id.strip()
            msg["query"] = item.get("query") if isinstance(item.get("query"), str) else ""
            msg["answer"] = item.get("answer") if isinstance(item.get("answer"), str) else ""
            created_at = _format_created_at(item.get("created_at"), self._tz)
            if created_at is not None:
                msg["created_at"] = created_at
            msg["citations"] = parse_citations(item.get("retriever_resources"))
            messages.append(msg)

        return messages

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
