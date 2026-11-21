from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.errors import UpstreamUnavailableError
from app.json_utils import loads_strict_json
from app.log import log_upstream_call
from app.schemas import is_invalid_path_segment

logger = logging.getLogger("guard.upstream")

WHITELISTED_REQUEST_HEADERS: tuple[str, ...] = (
    "Authorization",
    "Accept",
    "Content-Type",
    "X-Request-Id",
)
WHITELISTED_RESPONSE_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "X-Request-Id",
    "Location",
    "WWW-Authenticate",
)


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]
    duration_ms: int

    def json_dict(self) -> dict[str, Any] | None:
        try:
            parsed = loads_strict_json(self.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def extract_error(self) -> tuple[str | None, str | None]:
        data = self.json_dict()
        if data is None:
            return None, None
        raw_code = data.get("code")
        raw_msg = data.get("message")
        code = str(raw_code) if isinstance(raw_code, str) and raw_code else None
        msg = str(raw_msg) if isinstance(raw_msg, str) and raw_msg else None
        return code, msg

    def filtered_response_headers(self, request_id: str) -> dict[str, str]:
        lower_map = {k.lower(): v for k, v in self.headers.items()}
        out: dict[str, str] = {}
        for name in WHITELISTED_RESPONSE_HEADERS:
            val = lower_map.get(name.lower())
            if val is not None:
                out[name] = val
        out["X-Request-Id"] = request_id
        return out


def encode_path_segment(value: str) -> str:
    return quote(value, safe="")


def validate_path_params(path_params: Mapping[str, str]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for field, val in path_params.items():
        if is_invalid_path_segment(val):
            details.append(
                {
                    "field": field,
                    "message": "路径参数不能为 '.'、'..' 或包含控制字符",
                }
            )
    return details


def build_whitelisted_request_headers(
    inbound_headers: Mapping[str, str],
    request_id: str,
    *,
    authorization: str,
) -> dict[str, str]:
    lower_map = {k.lower(): v for k, v in inbound_headers.items()}
    headers: dict[str, str] = {"Authorization": authorization}
    for name in ("Accept", "Content-Type"):
        val = lower_map.get(name.lower())
        if val is not None:
            headers[name] = val
    headers["X-Request-Id"] = request_id
    return headers


class UpstreamClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _send(
        self,
        *,
        method: str,
        url: str,
        log_path: str,
        headers: dict[str, str],
        content: bytes | None = None,
        request_id: str,
        purpose: str,
    ) -> tuple[UpstreamResponse | None, int]:
        start = time.perf_counter()
        try:
            req = httpx.Request(method=method, url=url, headers=headers, content=content)
            resp = self._client.send(req)
        except (httpx.HTTPError, httpx.InvalidURL):
            duration_ms = int(round((time.perf_counter() - start) * 1000))
            log_upstream_call(
                logger,
                request_id=request_id,
                method=method,
                path=log_path,
                status=None,
                duration_ms=duration_ms,
                purpose=purpose,
            )
            return None, duration_ms

        duration_ms = int(round((time.perf_counter() - start) * 1000))
        log_upstream_call(
            logger,
            request_id=request_id,
            method=method,
            path=log_path,
            status=resp.status_code,
            duration_ms=duration_ms,
            purpose=purpose,
        )
        return (
            UpstreamResponse(
                status_code=resp.status_code,
                content=resp.content,
                headers=dict(resp.headers.items()),
                duration_ms=duration_ms,
            ),
            duration_ms,
        )

    def forward_read(
        self,
        *,
        method: str,
        path: str,
        query: str,
        inbound_headers: Mapping[str, str],
        authorization: str,
        request_id: str,
        purpose: str = "proxy",
    ) -> UpstreamResponse:
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = build_whitelisted_request_headers(
            inbound_headers,
            request_id,
            authorization=authorization,
        )
        resp, _ = self._send(
            method=method.upper(),
            url=url,
            log_path=path,
            headers=headers,
            request_id=request_id,
            purpose=purpose,
        )
        if resp is None:
            raise UpstreamUnavailableError()
        return resp

    def precheck_order(
        self,
        *,
        order_id: str,
        authorization: str,
        request_id: str,
    ) -> UpstreamResponse:
        path = f"/api/v1/orders/{encode_path_segment(order_id)}"
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
            "X-Request-Id": request_id,
        }
        resp, _ = self._send(
            method="GET",
            url=f"{self._base_url}{path}",
            log_path=path,
            headers=headers,
            request_id=request_id,
            purpose="precheck",
        )
        if resp is None:
            raise UpstreamUnavailableError()
        return resp

    def execute_refund(
        self,
        *,
        payload: dict[str, Any],
        authorization: str,
        request_id: str,
    ) -> tuple[UpstreamResponse | None, int]:
        path = "/api/v1/refunds"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        }
        return self._send(
            method="POST",
            url=f"{self._base_url}{path}",
            log_path=path,
            headers=headers,
            content=body,
            request_id=request_id,
            purpose="execute",
        )

    def dev_login(
        self,
        *,
        login_url: str,
        username: str,
        password: str,
        request_id: str,
    ) -> UpstreamResponse:
        body = json.dumps({"username": username, "password": password}).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        }
        resp, _ = self._send(
            method="POST",
            url=login_url,
            log_path="/token",
            headers=headers,
            content=body,
            request_id=request_id,
            purpose="login",
        )
        if resp is None:
            raise UpstreamUnavailableError("身份认证服务暂不可用，请稍后重试。")
        return resp
