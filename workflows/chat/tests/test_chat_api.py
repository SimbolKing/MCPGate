from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings
from app.errors import validation_details_from_pydantic
from app.log import MAX_ERROR_DETAIL_LENGTH, JsonFormatter, sanitize_error_detail
from app.main import MessageRequest, get_web_dir
from tests.conftest import (
    TEST_DIFY_API_KEY,
    FailingByteStream,
    FakeDify,
    FakeIdp,
    encode_sse_events,
)

CONV_ID_PREV = "11111111-1111-4111-8111-111111111111"
CONV_ID_HIST = "22222222-2222-4222-8222-222222222222"
CONV_ID_MISSING = "33333333-3333-4333-8333-333333333333"
CONV_ID_OTHER = "44444444-4444-4444-8444-444444444444"
CONV_ID_STALE = "55555555-5555-4555-8555-555555555555"
CONV_ID_NEW = "66666666-6666-4666-8666-666666666666"


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        ev_name = ""
        data_str = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        events.append((ev_name, json.loads(data_str)))
    return events


class _MemoryJsonHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []
        self.setFormatter(JsonFormatter("Asia/Shanghai"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def _attach_log_capture() -> _MemoryJsonHandler:
    handler = _MemoryJsonHandler()
    chat_logger = logging.getLogger("chat")
    chat_logger.addHandler(handler)
    return handler


def _detach_log_capture(handler: _MemoryJsonHandler) -> None:
    logging.getLogger("chat").removeHandler(handler)


def test_public_endpoints_and_static_assets(
    client: TestClient,
    build_app: Callable[..., Any],
) -> None:
    r_health = client.get("/healthz")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "ok"}

    r_root = client.get("/")
    assert r_root.status_code == 200
    assert "MCPGate 售后助手" in r_root.text
    assert get_web_dir().is_dir()

    r_js = client.get("/static/chat.js")
    assert r_js.status_code == 200
    assert "mcpgate_chat_token" in r_js.text

    r_css = client.get("/static/style.css")
    assert r_css.status_code == 200

    r_cfg = client.get("/chat/api/config")
    assert r_cfg.status_code == 200
    assert r_cfg.json() == {
        "dev_login_enabled": True,
        "approval_console_url": "http://localhost:8020/console/",
    }
    assert r_cfg.headers["Cache-Control"] == "no-store"

    # Config when dev_login_url is empty
    app_no_login = build_app(
        custom_settings=Settings(
            dify_api_key=TEST_DIFY_API_KEY,
            dev_login_url="",
            approval_console_url="https://guard.example/console/",
        )
    )
    with TestClient(app_no_login) as tc:
        r_cfg2 = tc.get("/chat/api/config")
        assert r_cfg2.status_code == 200
        assert r_cfg2.json() == {
            "dev_login_enabled": False,
            "approval_console_url": "https://guard.example/console/",
        }

    # OpenAPI / Swagger disabled -> G8 404
    r_docs = client.get("/docs")
    assert r_docs.status_code == 404
    assert r_docs.json()["code"] == "route_not_found"
    assert r_docs.json()["message"] == "请求的接口不存在或未对外开放。"

    # L1: 405 -> method_not_allowed + Chinese message + Allow header preserved
    r_405 = client.get("/chat/api/messages")
    assert r_405.status_code == 405
    body_405 = r_405.json()
    assert set(body_405.keys()) == {"code", "message", "request_id"}
    assert body_405["code"] == "method_not_allowed"
    assert body_405["message"] == "当前接口不支持该请求方法。"
    assert "POST" in r_405.headers.get("allow", "")
    assert r_405.headers["X-Request-Id"] == body_405["request_id"]

    # L1: Unregistered Starlette HTTPException returns fixed Chinese message (no English detail)
    app_http_exc = build_app()

    @app_http_exc.get("/_test_http_418")
    async def _http_418() -> dict[str, str]:
        raise StarletteHTTPException(status_code=418, detail="I am an English teapot")

    with TestClient(app_http_exc) as tc:
        r_418 = tc.get("/_test_http_418")
        assert r_418.status_code == 418
        body_418 = r_418.json()
        assert set(body_418.keys()) == {"code", "message", "request_id"}
        assert body_418["code"] == "http_418"
        assert body_418["message"] == "请求处理失败。"
        assert "English teapot" not in r_418.text


def test_dev_login_forwarding_and_errors(
    client: TestClient,
    build_app: Callable[..., Any],
    fake_idp: FakeIdp,
) -> None:
    log_handler = _attach_log_capture()
    try:
        # Success
        returned_jwt = "eyJhbGciOiJSUzI1NiJ9.LOGIN_TOKEN_SECRET.SIG"
        fake_idp.handler = lambda _req: httpx.Response(
            200,
            json={
                "access_token": returned_jwt,
                "token_type": "Bearer",
                "expires_in": 28800,
            },
        )
        r_ok = client.post(
            "/chat/api/login",
            json={"username": " wang ", "password": "super-secret-password"},
            headers={"X-Request-Id": "req-login-1"},
        )
        assert r_ok.status_code == 200
        assert r_ok.headers["X-Request-Id"] == "req-login-1"
        assert r_ok.headers["Cache-Control"] == "no-store"
        assert r_ok.json() == {
            "access_token": returned_jwt,
            "token_type": "Bearer",
            "expires_in": 28800,
        }
        assert len(fake_idp.requests) == 1
        assert fake_idp.requests[0].json() == {
            "username": "wang",
            "password": "super-secret-password",
        }

        # Credentials & token must never appear in logs
        all_logs = "\n".join(log_handler.records)
        assert "super-secret-password" not in all_logs
        assert returned_jwt not in all_logs
        assert '"username"' not in all_logs

        # 401 -> login_failed
        fake_idp.handler = lambda _req: httpx.Response(401, json={"detail": "invalid"})
        r_401 = client.post("/chat/api/login", json={"username": "wang", "password": "wrong"})
        assert r_401.status_code == 401
        assert r_401.json()["code"] == "login_failed"

        # 400 -> login_failed
        fake_idp.handler = lambda _req: httpx.Response(400, json={"detail": "bad"})
        r_400 = client.post("/chat/api/login", json={"username": "wang", "password": "wrong"})
        assert r_400.status_code == 401
        assert r_400.json()["code"] == "login_failed"

        # 500 -> auth_unavailable
        fake_idp.handler = lambda _req: httpx.Response(500, text="boom")
        r_500 = client.post("/chat/api/login", json={"username": "wang", "password": "pw"})
        assert r_500.status_code == 503
        assert r_500.json()["code"] == "auth_unavailable"

        # Invalid JSON / missing access_token -> auth_unavailable
        fake_idp.handler = lambda _req: httpx.Response(200, text="not-json")
        r_bad_json = client.post("/chat/api/login", json={"username": "wang", "password": "pw"})
        assert r_bad_json.status_code == 503
        assert r_bad_json.json()["code"] == "auth_unavailable"

        fake_idp.handler = lambda _req: httpx.Response(200, json={"token_type": "Bearer"})
        r_no_tok = client.post("/chat/api/login", json={"username": "wang", "password": "pw"})
        assert r_no_tok.status_code == 503
        assert r_no_tok.json()["code"] == "auth_unavailable"

        # Connection error -> auth_unavailable
        def _raise_conn(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        fake_idp.handler = _raise_conn
        r_conn = client.post("/chat/api/login", json={"username": "wang", "password": "pw"})
        assert r_conn.status_code == 503
        assert r_conn.json()["code"] == "auth_unavailable"

        # Blank username -> 422
        r_422 = client.post("/chat/api/login", json={"username": "   ", "password": "pw"})
        assert r_422.status_code == 422
        assert r_422.json()["code"] == "validation_error"
        assert r_422.json()["details"]
    finally:
        _detach_log_capture(log_handler)

    # Unconfigured dev_login_url -> 404 login_not_configured
    app_no_login = build_app(
        custom_settings=Settings(dify_api_key=TEST_DIFY_API_KEY, dev_login_url="")
    )
    with TestClient(app_no_login) as tc:
        r_404 = tc.post("/chat/api/login", json={"username": "wang", "password": "pw"})
        assert r_404.status_code == 404
        assert r_404.json()["code"] == "login_not_configured"


def test_messages_streaming_request_forwarding_and_secret_isolation(
    client: TestClient,
    fake_dify: FakeDify,
    make_token: Callable[..., str],
) -> None:
    raw_token = make_token("wang")
    query_text = "订单 A1023 的客户要退货，能退吗？"
    answer_part_1 = "订单 A1023 符合《售后制度》第 3.2 条，"
    answer_part_2 = "可以办理退货退款。"

    fake_dify.default_events = [
        "event: ping\n\n",
        ": keep-alive comment\n\n",
        {
            "event": "workflow_started",
            "conversation_id": "conv-abc",
            "message_id": "msg-xyz",
            "data": {"inputs": {"user_token": raw_token}},
        },
        {
            "event": "node_started",
            "data": {"node_id": "call_get_order", "inputs": {"user_token": raw_token}},
        },
        {
            "event": "node_finished",
            "data": {
                "node_id": "call_get_order",
                "inputs": {"user_token": raw_token},
                "process_data": {"headers": {"Authorization": f"Bearer {raw_token}"}},
                "outputs": {"status_code": 200, "body": '{"secret_raw": true}'},
            },
        },
        {
            "event": "node_finished",
            "data": {
                "node_id": "parse_order",
                "inputs": {"user_token": raw_token},
                "outputs": {
                    "call_summary": {
                        "tool": "get_order",
                        "kind": "ok",
                        "http_status": 200,
                        "user_token": raw_token,
                        "api_key": TEST_DIFY_API_KEY,
                    },
                    "facts_text": "SENSITIVE_FACTS",
                },
            },
        },
        {
            "event": "message",
            "conversation_id": "conv-abc",
            "message_id": "msg-xyz",
            "answer": answer_part_1,
        },
        {
            "event": "message",
            "conversation_id": "conv-abc",
            "message_id": "msg-xyz",
            "answer": answer_part_2,
        },
        {
            "event": "workflow_finished",
            "conversation_id": "conv-abc",
            "message_id": "msg-xyz",
            "data": {"status": "succeeded"},
        },
        {
            "event": "message_end",
            "conversation_id": "conv-abc",
            "message_id": "msg-xyz",
            "metadata": {
                "retriever_resources": [
                    {
                        "document_name": "售后制度.md",
                        "content": (
                            "售后制度 第 3.2 条 质量问题退货期限\n"
                            "规则：已签收天数不超过 30 天可以退货退款。"
                        ),
                        "score": 0.95,
                    }
                ]
            },
        },
    ]

    log_handler = _attach_log_capture()
    try:
        resp = client.post(
            "/chat/api/messages",
            json={"query": query_text, "conversation_id": "  ", "stream": True},
            headers={
                "Authorization": f"Bearer {raw_token}",
                "X-Request-Id": "req-stream-1",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        assert resp.headers["x-request-id"] == "req-stream-1"

        # Verify request sent to Dify
        assert len(fake_dify.requests) == 1
        dify_req = fake_dify.requests[0]
        assert dify_req.method == "POST"
        assert dify_req.path == "/v1/chat-messages"
        assert dify_req.headers["authorization"] == f"Bearer {TEST_DIFY_API_KEY}"
        assert dify_req.json() == {
            "inputs": {"user_token": raw_token},
            "query": query_text,
            "user": "u1001",
            "response_mode": "streaming",
            "auto_generate_name": False,
        }
        assert "conversation_id" not in dify_req.json()

        # Verify SSE output
        raw_body = resp.text
        assert raw_token not in raw_body
        assert TEST_DIFY_API_KEY not in raw_body
        assert "SENSITIVE_FACTS" not in raw_body
        assert "secret_raw" not in raw_body

        events = _parse_sse(raw_body)
        assert events[0] == ("trace", {"tool": "get_order", "kind": "ok", "http_status": 200})
        assert events[1] == ("delta", {"text": answer_part_1})
        assert events[2] == ("delta", {"text": answer_part_2})
        assert events[3][0] == "done"
        assert events[3][1] == {
            "conversation_id": "conv-abc",
            "message_id": "msg-xyz",
            "answer": answer_part_1 + answer_part_2,
            "citations": [
                {
                    "document": "售后制度",
                    "clause": "3.2",
                    "title": "质量问题退货期限",
                    "content": (
                        "售后制度 第 3.2 条 质量问题退货期限\n"
                        "规则：已签收天数不超过 30 天可以退货退款。"
                    ),
                    "score": 0.95,
                }
            ],
            "trace": [{"tool": "get_order", "kind": "ok", "http_status": 200}],
        }

        # Verify structured logs contain http_request & dify_call without secrets/text
        parsed_logs = [json.loads(line) for line in log_handler.records]
        all_logs_text = "\n".join(log_handler.records)
        assert raw_token not in all_logs_text
        assert TEST_DIFY_API_KEY not in all_logs_text
        assert query_text not in all_logs_text
        assert answer_part_1 not in all_logs_text

        http_logs = [entry for entry in parsed_logs if entry.get("event") == "http_request"]
        assert len(http_logs) == 1
        assert http_logs[0]["request_id"] == "req-stream-1"
        assert http_logs[0]["sub"] == "u1001"
        assert http_logs[0]["status"] == 200
        assert http_logs[0]["path"] == "/chat/api/messages"

        dify_logs = [entry for entry in parsed_logs if entry.get("event") == "dify_call"]
        assert len(dify_logs) == 1
        assert dify_logs[0]["request_id"] == "req-stream-1"
        assert dify_logs[0]["conversation_id"] == "conv-abc"
        assert dify_logs[0]["status"] == 200
    finally:
        _detach_log_capture(log_handler)


def test_messages_non_streaming_aggregation_and_conversation_continuation(
    client: TestClient,
    fake_dify: FakeDify,
    make_token: Callable[..., str],
) -> None:
    raw_token = make_token("li")
    fake_dify.default_events = [
        {
            "event": "node_finished",
            "conversation_id": CONV_ID_PREV,
            "message_id": "msg-2",
            "data": {
                "outputs": {
                    "call_summary": {
                        "tool": "get_order",
                        "kind": "ok",
                        "http_status": 200,
                    }
                }
            },
        },
        {
            "event": "node_finished",
            "conversation_id": CONV_ID_PREV,
            "message_id": "msg-2",
            "data": {
                "outputs": {
                    "call_summary": {
                        "tool": "create_refund",
                        "kind": "pending",
                        "http_status": 202,
                        "approval_id": "AP000001",
                        "user_token": raw_token,
                    }
                }
            },
        },
        {
            "event": "message",
            "conversation_id": CONV_ID_PREV,
            "message_id": "msg-2",
            "answer": "已提交退款申请 AP000001，等待主管审批。",
        },
        {
            "event": "message_end",
            "conversation_id": CONV_ID_PREV,
            "message_id": "msg-2",
            "metadata": {"retriever_resources": []},
        },
    ]

    resp = client.post(
        "/chat/api/messages",
        json={"query": "帮他把款退了", "conversation_id": CONV_ID_PREV, "stream": False},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data == {
        "conversation_id": CONV_ID_PREV,
        "message_id": "msg-2",
        "answer": "已提交退款申请 AP000001，等待主管审批。",
        "citations": [],
        "trace": [
            {"tool": "get_order", "kind": "ok", "http_status": 200},
            {
                "tool": "create_refund",
                "kind": "pending",
                "http_status": 202,
                "approval_id": "AP000001",
            },
        ],
    }
    assert raw_token not in resp.text
    assert TEST_DIFY_API_KEY not in resp.text
    assert fake_dify.requests[0].json()["conversation_id"] == CONV_ID_PREV
    assert fake_dify.requests[0].json()["user"] == "u2001"
    assert fake_dify.requests[0].json()["response_mode"] == "streaming"


def test_messages_dify_error_cases(
    client: TestClient,
    build_app: Callable[..., Any],
    fake_dify: FakeDify,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. Dify returns HTTP 500 -> 502 dify_error
    fake_dify.chat_handler = lambda _req: httpx.Response(500, text="dify internal error")
    r_500 = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": True},
        headers=auth_headers("wang"),
    )
    assert r_500.status_code == 502
    assert r_500.json()["code"] == "dify_error"

    # 2. Dify unreachable -> 502 dify_error
    def _connect_err(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dify down")

    fake_dify.chat_handler = _connect_err
    r_conn = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_conn.status_code == 502
    assert r_conn.json()["code"] == "dify_error"

    # 3. Stream emits workflow_finished failed: stream=false -> 502; stream=true -> SSE error event
    fake_dify.chat_handler = None
    fake_dify.default_events = [
        {"event": "workflow_finished", "data": {"status": "failed", "error": "boom"}},
    ]
    r_ns_fail = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_ns_fail.status_code == 502
    assert r_ns_fail.json()["code"] == "dify_error"

    r_s_fail = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": True},
        headers=auth_headers("wang"),
    )
    assert r_s_fail.status_code == 200
    assert _parse_sse(r_s_fail.text) == [
        ("error", {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"})
    ]

    # 4. Incomplete stream (no message_end) -> stream=false 502; stream=true SSE error
    fake_dify.default_events = [{"event": "message", "answer": "半截回答"}]
    r_inc_ns = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_inc_ns.status_code == 502
    assert r_inc_ns.json()["code"] == "dify_error"

    r_inc_s = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": True},
        headers=auth_headers("wang"),
    )
    assert r_inc_s.status_code == 200
    parsed_inc = _parse_sse(r_inc_s.text)
    assert parsed_inc[-1] == (
        "error",
        {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"},
    )

    # 5. Malformed SSE JSON -> stream=false 502; stream=true SSE error
    fake_dify.chat_handler = lambda _req: httpx.Response(
        200,
        content=b"data: {not-valid-json\n\n",
        headers={"Content-Type": "text/event-stream"},
    )
    r_bad_ns = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_bad_ns.status_code == 502
    assert r_bad_ns.json()["code"] == "dify_error"

    r_bad_s = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": True},
        headers=auth_headers("wang"),
    )
    assert r_bad_s.status_code == 200
    assert _parse_sse(r_bad_s.text) == [
        ("error", {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"})
    ]

    # 6. Non-dict SSE JSON -> 502
    fake_dify.chat_handler = lambda _req: httpx.Response(
        200,
        content=b"data: [1, 2, 3]\n\n",
        headers={"Content-Type": "text/event-stream"},
    )
    r_list_ns = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_list_ns.status_code == 502

    # 7. Mid-stream network disconnect -> SSE error event
    first_chunk = encode_sse_events([{"event": "message", "answer": "部分"}])
    fake_dify.chat_handler = lambda _req: httpx.Response(
        200,
        stream=FailingByteStream(first_chunk),
        headers={"Content-Type": "text/event-stream"},
    )
    r_drop = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": True},
        headers=auth_headers("wang"),
    )
    assert r_drop.status_code == 200
    drop_events = _parse_sse(r_drop.text)
    assert drop_events[0] == ("delta", {"text": "部分"})
    assert drop_events[1] == (
        "error",
        {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"},
    )

    # 8. Unconfigured CHAT_DIFY_API_KEY -> 503 dify_not_configured
    app_no_key = build_app(custom_settings=Settings(dify_api_key=""))
    with TestClient(app_no_key) as tc:
        r_503 = tc.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": False},
            headers=auth_headers("wang"),
        )
        assert r_503.status_code == 503
        assert r_503.json()["code"] == "dify_not_configured"


def test_messages_validation_errors(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    r_empty = client.post(
        "/chat/api/messages",
        json={"query": "   ", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_empty.status_code == 422
    assert r_empty.json()["code"] == "validation_error"
    assert r_empty.json()["details"][0]["field"] == "query"

    r_long = client.post(
        "/chat/api/messages",
        json={"query": "a" * 2001, "stream": False},
        headers=auth_headers("wang"),
    )
    assert r_long.status_code == 422
    assert r_long.json()["code"] == "validation_error"

    r_extra = client.post(
        "/chat/api/messages",
        json={"query": "ok", "unexpected": 1},
        headers=auth_headers("wang"),
    )
    assert r_extra.status_code == 422
    assert r_extra.json()["code"] == "validation_error"

    # Non-UUID conversation_id -> 422 validation_error (G8 structure)
    for bad_conv in ("not-a-uuid", "conv-1", "11111111111141118111111111111111"):
        r_bad_uuid = client.post(
            "/chat/api/messages",
            json={"query": "hello", "conversation_id": bad_conv, "stream": False},
            headers=auth_headers("wang"),
        )
        assert r_bad_uuid.status_code == 422
        body = r_bad_uuid.json()
        assert set(body.keys()) >= {"code", "message", "request_id", "details"}
        assert body["code"] == "validation_error"
        assert body["request_id"] == r_bad_uuid.headers["X-Request-Id"]
        assert body["details"][0]["field"] == "conversation_id"

    # Empty / None conversation_id treated as new conversation (200)
    for empty_conv in (None, "", "   "):
        r_ok = client.post(
            "/chat/api/messages",
            json={"query": "hello", "conversation_id": empty_conv, "stream": False},
            headers=auth_headers("wang"),
        )
        assert r_ok.status_code == 200


def test_conversation_history_strips_inputs_and_handles_errors(
    client: TestClient,
    build_app: Callable[..., Any],
    fake_dify: FakeDify,
    make_token: Callable[..., str],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    raw_token = make_token("wang")
    fake_dify.messages_handler = lambda _req: httpx.Response(
        200,
        json={
            "limit": 100,
            "has_more": False,
            "data": [
                {
                    "id": "m-1",
                    "conversation_id": CONV_ID_HIST,
                    "inputs": {"user_token": raw_token, "extra": "secret"},
                    "query": "订单 A1023 能退吗？",
                    "answer": "可以退货退款。",
                    "created_at": 1795060800,
                    "agent_thoughts": [{"thought": "internal"}],
                    "message_files": [],
                    "retriever_resources": [
                        {
                            "document_name": "售后制度.md",
                            "content": "售后制度 第 3.2 条 质量问题退货期限\n规则：30 天内可退。",
                            "score": 0.9,
                        }
                    ],
                },
                {
                    "id": "m-2",
                    "inputs": {"user_token": raw_token},
                    "query": "谢谢",
                    "answer": "不客气",
                    "created_at": "2026-11-20T12:05:00+08:00",
                    "retriever_resources": [],
                },
                "ignore-non-dict",
            ],
        },
    )

    resp = client.get(
        f"/chat/api/conversations/{CONV_ID_HIST}/messages",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200
    assert raw_token not in resp.text
    assert TEST_DIFY_API_KEY not in resp.text
    assert "agent_thoughts" not in resp.text
    assert "inputs" not in resp.text

    body = resp.json()
    assert body["conversation_id"] == CONV_ID_HIST
    assert len(body["messages"]) == 2
    assert body["messages"][0]["id"] == "m-1"
    assert body["messages"][0]["query"] == "订单 A1023 能退吗？"
    assert body["messages"][0]["answer"] == "可以退货退款。"
    assert body["messages"][0]["created_at"].endswith("+08:00")
    assert body["messages"][0]["citations"][0]["clause"] == "3.2"
    assert body["messages"][1]["created_at"] == "2026-11-20T12:05:00+08:00"

    assert fake_dify.requests[-1].path == "/v1/messages"
    assert fake_dify.requests[-1].params == {
        "conversation_id": CONV_ID_HIST,
        "user": "u1001",
        "limit": "100",
    }
    assert fake_dify.requests[-1].headers["authorization"] == f"Bearer {TEST_DIFY_API_KEY}"

    # Blank or non-UUID conversation_id -> 422 validation_error
    for bad_path_id in ("%20%20", "not-a-uuid", "conv-1"):
        r_bad = client.get(
            f"/chat/api/conversations/{bad_path_id}/messages",
            headers=auth_headers("wang"),
        )
        assert r_bad.status_code == 422
        bad_body = r_bad.json()
        assert bad_body["code"] == "validation_error"
        assert bad_body["request_id"] == r_bad.headers["X-Request-Id"]
        assert bad_body["details"][0]["field"] == "conversation_id"

    # Dify 404 -> 404 conversation_not_found (G8 three-key structure)
    fake_dify.messages_handler = lambda _req: httpx.Response(
        404,
        json={"code": "not_found", "message": "Conversation Not Exists.", "status": 404},
    )
    r_404 = client.get(
        f"/chat/api/conversations/{CONV_ID_MISSING}/messages",
        headers=auth_headers("wang"),
    )
    assert r_404.status_code == 404
    body_404 = r_404.json()
    assert set(body_404.keys()) == {"code", "message", "request_id"}
    assert body_404["code"] == "conversation_not_found"
    assert "不存在" in body_404["message"]
    assert body_404["request_id"] == r_404.headers["X-Request-Id"]

    # Dify 500 -> 502 dify_error
    fake_dify.messages_handler = lambda _req: httpx.Response(500, text="err")
    r_500 = client.get(
        f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
        headers=auth_headers("wang"),
    )
    assert r_500.status_code == 502
    assert r_500.json()["code"] == "dify_error"

    # Invalid JSON / unexpected structure -> 502
    fake_dify.messages_handler = lambda _req: httpx.Response(200, text="not-json")
    assert (
        client.get(
            f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
            headers=auth_headers("wang"),
        ).status_code
        == 502
    )
    fake_dify.messages_handler = lambda _req: httpx.Response(200, json={"data": "not-a-list"})
    assert (
        client.get(
            f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
            headers=auth_headers("wang"),
        ).status_code
        == 502
    )

    # Connection error -> 502
    def _raise_conn(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    fake_dify.messages_handler = _raise_conn
    assert (
        client.get(
            f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
            headers=auth_headers("wang"),
        ).status_code
        == 502
    )

    # Unconfigured API key -> 503
    app_no_key = build_app(custom_settings=Settings(dify_api_key=""))
    with TestClient(app_no_key) as tc:
        r_503 = tc.get(
            f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
            headers=auth_headers("wang"),
        )
        assert r_503.status_code == 503
        assert r_503.json()["code"] == "dify_not_configured"


def test_unexpected_exception_and_validation_helper(
    build_app: Callable[..., Any],
) -> None:
    app = build_app()

    @app.get("/_test_boom")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("sensitive internal error detail")

    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.get("/_test_boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "internal_error"
        assert "sensitive internal error detail" not in resp.text

    with pytest.raises(Exception) as exc_info:
        MessageRequest(query="")
    details = validation_details_from_pydantic(exc_info.value)  # type: ignore[arg-type]
    assert details[0]["field"] == "query"


def test_default_factory_lifespan() -> None:
    from app.main import create_app

    app = create_app(settings=Settings(dify_api_key=""))
    with TestClient(app) as tc:
        assert tc.get("/healthz").status_code == 200


@pytest.mark.parametrize("stream", [True, False])
def test_h2_conversation_not_found_resets_and_retries_once(
    client: TestClient,
    fake_dify: FakeDify,
    make_token: Callable[..., str],
    stream: bool,
) -> None:
    raw_token = make_token("zhao")
    attempt = 0

    def _chat_handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return httpx.Response(
                404,
                json={"code": "not_found", "message": "Conversation Not Exists.", "status": 404},
            )
        events = [
            {
                "event": "message",
                "conversation_id": CONV_ID_NEW,
                "message_id": "msg-new-1",
                "answer": "请提供具体订单号。",
            },
            {
                "event": "workflow_finished",
                "conversation_id": CONV_ID_NEW,
                "message_id": "msg-new-1",
                "data": {"status": "succeeded"},
            },
            {
                "event": "message_end",
                "conversation_id": CONV_ID_NEW,
                "message_id": "msg-new-1",
                "metadata": {"retriever_resources": []},
            },
        ]
        return httpx.Response(
            200,
            content=encode_sse_events(events),
            headers={"Content-Type": "text/event-stream"},
        )

    fake_dify.chat_handler = _chat_handler
    req_id = f"req-h2-{'stream' if stream else 'json'}"
    log_handler = _attach_log_capture()
    try:
        resp = client.post(
            "/chat/api/messages",
            json={
                "query": "那帮他把款退了。",
                "conversation_id": CONV_ID_STALE,
                "stream": stream,
            },
            headers={
                "Authorization": f"Bearer {raw_token}",
                "X-Request-Id": req_id,
            },
        )
        assert resp.status_code == 200
        if stream:
            events = _parse_sse(resp.text)
            done_events = [data for ev, data in events if ev == "done"]
            assert len(done_events) == 1
            assert done_events[0]["conversation_id"] == CONV_ID_NEW
            assert done_events[0]["answer"] == "请提供具体订单号。"
        else:
            body = resp.json()
            assert body["conversation_id"] == CONV_ID_NEW
            assert body["answer"] == "请提供具体订单号。"

        # Exactly two calls to Dify: first with old conversation_id, retry without it
        assert len(fake_dify.requests) == 2
        first_body = fake_dify.requests[0].json()
        retry_body = fake_dify.requests[1].json()
        assert first_body["conversation_id"] == CONV_ID_STALE
        assert "conversation_id" not in retry_body
        assert retry_body["inputs"]["user_token"] == raw_token
        assert retry_body["user"] == "u1002"
        assert retry_body["query"] == "那帮他把款退了。"
        assert retry_body["response_mode"] == "streaming"

        # Structured conversation_reset log without token
        parsed_logs = [json.loads(line) for line in log_handler.records]
        all_logs_text = "\n".join(log_handler.records)
        assert raw_token not in all_logs_text
        reset_logs = [entry for entry in parsed_logs if entry.get("event") == "conversation_reset"]
        assert len(reset_logs) == 1
        assert reset_logs[0]["request_id"] == req_id
        assert reset_logs[0]["sub"] == "u1002"
        assert reset_logs[0]["old_conversation_id"] == CONV_ID_STALE
    finally:
        _detach_log_capture(log_handler)


@pytest.mark.parametrize("stream", [True, False])
def test_h2_retry_failure_and_non_retryable_404_return_502(
    client: TestClient,
    fake_dify: FakeDify,
    auth_headers: Callable[..., dict[str, str]],
    stream: bool,
) -> None:
    # 1. Both attempts return 404 -> retries only once, then returns 502 dify_error
    fake_dify.requests.clear()
    fake_dify.chat_handler = lambda _req: httpx.Response(
        404,
        json={"code": "not_found", "message": "Conversation Not Exists."},
    )
    r_double_404 = client.post(
        "/chat/api/messages",
        json={"query": "hello", "conversation_id": CONV_ID_STALE, "stream": stream},
        headers=auth_headers("wang"),
    )
    assert r_double_404.status_code == 502
    assert r_double_404.json()["code"] == "dify_error"
    assert len(fake_dify.requests) == 2

    # 2. First 404, retry 500 -> returns 502 dify_error
    fake_dify.requests.clear()
    calls = 0

    def _404_then_500(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                404,
                json={"code": "not_found", "message": "Conversation Not Exists."},
            )
        return httpx.Response(500, json={"code": "internal_server_error", "message": "boom"})

    fake_dify.chat_handler = _404_then_500
    r_404_500 = client.post(
        "/chat/api/messages",
        json={"query": "hello", "conversation_id": CONV_ID_STALE, "stream": stream},
        headers=auth_headers("wang"),
    )
    assert r_404_500.status_code == 502
    assert r_404_500.json()["code"] == "dify_error"
    assert len(fake_dify.requests) == 2

    # 3. 404 without conversation_id -> must NOT retry
    fake_dify.requests.clear()
    fake_dify.chat_handler = lambda _req: httpx.Response(
        404, json={"code": "not_found", "message": "Conversation Not Exists."}
    )
    r_no_conv = client.post(
        "/chat/api/messages",
        json={"query": "hello", "stream": stream},
        headers=auth_headers("wang"),
    )
    assert r_no_conv.status_code == 502
    assert len(fake_dify.requests) == 1

    # 4. 404 for Workflow not found (not conversation) -> must NOT retry
    fake_dify.requests.clear()
    fake_dify.chat_handler = lambda _req: httpx.Response(
        404, json={"code": "not_found", "message": "Workflow not found"}
    )
    r_wf_404 = client.post(
        "/chat/api/messages",
        json={"query": "hello", "conversation_id": CONV_ID_STALE, "stream": stream},
        headers=auth_headers("wang"),
    )
    assert r_wf_404.status_code == 502
    assert len(fake_dify.requests) == 1

    # 5. L7: Route-level 404 (code=not_found, message without "conversation") -> must NOT retry
    fake_dify.requests.clear()
    fake_dify.chat_handler = lambda _req: httpx.Response(
        404,
        json={
            "code": "not_found",
            "message": "The requested URL was not found on the server.",
            "status": 404,
        },
    )
    log_handler = _attach_log_capture()
    try:
        r_route_404 = client.post(
            "/chat/api/messages",
            json={"query": "hello", "conversation_id": CONV_ID_STALE, "stream": stream},
            headers={**auth_headers("wang"), "X-Request-Id": f"req-l7-route-{stream}"},
        )
        assert r_route_404.status_code == 502
        assert r_route_404.json()["code"] == "dify_error"
        assert len(fake_dify.requests) == 1

        parsed_logs = [json.loads(line) for line in log_handler.records]
        assert not any(entry.get("event") == "conversation_reset" for entry in parsed_logs)
        dify_logs = [
            entry
            for entry in parsed_logs
            if entry.get("event") == "dify_call"
            and entry.get("request_id") == f"req-l7-route-{stream}"
        ]
        assert len(dify_logs) == 1
        assert "The requested URL was not found" in (dify_logs[0].get("error_detail") or "")
    finally:
        _detach_log_capture(log_handler)

    # 6. L5: 404 where message lacks "conversation" but other JSON fields (url/detail) or
    # non-JSON body contain "conversation" -> must NOT retry, return 502 dify_error
    for other_404_resp in [
        httpx.Response(
            404,
            json={
                "code": "not_found",
                "message": "The requested URL was not found on the server.",
                "url": f"/v1/chat-messages?conversation_id={CONV_ID_STALE}",
                "detail": "conversation route not mounted",
            },
        ),
        httpx.Response(
            404,
            json={"code": "conversation_not_found", "message": "Not found"},
        ),
        httpx.Response(
            404,
            text=(
                "<html><body>404 Not Found: "
                f"/v1/chat-messages?conversation_id={CONV_ID_STALE}</body></html>"
            ),
        ),
    ]:
        fake_dify.requests.clear()
        fake_dify.chat_handler = lambda _req, resp=other_404_resp: resp
        r_non_conv = client.post(
            "/chat/api/messages",
            json={"query": "hello", "conversation_id": CONV_ID_STALE, "stream": stream},
            headers=auth_headers("wang"),
        )
        assert r_non_conv.status_code == 502
        assert r_non_conv.json()["code"] == "dify_error"
        assert len(fake_dify.requests) == 1


def test_l7_history_route_level_404_returns_502_not_conversation_not_found(
    client: TestClient,
    fake_dify: FakeDify,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Route-level 404 (code=not_found without "conversation") -> 502 dify_error
    fake_dify.messages_handler = lambda _req: httpx.Response(
        404,
        json={
            "code": "not_found",
            "message": "The requested URL was not found on the server.",
            "status": 404,
        },
    )
    r_route = client.get(
        f"/chat/api/conversations/{CONV_ID_MISSING}/messages",
        headers=auth_headers("wang"),
    )
    assert r_route.status_code == 502
    assert r_route.json()["code"] == "dify_error"

    # Plain HTML 404 echoing conversation_id in URL -> 502 dify_error
    fake_dify.messages_handler = lambda _req: httpx.Response(
        404,
        text=(
            "<html><body>404 Not Found: "
            f"/v1/messages?conversation_id={CONV_ID_MISSING}</body></html>"
        ),
    )
    r_html = client.get(
        f"/chat/api/conversations/{CONV_ID_MISSING}/messages",
        headers=auth_headers("wang"),
    )
    assert r_html.status_code == 502
    assert r_html.json()["code"] == "dify_error"

    # L5: 404 JSON where message lacks "conversation" even if url/detail/code contain it -> 502
    for non_conv_body in [
        {
            "code": "not_found",
            "message": "The requested URL was not found.",
            "url": f"/v1/messages?conversation_id={CONV_ID_MISSING}",
            "detail": "conversation endpoint missing",
        },
        {"code": "conversation_not_found", "message": "Not found"},
    ]:
        fake_dify.messages_handler = lambda _req, body=non_conv_body: httpx.Response(404, json=body)
        r_body = client.get(
            f"/chat/api/conversations/{CONV_ID_MISSING}/messages",
            headers=auth_headers("wang"),
        )
        assert r_body.status_code == 502
        assert r_body.json()["code"] == "dify_error"

    # Real Dify 404 with "conversation" in parsed message -> 404 conversation_not_found
    fake_dify.messages_handler = lambda _req: httpx.Response(
        404,
        json={"code": "not_found", "message": "Conversation Not Exists.", "status": 404},
    )
    r_conv = client.get(
        f"/chat/api/conversations/{CONV_ID_MISSING}/messages",
        headers=auth_headers("wang"),
    )
    assert r_conv.status_code == 404
    assert r_conv.json()["code"] == "conversation_not_found"


def test_l6_raw_dify_errors_redacted_truncated_logged_and_not_leaked(
    client: TestClient,
    fake_dify: FakeDify,
    make_token: Callable[..., str],
) -> None:
    raw_token = make_token("wang")
    long_tail = "Z" * 700
    raw_secret_msg = (
        f"upstream failure auth=Bearer {raw_token} key={TEST_DIFY_API_KEY} detail={long_tail}"
    )

    log_handler = _attach_log_capture()
    try:
        # 1. Non-2xx HTTP response from POST /v1/chat-messages
        fake_dify.chat_handler = lambda _req: httpx.Response(
            500,
            json={"code": "provider_error", "message": raw_secret_msg},
        )
        r_http = client.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": False},
            headers={"Authorization": f"Bearer {raw_token}", "X-Request-Id": "req-l6-http"},
        )
        assert r_http.status_code == 502
        assert r_http.json()["code"] == "dify_error"
        assert "upstream failure" not in r_http.text
        assert raw_token not in r_http.text
        assert TEST_DIFY_API_KEY not in r_http.text

        # 2. Non-2xx HTTP response from GET /v1/messages
        fake_dify.messages_handler = lambda _req: httpx.Response(
            500,
            text=raw_secret_msg,
        )
        r_hist = client.get(
            f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
            headers={"Authorization": f"Bearer {raw_token}", "X-Request-Id": "req-l6-hist"},
        )
        assert r_hist.status_code == 502
        assert "upstream failure" not in r_hist.text
        assert raw_token not in r_hist.text

        # 3. SSE error event (stream=True and stream=False)
        fake_dify.chat_handler = None
        fake_dify.default_events = [
            {
                "event": "error",
                "status": 500,
                "code": "model_error",
                "message": raw_secret_msg,
            }
        ]
        r_err_stream = client.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": True},
            headers={"Authorization": f"Bearer {raw_token}", "X-Request-Id": "req-l6-sse-err"},
        )
        assert r_err_stream.status_code == 200
        assert _parse_sse(r_err_stream.text) == [
            ("error", {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"})
        ]
        assert "upstream failure" not in r_err_stream.text
        assert raw_token not in r_err_stream.text

        r_err_ns = client.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": False},
            headers={"Authorization": f"Bearer {raw_token}", "X-Request-Id": "req-l6-ns-err"},
        )
        assert r_err_ns.status_code == 502
        assert "upstream failure" not in r_err_ns.text
        assert raw_token not in r_err_ns.text

        # 4. SSE workflow_finished.data.error (stream=True and stream=False)
        fake_dify.default_events = [
            {
                "event": "workflow_finished",
                "data": {"status": "failed", "error": raw_secret_msg},
            }
        ]
        r_wf_stream = client.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": True},
            headers={"Authorization": f"Bearer {raw_token}", "X-Request-Id": "req-l6-sse-wf"},
        )
        assert r_wf_stream.status_code == 200
        assert _parse_sse(r_wf_stream.text) == [
            ("error", {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"})
        ]
        assert "upstream failure" not in r_wf_stream.text
        assert raw_token not in r_wf_stream.text

        r_wf_ns = client.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": False},
            headers={"Authorization": f"Bearer {raw_token}", "X-Request-Id": "req-l6-ns-wf"},
        )
        assert r_wf_ns.status_code == 502
        assert "upstream failure" not in r_wf_ns.text
        assert raw_token not in r_wf_ns.text

        # Verify all dify_call structured logs have truncated, JWT-redacted error_detail
        all_logs_text = "\n".join(log_handler.records)
        assert raw_token not in all_logs_text
        assert TEST_DIFY_API_KEY not in all_logs_text

        parsed = [json.loads(line) for line in log_handler.records]
        by_req = {
            entry["request_id"]: entry for entry in parsed if entry.get("event") == "dify_call"
        }
        for rid in (
            "req-l6-http",
            "req-l6-hist",
            "req-l6-sse-err",
            "req-l6-ns-err",
            "req-l6-sse-wf",
            "req-l6-ns-wf",
        ):
            assert rid in by_req, f"missing dify_call log for {rid}"
            detail = by_req[rid].get("error_detail")
            assert isinstance(detail, str) and detail
            assert len(detail) <= MAX_ERROR_DETAIL_LENGTH
            assert detail.endswith("...")
            assert "[REDACTED_JWT]" in detail
            assert "[REDACTED_KEY]" in detail
            assert raw_token not in detail
            assert TEST_DIFY_API_KEY not in detail
    finally:
        _detach_log_capture(log_handler)

    # Unit checks for sanitize_error_detail edge cases
    assert sanitize_error_detail(None) is None
    assert sanitize_error_detail("   ") is None
    assert sanitize_error_detail(b"raw bytes error") == "raw bytes error"
    assert sanitize_error_detail({"k": "v"}) == '{"k": "v"}'
    assert sanitize_error_detail("abcdef", max_length=3) == "abc"


class _ContinuousPingStream(httpx.AsyncByteStream):
    def __init__(self, interval_seconds: float = 0.02) -> None:
        self._interval = interval_seconds
        self.closed = False
        self.pings_sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.pings_sent += 1
            yield b"event: ping\n\n"
            await asyncio.sleep(self._interval)

    async def aclose(self) -> None:
        self.closed = True


class _SilentHangStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("stream", [True, False])
def test_m7_overall_timeout_on_continuous_pings_closes_upstream(
    build_app: Callable[..., Any],
    auth_headers: Callable[..., dict[str, str]],
    stream: bool,
) -> None:
    created_streams: list[_ContinuousPingStream] = []

    def _ping_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/chat-messages":
            ping_stream = _ContinuousPingStream(interval_seconds=0.02)
            created_streams.append(ping_stream)
            return httpx.Response(
                200,
                stream=ping_stream,
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(404)

    custom_dify = FakeDify(chat_handler=_ping_handler)
    app = build_app(
        custom_settings=Settings(
            dify_api_key=TEST_DIFY_API_KEY,
            dify_timeout_seconds=0.15,
        ),
        custom_dify=custom_dify,
    )

    log_handler = _attach_log_capture()
    try:
        with TestClient(app) as tc:
            req_id = f"req-m7-ping-{'stream' if stream else 'json'}"
            t0 = time.monotonic()
            resp = tc.post(
                "/chat/api/messages",
                json={"query": "请帮我查一下订单", "stream": stream},
                headers={**auth_headers("wang"), "X-Request-Id": req_id},
            )
            elapsed = time.monotonic() - t0

        assert elapsed < 2.0
        assert len(created_streams) == 1
        assert created_streams[0].pings_sent >= 2
        assert created_streams[0].closed is True

        if stream:
            assert resp.status_code == 200
            assert _parse_sse(resp.text) == [
                ("error", {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"})
            ]
        else:
            assert resp.status_code == 502
            assert resp.json()["code"] == "dify_error"

        parsed = [json.loads(line) for line in log_handler.records]
        dify_logs = [
            entry
            for entry in parsed
            if entry.get("event") == "dify_call" and entry.get("request_id") == req_id
        ]
        assert len(dify_logs) == 1
        assert dify_logs[0]["error"] == "timeout"
        assert dify_logs[0]["duration_ms"] >= 100
    finally:
        _detach_log_capture(log_handler)


def test_m7_silent_stream_and_send_timeout_and_retry_deadline(
    build_app: Callable[..., Any],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. Silent hanging stream triggers timeout and closes upstream
    hang_stream = _SilentHangStream()
    dify_hang = FakeDify(
        chat_handler=lambda _req: httpx.Response(
            200,
            stream=hang_stream,
            headers={"Content-Type": "text/event-stream"},
        )
    )
    app_hang = build_app(
        custom_settings=Settings(
            dify_api_key=TEST_DIFY_API_KEY,
            dify_timeout_seconds=0.12,
        ),
        custom_dify=dify_hang,
    )
    with TestClient(app_hang) as tc:
        r_hang = tc.post(
            "/chat/api/messages",
            json={"query": "hi", "stream": True},
            headers=auth_headers("wang"),
        )
    assert r_hang.status_code == 200
    assert _parse_sse(r_hang.text) == [
        ("error", {"code": "dify_error", "message": "助手暂时不可用，请稍后再试。"})
    ]
    assert hang_stream.closed is True

    # 2. Request send timeout on POST /v1/chat-messages and GET /v1/messages -> 502 + error=timeout
    def _raise_timeout(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out")

    dify_timeout = FakeDify(chat_handler=_raise_timeout, messages_handler=_raise_timeout)
    app_timeout = build_app(
        custom_settings=Settings(
            dify_api_key=TEST_DIFY_API_KEY,
            dify_timeout_seconds=0.2,
        ),
        custom_dify=dify_timeout,
    )
    log_handler = _attach_log_capture()
    try:
        with TestClient(app_timeout) as tc:
            r_post = tc.post(
                "/chat/api/messages",
                json={"query": "hi", "stream": False},
                headers={**auth_headers("wang"), "X-Request-Id": "req-m7-send-to"},
            )
            assert r_post.status_code == 502
            assert r_post.json()["code"] == "dify_error"

            r_get = tc.get(
                f"/chat/api/conversations/{CONV_ID_OTHER}/messages",
                headers={**auth_headers("wang"), "X-Request-Id": "req-m7-hist-to"},
            )
            assert r_get.status_code == 502
            assert r_get.json()["code"] == "dify_error"

        parsed = [json.loads(line) for line in log_handler.records]
        timeout_reqs = {
            entry["request_id"]
            for entry in parsed
            if entry.get("event") == "dify_call" and entry.get("error") == "timeout"
        }
        assert "req-m7-send-to" in timeout_reqs
        assert "req-m7-hist-to" in timeout_reqs
    finally:
        _detach_log_capture(log_handler)

    # 3. L9: H2 retry shares exact same overall deadline as initial attempt
    # (initial 404 takes 0.15s, total timeout 0.20s -> retry closes by ~0.20s, strictly < 0.30s,
    # whereas a fresh deadline on retry would take >= 0.35s)
    retry_stream = _ContinuousPingStream(interval_seconds=0.01)
    attempt = 0

    async def _async_retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            await asyncio.sleep(0.15)
            return httpx.Response(
                404,
                json={"code": "not_found", "message": "Conversation Not Exists."},
            )
        return httpx.Response(
            200,
            stream=retry_stream,
            headers={"Content-Type": "text/event-stream"},
        )

    from app.dify import DifyClient, DifyOpenedStream
    from app.main import create_app

    recorded_deadlines: list[float | None] = []

    class _DeadlineRecordingDifyClient(DifyClient):
        async def open_chat_stream(self, **kwargs: Any) -> DifyOpenedStream:
            recorded_deadlines.append(kwargs.get("deadline"))
            return await super().open_chat_stream(**kwargs)

    cfg = Settings(dify_api_key=TEST_DIFY_API_KEY, dify_timeout_seconds=0.2)
    base_app = build_app(custom_settings=cfg)
    shared_deadline_app = create_app(
        settings=cfg,
        signing_key_resolver=base_app.state.signing_key_resolver,
        dify_client=_DeadlineRecordingDifyClient(
            base_url=cfg.dify_base_url,
            api_key=cfg.dify_api_key,
            timeout_seconds=cfg.dify_timeout_seconds,
            transport=httpx.MockTransport(_async_retry_handler),
        ),
    )
    with TestClient(shared_deadline_app) as tc:
        t0 = time.monotonic()
        r_retry_to = tc.post(
            "/chat/api/messages",
            json={"query": "hi", "conversation_id": CONV_ID_STALE, "stream": False},
            headers=auth_headers("wang"),
        )
        elapsed = time.monotonic() - t0
    assert r_retry_to.status_code == 502
    assert retry_stream.closed is True
    assert len(recorded_deadlines) == 2
    assert recorded_deadlines[0] is not None
    assert recorded_deadlines[1] == recorded_deadlines[0]
    assert elapsed < 0.30


def test_chat_js_clears_stale_conversation_on_404(client: TestClient) -> None:
    r_js = client.get("/static/chat.js")
    assert r_js.status_code == 200
    assert "resp.status === 404" in r_js.text
    assert "resetConversationView({ clearSaved: true })" in r_js.text
