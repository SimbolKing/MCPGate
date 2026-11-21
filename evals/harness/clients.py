"""Synchronous HTTP clients and response normalizers for MCP, guard, and chat (5.3.1, 5.10.2)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import httpx

MCP_ERROR_RE = re.compile(r"^call failed, status: (\d{3}), response: (.*)$", re.DOTALL)

_MCP_STATUS_TO_RESULT: dict[int, str] = {
    401: "auth",
    403: "denied",
    404: "not_found",
    409: "conflict",
    422: "invalid",
}


def _header_get(headers: Mapping[str, str], name: str) -> str:
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return ""


def _parse_json_maybe_sse(raw_body: str, content_type: str = "") -> Any:
    text = raw_body.strip()
    if "text/event-stream" in content_type.lower() or text.startswith("data:"):
        data_lines: list[str] = []
        for line in raw_body.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            text = "\n".join(data_lines)
    return json.loads(text)


def normalize_mcp_response(
    tool: str,
    http_status: int,
    raw_body: str,
    *,
    is_write_tool: bool = False,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """Normalize an MCP `tools/call` HTTP response per 2.8.3, 2.8.4, and 5.10.2."""
    base: dict[str, Any] = {
        "tool": tool,
        "http_status": http_status,
        "result": "unavailable",
        "status_code": http_status,
        "error_code": None,
        "message": "",
        "request_id": "",
        "data": {},
        "raw_error_body_keys": None,
        "has_structured_content": False,
        "is_jsonrpc_response": False,
        "is_jsonrpc_error": False,
        "env_error": False,
    }

    if http_status == 401:
        base["result"] = "auth"
        base["message"] = raw_body.strip()
        return base

    if http_status != 200:
        base["result"] = "unavailable"
        base["message"] = raw_body.strip()
        base["env_error"] = True
        return base

    try:
        payload = _parse_json_maybe_sse(raw_body, content_type)
    except ValueError:
        base["result"] = "unavailable"
        base["message"] = raw_body.strip()
        base["env_error"] = True
        return base

    if not isinstance(payload, dict):
        base["result"] = "unavailable"
        base["env_error"] = True
        return base

    if "error" in payload and payload["error"] is not None:
        err = payload["error"]
        base["result"] = "internal"
        base["status_code"] = None
        base["error_code"] = (
            str(err.get("code")) if isinstance(err, dict) and err.get("code") is not None else None
        )
        base["message"] = str(err.get("message", "")) if isinstance(err, dict) else str(err)
        base["is_jsonrpc_response"] = False
        base["is_jsonrpc_error"] = True
        base["env_error"] = True
        return base

    rpc_result = payload.get("result")
    if not isinstance(rpc_result, dict):
        base["result"] = "unavailable"
        base["env_error"] = True
        return base

    base["is_jsonrpc_response"] = True
    has_structured = (
        "structuredContent" in rpc_result and rpc_result["structuredContent"] is not None
    )
    base["has_structured_content"] = has_structured

    if not rpc_result.get("isError"):
        structured = rpc_result.get("structuredContent")
        if not isinstance(structured, dict):
            base["result"] = "unavailable"
            base["env_error"] = True
            return base
        base["data"] = structured
        base["message"] = str(structured.get("message") or "")
        if is_write_tool and structured.get("status") == "pending":
            base["result"] = "pending"
            base["status_code"] = 202
        else:
            base["result"] = "ok"
            base["status_code"] = 200
        return base

    # isError == True
    text = ""
    content = rpc_result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text = item["text"]
                break

    match = MCP_ERROR_RE.match(text)
    if not match:
        base["result"] = "unavailable"
        base["message"] = text
        base["env_error"] = True
        return base

    upstream_status = int(match.group(1))
    raw_upstream = match.group(2)
    base["status_code"] = upstream_status

    if upstream_status in _MCP_STATUS_TO_RESULT:
        base["result"] = _MCP_STATUS_TO_RESULT[upstream_status]
    elif 400 <= upstream_status < 500:
        base["result"] = "invalid"
    elif 500 <= upstream_status < 600:
        base["result"] = "unavailable"
        base["env_error"] = True
    else:
        base["result"] = "internal"
        base["env_error"] = True

    try:
        err_body = json.loads(raw_upstream)
    except ValueError:
        err_body = None

    if isinstance(err_body, dict):
        base["error_code"] = err_body.get("code")
        base["message"] = str(err_body.get("message") or "")
        base["request_id"] = str(err_body.get("request_id") or "")
        base["raw_error_body_keys"] = sorted(str(k) for k in err_body.keys())
    else:
        base["message"] = raw_upstream.strip()

    return base


def normalize_guard_response(
    http_status: int,
    raw_body: str,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    """Normalize a guard console API response per 3.8.3 and 5.10.2."""
    req_id = _header_get(headers, "X-Request-Id")
    try:
        parsed = json.loads(raw_body) if raw_body.strip() else {}
    except ValueError:
        parsed = None

    data: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
    error_code: str | None = None
    message = ""
    raw_error_keys: list[str] | None = None

    if 200 <= http_status < 300:
        message = str(data.get("message") or "")
    else:
        if isinstance(parsed, dict):
            error_code = parsed.get("code")
            message = str(parsed.get("message") or "")
            if not req_id and parsed.get("request_id"):
                req_id = str(parsed["request_id"])
            raw_error_keys = sorted(str(k) for k in parsed.keys())
        else:
            message = raw_body.strip()

    return {
        "http_status": http_status,
        "error_code": error_code,
        "message": message,
        "request_id": req_id,
        "data": data,
        "raw_error_body_keys": raw_error_keys,
        "env_error": http_status >= 500,
    }


def normalize_chat_response(
    http_status: int,
    raw_body: str,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    """Normalize a `POST /chat/api/messages` (`stream: false`) response per 4.16 and 5.10.2."""
    req_id = _header_get(headers, "X-Request-Id")
    try:
        parsed = json.loads(raw_body) if raw_body.strip() else None
    except ValueError:
        parsed = None

    if http_status == 200 and isinstance(parsed, dict):
        trace = parsed.get("trace")
        trace_list = trace if isinstance(trace, list) else []
        citations = parsed.get("citations")
        citations_list = citations if isinstance(citations, list) else []
        has_trace_env_err = any(
            isinstance(item, dict) and item.get("kind") in ("unavailable", "internal")
            for item in trace_list
        )
        return {
            "http_status": 200,
            "error_code": None,
            "message": "",
            "request_id": req_id,
            "conversation_id": str(parsed.get("conversation_id") or ""),
            "message_id": str(parsed.get("message_id") or ""),
            "answer": str(parsed.get("answer") or ""),
            "citations": citations_list,
            "trace": trace_list,
            "env_error": has_trace_env_err,
        }

    error_code: str | None = None
    message = raw_body.strip()
    if isinstance(parsed, dict):
        error_code = parsed.get("code")
        message = str(parsed.get("message") or "")
        if not req_id and parsed.get("request_id"):
            req_id = str(parsed["request_id"])

    return {
        "http_status": http_status,
        "error_code": error_code,
        "message": message,
        "request_id": req_id,
        "conversation_id": "",
        "message_id": "",
        "answer": "",
        "citations": [],
        "trace": [],
        "env_error": http_status >= 500 or http_status == 200,
    }


class McpClient:
    """Stateless JSON-RPC `tools/call` client for Higress MCP endpoint (2.11.3, 5.3.1)."""

    def __init__(
        self,
        mcp_url: str,
        *,
        write_tools: set[str] | frozenset[str] = frozenset({"create_refund"}),
        timeout: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._url = mcp_url
        self._write_tools = set(write_tools)
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)
        self._rpc_id = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def call_tool(
        self,
        tool: str,
        args: dict[str, Any],
        token: str | None,
    ) -> dict[str, Any]:
        self._rpc_id += 1
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        try:
            resp = self._client.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return {
                "tool": tool,
                "http_status": 0,
                "result": "unavailable",
                "status_code": 0,
                "error_code": None,
                "message": str(exc),
                "request_id": "",
                "data": {},
                "raw_error_body_keys": None,
                "has_structured_content": False,
                "is_jsonrpc_response": False,
                "is_jsonrpc_error": False,
                "env_error": True,
            }

        return normalize_mcp_response(
            tool,
            resp.status_code,
            resp.text,
            is_write_tool=tool in self._write_tools,
            content_type=resp.headers.get("content-type", ""),
        )


class GuardClient:
    """HTTP client for guard console endpoints (3.8.3, 5.3.1)."""

    def __init__(
        self,
        guard_url: str,
        *,
        timeout: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base = guard_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def healthz(self) -> int:
        try:
            resp = self._client.get(f"{self._base}/healthz")
            return resp.status_code
        except httpx.HTTPError:
            return 0

    def _headers(self, token: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def call(
        self,
        action: str,
        token: str | None,
        *,
        approval_id: str | None = None,
        comment: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers(token)
        try:
            if action == "approve":
                body: dict[str, Any] = {} if comment is None else {"comment": comment}
                resp = self._client.post(
                    f"{self._base}/guard/v1/console/approvals/{approval_id}/approve",
                    json=body,
                    headers=headers,
                )
            elif action == "reject":
                body = {} if comment is None else {"comment": comment}
                resp = self._client.post(
                    f"{self._base}/guard/v1/console/approvals/{approval_id}/reject",
                    json=body,
                    headers=headers,
                )
            elif action == "order":
                resp = self._client.get(
                    f"{self._base}/guard/v1/console/approvals/{approval_id}/order",
                    headers=headers,
                )
            elif action == "detail":
                resp = self._client.get(
                    f"{self._base}/guard/v1/console/approvals/{approval_id}",
                    headers=headers,
                )
            elif action == "audit_logs":
                resp = self._client.get(
                    f"{self._base}/guard/v1/console/audit-logs",
                    params=params or {},
                    headers=headers,
                )
            else:
                raise ValueError(f"不支持的 guard 接口操作: {action}")
        except httpx.HTTPError as exc:
            return {
                "http_status": 0,
                "error_code": None,
                "message": str(exc),
                "request_id": "",
                "data": {},
                "raw_error_body_keys": None,
                "env_error": True,
            }
        return normalize_guard_response(resp.status_code, resp.text, resp.headers)

    def fetch_audit_since(
        self,
        cursor_id: int,
        token: str,
        *,
        page_size: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch all audit events with `id > cursor_id` by paging from page 1 (5.6.2)."""
        collected: dict[int, dict[str, Any]] = {}
        page = 1
        while True:
            res = self.call(
                "audit_logs",
                token,
                params={"page": page, "page_size": page_size},
            )
            if res["http_status"] != 200:
                raise RuntimeError(
                    f"拉取审计记录失败: status={res['http_status']} code={res['error_code']}"
                )
            items = res["data"].get("items") or []
            if not items:
                break
            hit_cursor = False
            for item in items:
                ev_id = int(item["id"])
                if ev_id <= cursor_id:
                    hit_cursor = True
                    continue
                collected[ev_id] = item
            if hit_cursor or len(items) < page_size:
                break
            page += 1

        ordered = [collected[k] for k in sorted(collected)]
        new_cursor = max(cursor_id, ordered[-1]["id"]) if ordered else cursor_id
        return ordered, new_cursor

    def latest_audit_id(self, token: str) -> int:
        res = self.call("audit_logs", token, params={"page": 1, "page_size": 1})
        if res["http_status"] != 200:
            raise RuntimeError(f"获取最新审计游标失败: status={res['http_status']}")
        items = res["data"].get("items") or []
        return int(items[0]["id"]) if items else 0


class ChatClient:
    """HTTP client for `chat` non-streaming message API (4.16, 5.3.1)."""

    def __init__(
        self,
        chat_url: str,
        *,
        timeout: float = 150.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base = chat_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def healthz(self) -> int:
        try:
            resp = self._client.get(f"{self._base}/healthz")
            return resp.status_code
        except httpx.HTTPError:
            return 0

    def send_message(
        self,
        query: str,
        token: str | None,
        *,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        payload: dict[str, Any] = {"query": query, "stream": False}
        if conversation_id:
            payload["conversation_id"] = conversation_id

        try:
            resp = self._client.post(
                f"{self._base}/chat/api/messages",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return {
                "http_status": 0,
                "error_code": None,
                "message": str(exc),
                "request_id": "",
                "conversation_id": "",
                "message_id": "",
                "answer": "",
                "citations": [],
                "trace": [],
                "env_error": True,
            }
        return normalize_chat_response(resp.status_code, resp.text, resp.headers)
