#!/usr/bin/env python3
"""End-to-end smoke verification for MCPGate gateway (V1–V34).

Uses only Python standard library so it can be run directly with `python3 gateway/scripts/smoke.py`.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_ERROR_RE = re.compile(r"^call failed, status: (\d{3}), response: (.*)$", re.DOTALL)
CHECK_ID_RE = re.compile(r"^V(\d+)$", re.IGNORECASE)


class CheckFailure(AssertionError):
    """Raised when a smoke verification assertion fails."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise CheckFailure(
                f"响应体不是合法 JSON (HTTP {self.status}): {self.body[:200]!r}"
            ) from exc


@dataclass(frozen=True)
class ToolErrorInfo:
    status: int
    raw_response: str
    error_body: dict[str, Any] | None


def parse_tool_error(text: str) -> ToolErrorInfo:
    """Parse mcp-server's default error text: 'call failed, status: <N>, response: <body>'."""
    match = TOOL_ERROR_RE.match(text)
    if not match:
        raise CheckFailure(f"错误文本不符合 mcp-server 默认格式: {text!r}")
    status = int(match.group(1))
    raw_resp = match.group(2)
    try:
        parsed = json.loads(raw_resp)
        error_body = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        error_body = None
    return ToolErrorInfo(status=status, raw_response=raw_resp, error_body=error_body)


def tamper_jwt(token: str) -> str:
    """Modify a middle character of a JWT signature so decoded bytes always change."""
    if not token:
        raise CheckFailure("待篡改的 token 为空")
    parts = token.split(".")
    if len(parts) != 3 or not parts[2]:
        raise CheckFailure("待篡改的 token 不是合法三段式 JWT")
    sig = parts[2]
    idx = 0 if len(sig) <= 2 else min(10, len(sig) // 2)
    replacement = "A" if sig[idx] != "A" else "B"
    parts[2] = sig[:idx] + replacement + sig[idx + 1 :]
    return ".".join(parts)


def _poll_until(
    predicate: Callable[[], bool],
    *,
    attempts: int,
    interval: float,
    sleep_fn: Callable[[float], None],
) -> bool:
    for attempt in range(attempts):
        if predicate():
            return True
        if attempt + 1 < attempts:
            sleep_fn(interval)
    return False


@dataclass
class SmokeContext:
    repo_root: Path
    mcp_url: str = "http://127.0.0.1:8080/aftersale/mcp"
    mock_api_url: str = "http://127.0.0.1:8000"
    guard_url: str = "http://127.0.0.1:8020"
    manifest_path: Path | None = None
    timeout: float = 15.0
    log_retry_attempts: int = 5
    log_retry_interval: float = 1.0
    sleep_fn: Callable[[float], None] = time.sleep
    _rpc_id: int = 0
    tokens: dict[tuple[str, int | None, str | None], str] = field(default_factory=dict)
    used_tokens: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.manifest_path is None:
            self.manifest_path = self.repo_root / "gateway/generated/tools.manifest.json"

    def next_rpc_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def run_cmd(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            args,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise CheckFailure(
                f"命令执行失败 ({proc.returncode}): {' '.join(args)}\nstderr: {proc.stderr.strip()}"
            )
        return proc

    def mint(
        self,
        user: str,
        *,
        ttl: int | None = None,
        audience: str | None = None,
    ) -> str:
        cache_key = (user, ttl, audience)
        if cache_key in self.tokens:
            return self.tokens[cache_key]
        cmd = ["docker", "compose", "exec", "-T"]
        if audience is not None:
            cmd.extend(["-e", f"DEVIDP_AUDIENCE={audience}"])
        cmd.extend(["dev-idp", "python", "-m", "dev_idp.mint", "--user", user])
        if ttl is not None:
            cmd.extend(["--ttl", str(ttl)])
        proc = self.run_cmd(cmd)
        token = proc.stdout.strip()
        if not token:
            raise CheckFailure(f"签发器返回了空 token (user={user})")
        self.tokens[cache_key] = token
        self.used_tokens.append(token)
        return token

    def http_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> HttpResponse:
        req_headers = dict(headers or {})
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return HttpResponse(
                    status=resp.status,
                    headers=resp_headers,
                    body=resp.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in exc.headers.items()}
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except (
                TimeoutError,
                ConnectionError,
                OSError,
                http.client.HTTPException,
            ) as read_exc:
                raise CheckFailure(
                    f"读取 {method} {url} 错误响应失败 (HTTP {exc.code}): {read_exc}"
                ) from read_exc
            return HttpResponse(status=exc.code, headers=resp_headers, body=body)
        except urllib.error.URLError as exc:
            raise CheckFailure(f"请求 {method} {url} 连接失败: {exc.reason}") from exc
        except (TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            raise CheckFailure(f"请求 {method} {url} 传输失败: {exc}") from exc

    def mcp_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        token: str | None,
        notification: bool = False,
    ) -> HttpResponse:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.next_rpc_id()
        if params is not None:
            payload["params"] = params
        return self.http_request("POST", self.mcp_url, headers=headers, json_body=payload)

    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        token: str,
    ) -> tuple[HttpResponse, dict[str, Any]]:
        resp = self.mcp_call(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            token=token,
        )
        if resp.status != 200:
            raise CheckFailure(
                f"tools/call({name}) 期望 HTTP 200，实际为 {resp.status}: {resp.body[:200]}"
            )
        body = resp.json()
        if "result" not in body or not isinstance(body["result"], dict):
            raise CheckFailure(f"tools/call({name}) 缺少 result 字段: {body}")
        return resp, body["result"]

    def guard_request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> HttpResponse:
        url = f"{self.guard_url.rstrip('/')}{path}"
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if qs:
                url = f"{url}?{qs}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.http_request(method, url, headers=headers, json_body=json_body)

    def load_dev_user(self, username: str) -> dict[str, Any]:
        users_path = self.repo_root / "jwt/users.json"
        users = json.loads(users_path.read_text(encoding="utf-8"))
        for user in users:
            if user.get("username") == username:
                return user
        raise CheckFailure(f"jwt/users.json 中未找到用户: {username}")


@dataclass(frozen=True)
class CheckSpec:
    id: str
    number: int
    title: str
    requires_guard: bool
    fn: Callable[[SmokeContext], None]


CHECKS: list[CheckSpec] = []


def register_check(
    check_id: str,
    title: str,
    *,
    requires_guard: bool = True,
) -> Callable[[Callable[[SmokeContext], None]], Callable[[SmokeContext], None]]:
    match = CHECK_ID_RE.match(check_id)
    if not match:
        raise ValueError(f"Invalid check id: {check_id}")
    number = int(match.group(1))
    normalized_id = f"V{number}"

    def decorator(fn: Callable[[SmokeContext], None]) -> Callable[[SmokeContext], None]:
        CHECKS.append(
            CheckSpec(
                id=normalized_id,
                number=number,
                title=title,
                requires_guard=requires_guard,
                fn=fn,
            )
        )
        return fn

    return decorator


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _extract_first_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    _assert(isinstance(content, list) and len(content) > 0, f"工具结果缺少 content: {result}")
    first = content[0]
    _assert(isinstance(first, dict) and isinstance(first.get("text"), str), f"缺少文本项: {first}")
    return first["text"]


def _assert_tool_error(
    result: dict[str, Any],
    *,
    expected_status: int,
    expected_code: str,
) -> ToolErrorInfo:
    _assert(result.get("isError") is True, f"期望 isError == true，实际为: {result}")
    text = _extract_first_text(result)
    info = parse_tool_error(text)
    _assert(
        info.status == expected_status,
        f"期望上游状态码 {expected_status}，实际为 {info.status} (text={text!r})",
    )
    _assert(
        info.error_body is not None and info.error_body.get("code") == expected_code,
        f"期望错误码 {expected_code!r}，实际响应为 {info.raw_response!r}",
    )
    return info


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcpgate-smoke", "version": "1.0"},
    }


# ---------------------------------------------------------------------------
# V1–V7: Higress 自身应答（鉴权、协议握手、方法限制、工具清单）
# ---------------------------------------------------------------------------


@register_check("V1", "无 token 被拒", requires_guard=False)
def check_v1(ctx: SmokeContext) -> None:
    resp = ctx.mcp_call("initialize", _initialize_params(), token=None)
    _assert(resp.status == 401, f"无 token 期望 HTTP 401，实际为 {resp.status}")


@register_check("V2", "过期 token 被拒", requires_guard=False)
def check_v2(ctx: SmokeContext) -> None:
    expired = ctx.mint("wang", ttl=-300)
    resp = ctx.mcp_call("initialize", _initialize_params(), token=expired)
    _assert(resp.status == 401, f"过期 token 期望 HTTP 401，实际为 {resp.status}")


@register_check("V3", "签名被篡改", requires_guard=False)
def check_v3(ctx: SmokeContext) -> None:
    tampered = tamper_jwt(ctx.mint("wang"))
    resp = ctx.mcp_call("initialize", _initialize_params(), token=tampered)
    _assert(resp.status == 401, f"篡改签名期望 HTTP 401，实际为 {resp.status}")


@register_check("V4", "aud 不符被 Higress 拒绝", requires_guard=False)
def check_v4(ctx: SmokeContext) -> None:
    wrong_aud = ctx.mint("wang", audience="other")
    resp = ctx.mcp_call("initialize", _initialize_params(), token=wrong_aud)
    _assert(
        resp.status == 401,
        f"aud 不符期望 HTTP 401，实际为 {resp.status}（若为 200 说明 claims.aud 未生效）",
    )


@register_check("V5", "握手", requires_guard=False)
def check_v5(ctx: SmokeContext) -> None:
    token = ctx.mint("wang")
    init_resp = ctx.mcp_call("initialize", _initialize_params(), token=token)
    _assert(init_resp.status == 200, f"initialize 期望 200，实际为 {init_resp.status}")
    init_body = init_resp.json()
    server_name = ((init_body.get("result") or {}).get("serverInfo") or {}).get("name")
    _assert(
        server_name == "aftersale",
        f"期望 serverInfo.name == 'aftersale'，实际为 {server_name!r}",
    )

    notify_resp = ctx.mcp_call(
        "notifications/initialized",
        None,
        token=token,
        notification=True,
    )
    _assert(
        notify_resp.status == 202,
        f"notifications/initialized 期望 HTTP 202，实际为 {notify_resp.status}",
    )


@register_check("V6", "非 POST", requires_guard=False)
def check_v6(ctx: SmokeContext) -> None:
    token = ctx.mint("wang")
    resp = ctx.http_request(
        "GET",
        ctx.mcp_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )
    _assert(resp.status == 405, f"GET MCP 端点期望 HTTP 405，实际为 {resp.status}")


@register_check("V7", "工具清单", requires_guard=False)
def check_v7(ctx: SmokeContext) -> None:
    token = ctx.mint("wang")
    resp = ctx.mcp_call("tools/list", None, token=token)
    _assert(resp.status == 200, f"tools/list 期望 HTTP 200，实际为 {resp.status}")
    body = resp.json()
    tools = (body.get("result") or {}).get("tools")
    _assert(isinstance(tools, list), f"tools/list 返回缺少 tools 数组: {body}")

    assert ctx.manifest_path is not None
    manifest = json.loads(ctx.manifest_path.read_text(encoding="utf-8"))
    expected_names = {item["name"] for item in manifest}
    actual_names = {t.get("name") for t in tools if isinstance(t, dict)}
    _assert(
        actual_names == expected_names,
        f"tools/list 工具集合与 tools.manifest.json 不一致: "
        f"actual={sorted(actual_names)}, expected={sorted(expected_names)}",
    )

    create_refund = next((t for t in tools if t.get("name") == "create_refund"), None)
    _assert(create_refund is not None, "tools/list 缺少 create_refund")
    desc = str(create_refund.get("description") or "")
    _assert("【写操作】" in desc, f"create_refund 描述缺少'【写操作】': {desc!r}")
    _assert("审批" in desc, f"create_refund 描述缺少'审批': {desc!r}")


# ---------------------------------------------------------------------------
# V8–V17: 经 guard 转发、行级/角色权限传达、写拦截、请求追踪与直连防线
# ---------------------------------------------------------------------------


@register_check("V8", "读调用成功", requires_guard=True)
def check_v8(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call("get_order", {"order_id": "A1023"}, token=ctx.mint("wang"))
    _assert(result.get("isError") is False, f"期望 isError == false，实际为: {result}")
    structured = result.get("structuredContent") or {}
    _assert(
        structured.get("order_id") == "A1023",
        f"期望 structuredContent.order_id == 'A1023'，实际为: {structured}",
    )


@register_check("V9", "越权读被拒且原因可见", requires_guard=True)
def check_v9(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call("get_order", {"order_id": "B2087"}, token=ctx.mint("wang"))
    info = _assert_tool_error(result, expected_status=403, expected_code="order_access_denied")
    assert info.error_body is not None
    request_id = info.error_body.get("request_id")
    _assert(
        isinstance(request_id, str) and bool(request_id.strip()),
        f"V9 错误体缺少 request_id: {info.error_body}",
    )
    ctx.state["v9_request_id"] = request_id


@register_check("V10", "同一请求、不同身份", requires_guard=True)
def check_v10(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call("get_order", {"order_id": "B2087"}, token=ctx.mint("zhao"))
    _assert(result.get("isError") is False, f"zhao 查询 B2087 期望成功，实际为: {result}")
    structured = result.get("structuredContent") or {}
    _assert(
        structured.get("order_id") == "B2087",
        f"期望 structuredContent.order_id == 'B2087'，实际为: {structured}",
    )


@register_check("V11", "404 传达", requires_guard=True)
def check_v11(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call("get_order", {"order_id": "X9999"}, token=ctx.mint("wang"))
    _assert_tool_error(result, expected_status=404, expected_code="order_not_found")


@register_check("V12", "角色限制传达", requires_guard=True)
def check_v12(ctx: SmokeContext) -> None:
    _, wang_result = ctx.tool_call("list_refunds", {}, token=ctx.mint("wang"))
    _assert_tool_error(wang_result, expected_status=403, expected_code="insufficient_role")

    _, li_result = ctx.tool_call("list_refunds", {}, token=ctx.mint("li"))
    _assert(li_result.get("isError") is False, f"li 调 list_refunds 期望成功，实际为: {li_result}")
    structured = li_result.get("structuredContent") or {}
    _assert(isinstance(structured.get("items"), list), f"list_refunds 缺少 items: {structured}")


@register_check("V13", "422 传达", requires_guard=True)
def check_v13(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call("list_orders", {"page_size": 100}, token=ctx.mint("wang"))
    _assert_tool_error(result, expected_status=422, expected_code="validation_error")


@register_check("V14", "写调用被拦截", requires_guard=True)
def check_v14(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call(
        "create_refund",
        {
            "order_id": "A1024",
            "amount": 10,
            "reason_category": "no_reason",
            "reason": "冒烟测试",
        },
        token=ctx.mint("wang"),
    )
    _assert(result.get("isError") is False, f"create_refund 期望被拦截返回 202 成功: {result}")
    structured = result.get("structuredContent") or {}
    _assert(
        structured.get("status") == "pending",
        f"期望 status == 'pending'，实际为: {structured}",
    )
    _assert(
        structured.get("approval_id") == "AP000001",
        f"期望 approval_id == 'AP000001'，实际为: {structured}",
    )
    message = str(structured.get("message") or "")
    _assert(
        "等待主管审批" in message,
        f"期望 message 含'等待主管审批'，实际为: {message!r}",
    )
    ctx.state["v14_approval_id"] = structured["approval_id"]


@register_check("V15", "申请未执行", requires_guard=True)
def check_v15(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call("get_order", {"order_id": "A1024"}, token=ctx.mint("li"))
    _assert(result.get("isError") is False, f"li 查询 A1024 失败: {result}")
    structured = result.get("structuredContent") or {}
    _assert(
        structured.get("refunded_amount") == 0,
        f"期望 refunded_amount == 0，实际为: {structured.get('refunded_amount')}",
    )
    refunds = structured.get("refunds")
    _assert(
        isinstance(refunds, list) and len(refunds) == 0,
        f"期望 refunds 为空列表，实际为: {refunds!r}",
    )


@register_check("V16", "请求可追踪", requires_guard=True)
def check_v16(ctx: SmokeContext) -> None:
    request_id = ctx.state.get("v9_request_id")
    if not request_id:
        check_v9(ctx)
        request_id = ctx.state.get("v9_request_id")
    _assert(isinstance(request_id, str) and bool(request_id), "无法获取 V9 的 request_id")

    def _has_higress_log() -> bool:
        higress_log = ctx.run_cmd(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "higress",
                "grep",
                "-F",
                "--",
                request_id,
                "/var/log/higress/gateway.log",
            ],
            check=False,
        )
        return higress_log.returncode == 0 and request_id in higress_log.stdout

    _assert(
        _poll_until(
            _has_higress_log,
            attempts=ctx.log_retry_attempts,
            interval=ctx.log_retry_interval,
            sleep_fn=ctx.sleep_fn,
        ),
        f"Higress 访问日志 (/var/log/higress/gateway.log) 在 {ctx.log_retry_attempts} 次检查内"
        f"未找到 request_id={request_id}",
    )

    def _has_mock_api_log() -> bool:
        mock_logs = ctx.run_cmd(["docker", "compose", "logs", "--no-color", "mock-api"])
        combined = mock_logs.stdout + "\n" + mock_logs.stderr
        return request_id in combined

    _assert(
        _poll_until(
            _has_mock_api_log,
            attempts=ctx.log_retry_attempts,
            interval=ctx.log_retry_interval,
            sleep_fn=ctx.sleep_fn,
        ),
        f"mock-api 日志在 {ctx.log_retry_attempts} 次检查内未找到 request_id={request_id}",
    )


@register_check("V17", "网关之外的防线仍在", requires_guard=False)
def check_v17(ctx: SmokeContext) -> None:
    expired = ctx.mint("wang", ttl=-300)
    url = f"{ctx.mock_api_url.rstrip('/')}/api/v1/orders/A1023"
    resp = ctx.http_request("GET", url, headers={"Authorization": f"Bearer {expired}"})
    _assert(resp.status == 401, f"直连 mock-api 过期 token 期望 HTTP 401，实际为 {resp.status}")
    body = resp.json()
    _assert(
        isinstance(body, dict) and body.get("code") == "token_expired",
        f"直连 mock-api 期望错误码 'token_expired'，实际为 {body}",
    )


# ---------------------------------------------------------------------------
# V18–V34: 审批与审计完整闭环（PROPOSAL 3.14）
# ---------------------------------------------------------------------------


def _assert_guard_error(resp: HttpResponse, *, expected_status: int, expected_code: str) -> None:
    _assert(
        resp.status == expected_status,
        f"期望 HTTP {expected_status}，实际为 {resp.status}: {resp.body[:200]}",
    )
    body = resp.json()
    _assert(
        isinstance(body, dict) and body.get("code") == expected_code,
        f"期望错误码 {expected_code!r}，实际为 {body}",
    )


@register_check("V18", "重复提交被拒", requires_guard=True)
def check_v18(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call(
        "create_refund",
        {
            "order_id": "A1024",
            "amount": 10,
            "reason_category": "no_reason",
            "reason": "冒烟测试",
        },
        token=ctx.mint("wang"),
    )
    info = _assert_tool_error(result, expected_status=409, expected_code="approval_pending_exists")
    _assert(
        "AP000001" in info.raw_response,
        f"期望错误信息包含 AP000001，实际为: {info.raw_response!r}",
    )


@register_check("V19", "越权写被拒且不生成审批单", requires_guard=True)
def check_v19(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call(
        "create_refund",
        {
            "order_id": "B2087",
            "amount": 10,
            "reason_category": "no_reason",
            "reason": "越权写测试",
        },
        token=ctx.mint("wang"),
    )
    _assert_tool_error(result, expected_status=403, expected_code="order_access_denied")

    _, list_result = ctx.tool_call("list_approvals", {"order_id": "B2087"}, token=ctx.mint("sun"))
    _assert(list_result.get("isError") is False, f"list_approvals(B2087) 失败: {list_result}")
    structured = list_result.get("structuredContent") or {}
    _assert(
        structured.get("total") == 0,
        f"越权写不应生成审批单，期望 total == 0，实际为: {structured}",
    )


@register_check("V20", "写调用的参数校验", requires_guard=True)
def check_v20(ctx: SmokeContext) -> None:
    _, result = ctx.tool_call(
        "create_refund",
        {
            "order_id": "A1023",
            "amount": -1,
            "reason_category": "no_reason",
            "reason": "负数金额测试",
        },
        token=ctx.mint("wang"),
    )
    _assert_tool_error(result, expected_status=422, expected_code="validation_error")


@register_check("V21", "进度查询与可见范围", requires_guard=True)
def check_v21(ctx: SmokeContext) -> None:
    _, wang_get = ctx.tool_call("get_approval", {"approval_id": "AP000001"}, token=ctx.mint("wang"))
    _assert(wang_get.get("isError") is False, f"wang get_approval(AP000001) 失败: {wang_get}")
    _assert(
        (wang_get.get("structuredContent") or {}).get("status") == "pending",
        f"期望 AP000001 状态为 pending，实际为: {wang_get}",
    )

    _, zhao_get = ctx.tool_call("get_approval", {"approval_id": "AP000001"}, token=ctx.mint("zhao"))
    _assert_tool_error(zhao_get, expected_status=403, expected_code="approval_access_denied")

    _, miss_get = ctx.tool_call("get_approval", {"approval_id": "AP999999"}, token=ctx.mint("wang"))
    _assert_tool_error(miss_get, expected_status=404, expected_code="approval_not_found")

    _, list_res = ctx.tool_call("list_approvals", {"order_id": "A1024"}, token=ctx.mint("wang"))
    _assert(list_res.get("isError") is False, f"wang list_approvals(A1024) 失败: {list_res}")
    _assert(
        (list_res.get("structuredContent") or {}).get("total") == 1,
        f"期望 A1024 审批单 total == 1，实际为: {list_res}",
    )


@register_check("V22", "审批权限：角色与区域", requires_guard=True)
def check_v22(ctx: SmokeContext) -> None:
    r_wang = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000001/approve",
        token=ctx.mint("wang"),
        json_body={"comment": "客服尝试审批"},
    )
    _assert_guard_error(r_wang, expected_status=403, expected_code="insufficient_role")

    r_zhou = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000001/approve",
        token=ctx.mint("zhou"),
        json_body={"comment": "跨区主管尝试审批"},
    )
    _assert_guard_error(r_zhou, expected_status=403, expected_code="approval_access_denied")

    _, check_res = ctx.tool_call(
        "get_approval",
        {"approval_id": "AP000001"},
        token=ctx.mint("wang"),
    )
    _assert(
        (check_res.get("structuredContent") or {}).get("status") == "pending",
        f"期望 AP000001 仍为 pending，实际为: {check_res}",
    )


@register_check("V23", "回避", requires_guard=True)
def check_v23(ctx: SmokeContext) -> None:
    _, sub_res = ctx.tool_call(
        "create_refund",
        {
            "order_id": "A1033",
            "amount": 100,
            "reason_category": "quality_issue",
            "reason": "主管提交退款测试回避规则",
        },
        token=ctx.mint("sun"),
    )
    _assert(sub_res.get("isError") is False, f"sun 提交 A1033 失败: {sub_res}")
    structured = sub_res.get("structuredContent") or {}
    _assert(
        structured.get("approval_id") == "AP000002",
        f"期望 approval_id == 'AP000002'，实际为: {structured}",
    )

    r_self = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000002/approve",
        token=ctx.mint("sun"),
        json_body={"comment": "自批尝试"},
    )
    _assert_guard_error(r_self, expected_status=403, expected_code="self_approval_forbidden")


@register_check("V24", "同意并执行", requires_guard=True)
def check_v24(ctx: SmokeContext) -> None:
    resp = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000001/approve",
        token=ctx.mint("li"),
        json_body={"comment": "同意"},
    )
    _assert(resp.status == 200, f"li 同意 AP000001 期望 200，实际 {resp.status}: {resp.body[:200]}")
    body = resp.json()
    _assert(body.get("status") == "executed", f"期望 status == 'executed'，实际为: {body}")
    _assert(body.get("refund_id") == "R000105", f"期望 refund_id == 'R000105'，实际为: {body}")


@register_check("V25", "执行用的是审批人的身份", requires_guard=True)
def check_v25(ctx: SmokeContext) -> None:
    _, refund_res = ctx.tool_call("get_refund", {"refund_id": "R000105"}, token=ctx.mint("li"))
    _assert(refund_res.get("isError") is False, f"get_refund(R000105) 失败: {refund_res}")
    r_data = refund_res.get("structuredContent") or {}
    _assert(r_data.get("created_by") == "u2001", f"期望 created_by == 'u2001'，实际为: {r_data}")
    _assert(
        r_data.get("created_by_role") == "supervisor",
        f"期望 created_by_role == 'supervisor'，实际为: {r_data}",
    )

    _, order_res = ctx.tool_call("get_order", {"order_id": "A1024"}, token=ctx.mint("li"))
    _assert(order_res.get("isError") is False, f"get_order(A1024) 失败: {order_res}")
    o_data = order_res.get("structuredContent") or {}
    _assert(
        o_data.get("refunded_amount") == 10,
        f"期望 A1024 refunded_amount == 10，实际为: {o_data.get('refunded_amount')}",
    )


@register_check("V26", "重复同意不重复退款", requires_guard=True)
def check_v26(ctx: SmokeContext) -> None:
    resp = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000001/approve",
        token=ctx.mint("li"),
        json_body={"comment": "重复同意"},
    )
    _assert_guard_error(resp, expected_status=409, expected_code="approval_not_pending")

    _, order_res = ctx.tool_call("get_order", {"order_id": "A1024"}, token=ctx.mint("li"))
    _assert(order_res.get("isError") is False, f"get_order(A1024) 失败: {order_res}")
    refunds = (order_res.get("structuredContent") or {}).get("refunds")
    _assert(
        isinstance(refunds, list) and len(refunds) == 1,
        f"期望 A1024 refunds 仍只有 1 张，实际为: {refunds!r}",
    )


@register_check("V27", "拒绝", requires_guard=True)
def check_v27(ctx: SmokeContext) -> None:
    r_no_comment = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000002/reject",
        token=ctx.mint("li"),
        json_body={},
    )
    _assert(
        r_no_comment.status == 422,
        f"不带意见拒绝期望 HTTP 422，实际 {r_no_comment.status}: {r_no_comment.body[:200]}",
    )

    r_ok = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000002/reject",
        token=ctx.mint("li"),
        json_body={"comment": "金额有误"},
    )
    _assert(r_ok.status == 200, f"拒绝 AP000002 期望 200，实际 {r_ok.status}: {r_ok.body[:200]}")
    _assert(r_ok.json().get("status") == "rejected", f"期望 status == 'rejected'，实际 {r_ok.body}")

    _, wang_get = ctx.tool_call("get_approval", {"approval_id": "AP000002"}, token=ctx.mint("wang"))
    _assert(wang_get.get("isError") is False, f"wang get_approval(AP000002) 失败: {wang_get}")
    ap_data = wang_get.get("structuredContent") or {}
    _assert(
        "金额有误" in str(ap_data.get("message") or ""),
        f"期望 AP000002 message 含'金额有误'，实际为: {ap_data}",
    )

    _, order_res = ctx.tool_call("get_order", {"order_id": "A1033"}, token=ctx.mint("li"))
    _assert(order_res.get("isError") is False, f"get_order(A1033) 失败: {order_res}")
    _assert(
        (order_res.get("structuredContent") or {}).get("refunded_amount") == 0,
        f"期望 A1033 refunded_amount == 0，实际为: {order_res}",
    )


@register_check("V28", "同意后被订单系统拒绝", requires_guard=True)
def check_v28(ctx: SmokeContext) -> None:
    _, sub_res = ctx.tool_call(
        "create_refund",
        {
            "order_id": "A1029",
            "amount": 10,
            "reason_category": "other",
            "reason": "测试未支付订单执行失败",
        },
        token=ctx.mint("wang"),
    )
    _assert(sub_res.get("isError") is False, f"提交 A1029 申请期望受理: {sub_res}")
    sub_data = sub_res.get("structuredContent") or {}
    _assert(
        sub_data.get("approval_id") == "AP000003",
        f"期望 approval_id == 'AP000003'，实际为: {sub_data}",
    )

    r_app = ctx.guard_request(
        "POST",
        "/guard/v1/console/approvals/AP000003/approve",
        token=ctx.mint("li"),
        json_body={"comment": "同意"},
    )
    _assert(r_app.status == 200, f"同意 AP000003 期望 HTTP 200，实际 {r_app.status}")
    app_data = r_app.json()
    _assert(
        app_data.get("status") == "execution_failed",
        f"期望 status == 'execution_failed'，实际为: {app_data}",
    )
    _assert(
        app_data.get("failure_code") == "order_not_refundable",
        f"期望 failure_code == 'order_not_refundable'，实际为: {app_data}",
    )

    _, get_res = ctx.tool_call("get_approval", {"approval_id": "AP000003"}, token=ctx.mint("wang"))
    _assert(get_res.get("isError") is False, f"get_approval(AP000003) 失败: {get_res}")
    msg = str((get_res.get("structuredContent") or {}).get("message") or "")
    _assert("拒绝执行" in msg, f"期望 message 含'拒绝执行'，实际为: {msg!r}")


@register_check("V29", "审计：越权读有记录且状态码真实", requires_guard=True)
def check_v29(ctx: SmokeContext) -> None:
    request_id = ctx.state.get("v9_request_id")
    if not request_id:
        check_v9(ctx)
        request_id = ctx.state.get("v9_request_id")
    _assert(isinstance(request_id, str) and bool(request_id), "无法获取 V9 的 request_id")

    resp = ctx.guard_request(
        "GET",
        "/guard/v1/console/audit-logs",
        token=ctx.mint("sun"),
        params={"request_id": request_id},
    )
    _assert(resp.status == 200, f"查询审计期望 200，实际 {resp.status}: {resp.body[:200]}")
    data = resp.json()
    items = data.get("items") or []
    _assert(
        data.get("total") == 1 and len(items) == 1,
        f"期望 request_id={request_id} 恰好 1 条审计记录，实际为: {data}",
    )
    ev = items[0]
    _assert(ev.get("tool") == "get_order", f"期望 tool == 'get_order'，实际: {ev}")
    _assert(ev.get("actor_sub") == "u1001", f"期望 actor_sub == 'u1001'，实际: {ev}")
    _assert(ev.get("outcome") == "access_denied", f"期望 outcome == 'access_denied'，实际: {ev}")
    _assert(ev.get("status_code") == 403, f"期望 status_code == 403，实际: {ev}")
    _assert(
        (ev.get("arguments") or {}).get("order_id") == "B2087",
        f"期望 arguments.order_id == 'B2087'，实际: {ev}",
    )


@register_check("V30", "审计：审批链完整，被拒的审批尝试也有记录", requires_guard=True)
def check_v30(ctx: SmokeContext) -> None:
    resp = ctx.guard_request(
        "GET",
        "/guard/v1/console/audit-logs",
        token=ctx.mint("sun"),
        params={"approval_id": "AP000001", "page_size": 100},
    )
    _assert(resp.status == 200, f"查询 AP000001 审计期望 200，实际 {resp.status}")
    items = sorted(resp.json().get("items") or [], key=lambda e: int(e["id"]))

    main_chain = [
        e
        for e in items
        if e.get("tool") == "create_refund" and e.get("outcome") in ("pending_approval", "success")
    ]
    _assert(
        len(main_chain) == 3,
        f"期望 AP000001 主审批链恰好 3 条记录，实际 {len(main_chain)} 条: {main_chain}",
    )
    sub_ev, app_ev, exec_ev = main_chain
    _assert(
        sub_ev.get("event_type") == "tool_call"
        and sub_ev.get("outcome") == "pending_approval"
        and sub_ev.get("actor_sub") == "u1001",
        f"第 1 条提交记录不符: {sub_ev}",
    )
    _assert(
        app_ev.get("event_type") == "approval_approve" and app_ev.get("actor_sub") == "u2001",
        f"第 2 条同意记录不符: {app_ev}",
    )
    _assert(
        exec_ev.get("event_type") == "approval_execute"
        and exec_ev.get("actor_sub") == "u2001"
        and exec_ev.get("on_behalf_of_sub") == "u1001"
        and exec_ev.get("status_code") == 201
        and (exec_ev.get("result") or {}).get("refund_id") == "R000105",
        f"第 3 条执行记录不符: {exec_ev}",
    )

    denied_approves = [
        e
        for e in items
        if e.get("event_type") == "approval_approve" and e.get("outcome") == "access_denied"
    ]
    denied_actors = {e.get("actor_sub") for e in denied_approves}
    _assert(
        {"u1001", "u2002"}.issubset(denied_actors) and len(denied_approves) >= 2,
        f"期望 V22 留下 u1001 与 u2002 两条被拒审批记录，实际: {denied_approves}",
    )

    conflict_approves = [
        e
        for e in items
        if e.get("event_type") == "approval_approve" and e.get("outcome") == "conflict"
    ]
    _assert(
        len(conflict_approves) >= 1,
        f"期望 V26 留下至少一条 conflict 审批记录，实际: {conflict_approves}",
    )


@register_check("V31", "审计：角色与区域范围", requires_guard=True)
def check_v31(ctx: SmokeContext) -> None:
    r_wang = ctx.guard_request("GET", "/guard/v1/console/audit-logs", token=ctx.mint("wang"))
    _assert_guard_error(r_wang, expected_status=403, expected_code="insufficient_role")

    r_zhou = ctx.guard_request(
        "GET",
        "/guard/v1/console/audit-logs",
        token=ctx.mint("zhou"),
        params={"actor": "u1001"},
    )
    _assert(r_zhou.status == 200, f"zhou 查询审计期望 200，实际 {r_zhou.status}")
    _assert(
        r_zhou.json().get("total") == 0,
        f"zhou 不应看到华东客服 u1001 的审计记录，实际: {r_zhou.json()}",
    )


@register_check("V32", "审计不含凭证", requires_guard=True)
def check_v32(ctx: SmokeContext) -> None:
    all_items: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = ctx.guard_request(
            "GET",
            "/guard/v1/console/audit-logs",
            token=ctx.mint("sun"),
            params={"page": page, "page_size": 100},
        )
        _assert(resp.status == 200, f"拉取全量审计期望 200，实际 {resp.status}")
        data = resp.json()
        batch = data.get("items") or []
        all_items.extend(batch)
        if len(all_items) >= int(data.get("total") or 0) or not batch:
            break
        page += 1

    blob = json.dumps(all_items, ensure_ascii=False)
    for token in ctx.used_tokens:
        _assert(token not in blob, "审计记录中发现了明文 JWT token！")
        parts = token.split(".")
        if len(parts) == 3 and len(parts[2]) >= 12:
            _assert(parts[2] not in blob, "审计记录中发现了 JWT 签名片段！")


@register_check("V33", "审批页与登录", requires_guard=True)
def check_v33(ctx: SmokeContext) -> None:
    r_page = ctx.http_request("GET", f"{ctx.guard_url.rstrip('/')}/console/")
    _assert(r_page.status == 200, f"GET /console/ 期望 200，实际 {r_page.status}")
    ctype = r_page.headers.get("content-type", "")
    _assert("text/html" in ctype, f"期望 Content-Type 含 text/html，实际为 {ctype!r}")

    li_user = ctx.load_dev_user("li")
    r_login = ctx.guard_request(
        "POST",
        "/guard/v1/console/login",
        json_body={"username": li_user["username"], "password": li_user["password"]},
    )
    _assert(r_login.status == 200, f"开发登录期望 200，实际 {r_login.status}: {r_login.body[:200]}")
    access_token = r_login.json().get("access_token")
    _assert(
        isinstance(access_token, str) and bool(access_token.strip()),
        f"开发登录未返回有效 access_token: {r_login.body}",
    )

    r_approvals = ctx.guard_request("GET", "/guard/v1/console/approvals", token=access_token)
    _assert(
        r_approvals.status == 200,
        f"用开发登录 token 调控制台审批列表期望 200，实际 {r_approvals.status}",
    )


@register_check("V34", "guard 独立验签，且不是开放代理", requires_guard=True)
def check_v34(ctx: SmokeContext) -> None:
    r_no_tok = ctx.guard_request("GET", "/api/v1/orders/A1023", token=None)
    _assert(r_no_tok.status == 401, f"无 token 直连 guard 期望 401，实际 {r_no_tok.status}")

    r_openapi = ctx.guard_request("GET", "/openapi.json", token=ctx.mint("wang"))
    _assert_guard_error(r_openapi, expected_status=404, expected_code="route_not_found")


# ---------------------------------------------------------------------------
# Selection, clean-state guard check, and runner
# ---------------------------------------------------------------------------


def parse_only_selector(spec: str | None, checks: list[CheckSpec]) -> list[CheckSpec]:
    """Parse --only selector such as 'V1-V7' or 'V1,V5,V8-V10'."""
    ordered = sorted(checks, key=lambda c: c.number)
    if not spec:
        return ordered

    by_num = {c.number: c for c in ordered}
    selected_nums: set[int] = set()

    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            pieces = part.split("-", 1)
            m_start = CHECK_ID_RE.match(pieces[0].strip())
            m_end = CHECK_ID_RE.match(pieces[1].strip())
            if not m_start or not m_end:
                raise ValueError(f"非法的验证项区间: {part!r}（示例: V1-V7）")
            start_n = int(m_start.group(1))
            end_n = int(m_end.group(1))
            if start_n > end_n:
                raise ValueError(f"区间起点大于终点: {part!r}")
            matched_any = False
            for num in range(start_n, end_n + 1):
                if num in by_num:
                    selected_nums.add(num)
                    matched_any = True
            if not matched_any:
                raise ValueError(f"区间 {part!r} 未匹配任何已注册验证项")
        else:
            match = CHECK_ID_RE.match(part)
            if not match:
                raise ValueError(f"非法的验证项编号: {part!r}（示例: V5）")
            num = int(match.group(1))
            if num not in by_num:
                raise ValueError(f"未知的验证项编号: {part!r}")
            selected_nums.add(num)

    if not selected_nums:
        raise ValueError(f"--only {spec!r} 未选中任何验证项")
    return [c for c in ordered if c.number in selected_nums]


def verify_clean_guard_state(ctx: SmokeContext) -> None:
    """Before running V8+, verify guard starts with empty approvals (PROPOSAL 3.14)."""
    _, result = ctx.tool_call("list_approvals", {}, token=ctx.mint("sun"))
    if result.get("isError") is True:
        raise CheckFailure(f"前置检查 list_approvals 失败: {result}")
    structured = result.get("structuredContent") or {}
    total = structured.get("total")
    if total != 0:
        raise CheckFailure(
            f"guard 当前已有 {total} 条审批记录，请先重置数据后再运行：\n"
            "  docker compose exec guard python -m app.reset --yes\n"
            "  docker compose restart mock-api"
        )


def run_checks(
    ctx: SmokeContext,
    selected: list[CheckSpec],
    *,
    skip_clean_check: bool = False,
) -> tuple[int, int]:
    """Execute selected checks in order. Returns (passed, failed)."""
    needs_clean_check = (not skip_clean_check) and any(c.requires_guard for c in selected)
    clean_checked = False
    passed = 0
    failed = 0

    for spec in selected:
        if needs_clean_check and not clean_checked and spec.number >= 8:
            try:
                verify_clean_guard_state(ctx)
            except CheckFailure as exc:
                print(f"[PRECHECK FAIL] {exc}", file=sys.stderr)
                return passed, failed + 1
            except Exception as exc:  # pragma: no cover - defensive runtime catch
                print(f"[PRECHECK FAIL] 前置检查发生未预期异常: {exc}", file=sys.stderr)
                return passed, failed + 1
            clean_checked = True

        try:
            spec.fn(ctx)
        except CheckFailure as exc:
            failed += 1
            print(f"[FAIL] {spec.id} {spec.title}: {exc}")
        except Exception as exc:  # pragma: no cover - defensive runtime catch
            failed += 1
            print(f"[FAIL] {spec.id} {spec.title}: 未预期异常: {exc}")
        else:
            passed += 1
            print(f"[PASS] {spec.id} {spec.title}")

    return passed, failed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCPGate 网关端到端验证脚本（V1–V34）")
    parser.add_argument(
        "--only",
        help="只执行指定验证项或区间，如 V1-V7、V1,V5,V8-V10",
    )
    parser.add_argument(
        "--mcp-url",
        default="http://127.0.0.1:8080/aftersale/mcp",
        help="Higress MCP 端点 URL",
    )
    parser.add_argument(
        "--mock-api-url",
        default="http://127.0.0.1:8000",
        help="mock-api 直连基础 URL（用于 V17）",
    )
    parser.add_argument(
        "--guard-url",
        default="http://127.0.0.1:8020",
        help="guard 直连基础 URL（用于 V18–V34）",
    )
    parser.add_argument(
        "--manifest",
        help="tools.manifest.json 路径（默认 gateway/generated/tools.manifest.json）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="单次 HTTP 请求超时（秒）",
    )
    parser.add_argument(
        "--skip-clean-check",
        action="store_true",
        help="跳过 V8 之前的 list_approvals 空库检查",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent.parent
    try:
        selected = parse_only_selector(args.only, CHECKS)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    ctx = SmokeContext(
        repo_root=repo_root,
        mcp_url=args.mcp_url,
        mock_api_url=args.mock_api_url,
        guard_url=args.guard_url,
        manifest_path=Path(args.manifest) if args.manifest else None,
        timeout=args.timeout,
    )

    passed, failed = run_checks(ctx, selected, skip_clean_check=args.skip_clean_check)
    print(f"\n完成：通过 {passed} 项，失败 {failed} 项。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
