"""Tests for clients response normalization and token providers (5.13)."""

from __future__ import annotations

import base64
import json
import subprocess

import httpx
import pytest

from harness.clients import (
    ChatClient,
    GuardClient,
    McpClient,
    normalize_chat_response,
    normalize_guard_response,
    normalize_mcp_response,
)
from harness.config import Settings
from harness.tokens import StaticTokenProvider, TokenError, TokenProvider, tamper_jwt


def test_normalize_mcp_all_variants() -> None:
    # 1. Success read (2.8.3)
    ok_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": "# API Response\n{}"}],
                "structuredContent": {"order_id": "A1023", "region": "east"},
            },
        }
    )
    res = normalize_mcp_response("get_order", 200, ok_body)
    assert res["result"] == "ok"
    assert res["status_code"] == 200
    assert res["data"]["order_id"] == "A1023"
    assert res["is_jsonrpc_response"] is True
    assert res["has_structured_content"] is True
    assert res["env_error"] is False

    # 2. Write tool intercepted as pending (3.7.1, 5.10.2)
    pending_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "isError": False,
                "structuredContent": {
                    "approval_id": "AP000001",
                    "status": "pending",
                    "message": "已提交退款申请 AP000001，等待主管审批。",
                },
            },
        }
    )
    res_p = normalize_mcp_response("create_refund", 200, pending_body, is_write_tool=True)
    assert res_p["result"] == "pending"
    assert res_p["status_code"] == 202
    assert res_p["data"]["approval_id"] == "AP000001"

    # 3. SSE envelope
    sse_body = f"event: message\ndata: {ok_body}\n\n"
    res_sse = normalize_mcp_response("get_order", 200, sse_body, content_type="text/event-stream")
    assert res_sse["result"] == "ok"

    # 4. isError 403 / 404 / 409 / 422 / 401 / 400 / 503 non-JSON (2.8.4)
    for code_num, err_code, expected_result in (
        (403, "order_access_denied", "denied"),
        (404, "order_not_found", "not_found"),
        (409, "approval_pending_exists", "conflict"),
        (422, "validation_error", "invalid"),
        (401, "token_expired", "auth"),
        (400, "bad_request", "invalid"),
    ):
        g8 = json.dumps({"code": err_code, "message": "提示", "request_id": "req-1"})
        err_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": f"call failed, status: {code_num}, response: {g8}"}
                    ],
                },
            }
        )
        r = normalize_mcp_response("get_order", 200, err_body)
        assert r["result"] == expected_result
        assert r["status_code"] == code_num
        assert r["error_code"] == err_code
        assert r["request_id"] == "req-1"
        assert r["raw_error_body_keys"] == ["code", "message", "request_id"]
        assert r["has_structured_content"] is False
        assert r["env_error"] is False

    # Non-JSON 503 from Envoy inside isError
    envoy_503 = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "call failed, status: 503, response: upstream connect error",
                    }
                ],
            },
        }
    )
    r503 = normalize_mcp_response("get_order", 200, envoy_503)
    assert r503["result"] == "unavailable"
    assert r503["status_code"] == 503
    assert r503["raw_error_body_keys"] is None
    assert r503["env_error"] is True

    # HTTP 401 from Higress jwt-auth (plain text, no JSON-RPC)
    r401 = normalize_mcp_response("get_order", 401, "Jwt is expired")
    assert r401["result"] == "auth"
    assert r401["http_status"] == 401
    assert r401["is_jsonrpc_response"] is False
    assert r401["env_error"] is False

    # HTTP 503 directly
    r_http_503 = normalize_mcp_response("get_order", 503, "Service Unavailable")
    assert r_http_503["result"] == "unavailable"
    assert r_http_503["env_error"] is True

    # JSON-RPC error (-32602 unknown tool) -> never reaches guard, not a JSON-RPC result (L7-5)
    rpc_err = json.dumps(
        {"jsonrpc": "2.0", "id": 5, "error": {"code": -32602, "message": "unknown tool: foo"}}
    )
    r_rpc = normalize_mcp_response("foo", 200, rpc_err)
    assert r_rpc["result"] == "internal"
    assert r_rpc["status_code"] is None
    assert r_rpc["error_code"] == "-32602"
    assert r_rpc["is_jsonrpc_response"] is False
    assert r_rpc["is_jsonrpc_error"] is True
    assert r_rpc["env_error"] is True
    assert "unknown tool" in r_rpc["message"]

    # Malformed payloads
    assert normalize_mcp_response("get_order", 200, "not-json")["env_error"] is True
    assert normalize_mcp_response("get_order", 200, "[]")["env_error"] is True
    assert (
        normalize_mcp_response("get_order", 200, json.dumps({"jsonrpc": "2.0"}))["env_error"]
        is True
    )
    assert (
        normalize_mcp_response("get_order", 200, json.dumps({"result": {"isError": False}}))[
            "env_error"
        ]
        is True
    )
    assert (
        normalize_mcp_response(
            "get_order",
            200,
            json.dumps(
                {"result": {"isError": True, "content": [{"type": "text", "text": "weird"}]}}
            ),
        )["env_error"]
        is True
    )


def test_normalize_guard_and_chat_variants() -> None:
    # Guard 200
    g_ok = normalize_guard_response(
        200,
        json.dumps({"approval_id": "AP000001", "status": "executed", "message": "退款已执行"}),
        {"X-Request-Id": "req-g1"},
    )
    assert g_ok["http_status"] == 200
    assert g_ok["request_id"] == "req-g1"
    assert g_ok["env_error"] is False

    # Guard 403
    g_403 = normalize_guard_response(
        403,
        json.dumps(
            {"code": "self_approval_forbidden", "message": "不能自批", "request_id": "req-g2"}
        ),
        {},
    )
    assert g_403["error_code"] == "self_approval_forbidden"
    assert g_403["request_id"] == "req-g2"
    assert g_403["raw_error_body_keys"] == ["code", "message", "request_id"]

    # Guard 500 non-JSON
    g_500 = normalize_guard_response(500, "Internal Server Error", {})
    assert g_500["env_error"] is True

    # Chat 200 normal vs trace unavailable
    c_ok = normalize_chat_response(
        200,
        json.dumps(
            {
                "conversation_id": "c1",
                "message_id": "m1",
                "answer": "可退",
                "citations": [],
                "trace": [{"tool": "get_order", "kind": "ok"}],
            }
        ),
        {"X-Request-Id": "req-c1"},
    )
    assert c_ok["env_error"] is False
    assert c_ok["answer"] == "可退"

    c_unavail = normalize_chat_response(
        200,
        json.dumps(
            {
                "conversation_id": "c1",
                "answer": "暂不可用",
                "citations": [],
                "trace": [{"tool": "get_order", "kind": "unavailable"}],
            }
        ),
        {},
    )
    assert c_unavail["env_error"] is True

    c_401 = normalize_chat_response(
        401,
        json.dumps({"code": "token_expired", "message": "过期", "request_id": "req-c2"}),
        {},
    )
    assert c_401["error_code"] == "token_expired"
    assert c_401["env_error"] is False

    c_502 = normalize_chat_response(502, json.dumps({"code": "dify_error", "message": "失败"}), {})
    assert c_502["env_error"] is True


def test_http_clients_with_mock_transport() -> None:
    # Verify MCP request headers (no Origin, proper Accept)
    seen_headers: list[dict[str, str]] = []

    def mcp_handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"isError": False, "structuredContent": {"ok": True}},
            },
        )

    mcp = McpClient(
        "http://mcp.test/aftersale/mcp",
        http_client=httpx.Client(transport=httpx.MockTransport(mcp_handler)),
    )
    res = mcp.call_tool("get_order", {"order_id": "A1023"}, "tok.abc.def")
    assert res["result"] == "ok"
    assert "origin" not in {k.lower() for k in seen_headers[0]}
    assert "text/event-stream" in seen_headers[0]["accept"]

    # Guard pagination in fetch_audit_since
    all_events = [{"id": i} for i in range(10, 0, -1)]  # newest first: 10..1

    def guard_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("/audit-logs"):
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("page_size", "50"))
            start = (page - 1) * size
            return httpx.Response(
                200, json={"items": all_events[start : start + size], "total": len(all_events)}
            )
        return httpx.Response(200, json={"status": "executed"}, headers={"X-Request-Id": "r1"})

    guard = GuardClient(
        "http://guard.test", http_client=httpx.Client(transport=httpx.MockTransport(guard_handler))
    )
    assert guard.healthz() == 200
    assert guard.latest_audit_id("tok") == 10
    fetched, cursor = guard.fetch_audit_since(4, "tok", page_size=3)
    assert [e["id"] for e in fetched] == [5, 6, 7, 8, 9, 10]
    assert cursor == 10
    assert guard.call("approve", "tok", approval_id="AP000001", comment="ok")["http_status"] == 200
    assert guard.call("reject", "tok", approval_id="AP000001", comment="no")["http_status"] == 200
    assert guard.call("order", "tok", approval_id="AP000001")["http_status"] == 200
    assert guard.call("detail", "tok", approval_id="AP000001")["http_status"] == 200

    # Chat client
    def chat_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        assert body["stream"] is False
        return httpx.Response(
            200, json={"conversation_id": "c1", "answer": "hi", "citations": [], "trace": []}
        )

    chat = ChatClient(
        "http://chat.test", http_client=httpx.Client(transport=httpx.MockTransport(chat_handler))
    )
    assert chat.healthz() == 200
    assert chat.send_message("hello", "tok")["answer"] == "hi"


def test_token_provider_and_tamper() -> None:
    # M14: tampering modifies middle of signature so decoded RS256 bytes ALWAYS change
    # for all 4 legal 256-byte base64url trailing characters (A / Q / g / w).
    for last_byte, expected_tail in ((0x00, "A"), (0x01, "Q"), (0x02, "g"), (0x03, "w")):
        raw_sig = bytes([0x11] * 255 + [last_byte])
        sig_b64 = base64.urlsafe_b64encode(raw_sig).decode("ascii").rstrip("=")
        assert sig_b64.endswith(expected_tail)
        token = f"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.{sig_b64}"
        tampered = tamper_jwt(token)
        assert tampered != token
        t_parts = tampered.split(".")
        assert len(t_parts) == 3
        assert t_parts[0] == "eyJhbGciOiJSUzI1NiJ9"
        assert t_parts[1] == "eyJzdWIiOiJ1MTAwMSJ9"
        pad = "=" * ((4 - len(t_parts[2]) % 4) % 4)
        decoded_tampered = base64.urlsafe_b64decode(t_parts[2] + pad)
        assert decoded_tampered != raw_sig

    # Also cover 'A' -> 'B' replacement and short signature
    assert tamper_jwt("aaa.bbb.AA") == "aaa.bbb.BA"
    assert tamper_jwt("aaa.bbb.ccca") == "aaa.bbb.ccAa"
    with pytest.raises(TokenError):
        tamper_jwt("")
    with pytest.raises(TokenError):
        tamper_jwt("not-a-jwt")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        user = cmd[cmd.index("--user") + 1]
        suffix = "exp" if "--ttl" in cmd else "val"
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"hdr.{user}{suffix}.sig1234567890123456\n", stderr=""
        )

    tp = TokenProvider(Settings(), runner=fake_run)
    t1 = tp.get_token("wang", "valid")
    t2 = tp.get_token("wang", "valid")
    assert t1 == t2
    assert len(calls) == 1  # cached!
    assert tp.get_token("wang", "none") is None
    t_exp = tp.get_token("wang", "expired")
    assert t_exp is not None and "exp" in t_exp
    t_tamp = tp.get_token("wang", "tampered")
    assert t_tamp is not None and t_tamp != t1
    assert len(tp.all_issued_tokens()) == 3

    def bad_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(TokenError):
        TokenProvider(Settings(), runner=bad_run).get_token("wang", "valid")

    stp = StaticTokenProvider({"wang": "eyJaaa.eyJbbb.sig1234567890123456"})
    assert stp.get_token("wang", "none") is None
    assert stp.get_token("wang", "valid") is not None
    assert stp.get_token("wang", "expired") is not None
    assert stp.get_token("wang", "tampered") is not None
