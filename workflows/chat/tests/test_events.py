from __future__ import annotations

import json

from app.events import (
    DIFY_ERROR_EVENT_PAYLOAD,
    DifyStreamProcessor,
    format_sse,
    parse_citations,
    sanitize_call_summary,
)


def test_parse_citations_standard_and_edge_cases() -> None:
    resources = [
        {
            "document_name": "售后制度.md",
            "content": "售后制度 第 3.2 条 质量问题退货期限\n适用：质量问题\n规则：30 天内可退。",
            "score": 0.92,
            "dataset_name": "售后制度",
        },
        {
            "document_name": "客服操作手册.MD",
            "content": "## 客服操作手册 第 2.4 条 进度查询\n操作步骤：问助手。",
            "score": 0.8,
        },
        {
            "document_name": "售后制度.md",
            "content": "《售后制度》第 5.4 条 不得重复退款\n规则：已有处理中退款不得重复提交。",
            "score": None,
        },
        {
            "document_name": "售后制度.md",
            "content": "售后制度 第 7.1 条\n所有退款须经主管审批。",
        },
        {
            "document_name": "售后制度.md",
            "content": "售后制度\n本制度适用于全部区域订单，不含条款编号。",
            "score": True,  # bool must not be treated as numeric score
        },
        {
            "content": "故障排查手册 第 1.1 条 蓝牙耳机无声\n请先重启耳机。",
            "score": 0.75,
        },
        "not-a-dict",
        {},
    ]

    result = parse_citations(resources)
    assert len(result) == 6

    assert result[0] == {
        "document": "售后制度",
        "clause": "3.2",
        "title": "质量问题退货期限",
        "content": "售后制度 第 3.2 条 质量问题退货期限\n适用：质量问题\n规则：30 天内可退。",
        "score": 0.92,
    }
    assert result[1] == {
        "document": "客服操作手册",
        "clause": "2.4",
        "title": "进度查询",
        "content": "## 客服操作手册 第 2.4 条 进度查询\n操作步骤：问助手。",
        "score": 0.8,
    }
    assert result[2] == {
        "document": "售后制度",
        "clause": "5.4",
        "title": "不得重复退款",
        "content": "《售后制度》第 5.4 条 不得重复退款\n规则：已有处理中退款不得重复提交。",
    }
    assert "score" not in result[2]

    assert result[3] == {
        "document": "售后制度",
        "clause": "7.1",
        "content": "售后制度 第 7.1 条\n所有退款须经主管审批。",
    }
    assert "title" not in result[3]

    # Unparseable header: clause & title omitted (no nulls per G6)
    assert result[4] == {
        "document": "售后制度",
        "content": "售后制度\n本制度适用于全部区域订单，不含条款编号。",
    }
    assert "clause" not in result[4]
    assert "title" not in result[4]
    assert "score" not in result[4]

    # Fallback document from header when document_name omitted
    assert result[5]["document"] == "故障排查手册"
    assert result[5]["clause"] == "1.1"
    assert result[5]["title"] == "蓝牙耳机无声"

    assert parse_citations(None) == []
    assert parse_citations("invalid") == []


def test_sanitize_call_summary_whitelist_and_redaction() -> None:
    raw = {
        "tool": "create_refund",
        "kind": "pending",
        "http_status": 202,
        "error_code": "",
        "request_id": None,
        "approval_id": "AP000001",
        "user_token": "eyJhbGciOiJSUzI1NiJ9.secret",
        "inputs": {"user_token": "eyJ-secret"},
        "raw_response": {"customer": "secret"},
    }
    cleaned = sanitize_call_summary(raw)
    assert cleaned == {
        "tool": "create_refund",
        "kind": "pending",
        "http_status": 202,
        "approval_id": "AP000001",
    }
    assert "user_token" not in cleaned
    assert "error_code" not in cleaned
    assert "request_id" not in cleaned

    # Digit string status accepted; bool rejected
    assert sanitize_call_summary({"tool": "get_order", "kind": "denied", "http_status": "403"}) == {
        "tool": "get_order",
        "kind": "denied",
        "http_status": 403,
    }
    assert sanitize_call_summary({"tool": "get_order", "kind": "ok", "http_status": True}) == {
        "tool": "get_order",
        "kind": "ok",
    }

    assert sanitize_call_summary(None) is None
    assert sanitize_call_summary("bad") is None
    assert sanitize_call_summary({}) is None
    assert sanitize_call_summary({"http_status": 200, "extra": "ignored"}) is None


def test_stream_processor_full_happy_path_and_filtering() -> None:
    proc = DifyStreamProcessor()
    secret_jwt = "eyJhbGciOiJSUzI1NiJ9.SUPER_SECRET_PAYLOAD.SIG"

    # Ignored events
    assert (
        proc.process_event(
            {
                "event": "workflow_started",
                "conversation_id": "c-100",
                "message_id": "m-200",
                "data": {"inputs": {"user_token": secret_jwt}},
            }
        )
        == []
    )
    assert proc.process_event({"event": "ping"}) == []
    assert (
        proc.process_event(
            {
                "event": "node_started",
                "data": {"node_id": "start", "inputs": {"user_token": secret_jwt}},
            }
        )
        == []
    )
    # node_finished without call_summary -> dropped
    assert (
        proc.process_event(
            {
                "event": "node_finished",
                "data": {
                    "node_id": "call_get_order",
                    "inputs": {"user_token": secret_jwt},
                    "process_data": {"headers": {"Authorization": f"Bearer {secret_jwt}"}},
                    "outputs": {"body": "raw-order-json", "status_code": 200},
                },
            }
        )
        == []
    )

    # node_finished with call_summary -> trace emitted, extra fields stripped
    trace_events = proc.process_event(
        {
            "event": "node_finished",
            "data": {
                "node_id": "parse_order",
                "inputs": {"user_token": secret_jwt},
                "process_data": {"token": secret_jwt},
                "outputs": {
                    "call_summary": {
                        "tool": "get_order",
                        "kind": "denied",
                        "http_status": 403,
                        "error_code": "order_access_denied",
                        "request_id": "req-403",
                        "user_token": secret_jwt,
                    },
                    "reply": "你没有权限查看该订单。",
                },
            },
        }
    )
    assert trace_events == [
        (
            "trace",
            {
                "tool": "get_order",
                "kind": "denied",
                "http_status": 403,
                "error_code": "order_access_denied",
                "request_id": "req-403",
            },
        )
    ]

    # Empty message delta ignored; non-empty emitted
    assert proc.process_event({"event": "message", "answer": ""}) == []
    assert proc.process_event({"event": "message", "answer": "你没有权限"}) == [
        ("delta", {"text": "你没有权限"})
    ]
    assert proc.process_event({"event": "message", "answer": "查看该订单。"}) == [
        ("delta", {"text": "查看该订单。"})
    ]

    # workflow_finished succeeded -> dropped
    assert proc.process_event({"event": "workflow_finished", "data": {"status": "succeeded"}}) == []

    # message_end -> done
    done_events = proc.process_event(
        {
            "event": "message_end",
            "conversation_id": "c-100",
            "message_id": "m-200",
            "metadata": {
                "retriever_resources": [
                    {
                        "document_name": "售后制度.md",
                        "content": "售后制度 第 7.6 条 数据范围\n只处理本人负责区域的订单。",
                        "score": 0.88,
                    }
                ]
            },
        }
    )
    assert len(done_events) == 1
    ev_name, payload = done_events[0]
    assert ev_name == "done"
    assert payload == {
        "conversation_id": "c-100",
        "message_id": "m-200",
        "answer": "你没有权限查看该订单。",
        "citations": [
            {
                "document": "售后制度",
                "clause": "7.6",
                "title": "数据范围",
                "content": "售后制度 第 7.6 条 数据范围\n只处理本人负责区域的订单。",
                "score": 0.88,
            }
        ],
        "trace": [
            {
                "tool": "get_order",
                "kind": "denied",
                "http_status": 403,
                "error_code": "order_access_denied",
                "request_id": "req-403",
            }
        ],
    }
    assert secret_jwt not in json.dumps(payload, ensure_ascii=False)
    assert proc.done_payload == payload


def test_stream_processor_error_and_workflow_failed() -> None:
    secret_jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.SUPER_SECRET_SIG_123456"

    # Direct error event: captures redacted & truncated error_detail, never exposes to browser
    p1 = DifyStreamProcessor()
    out1 = p1.process_event(
        {
            "event": "error",
            "status": 500,
            "code": "provider_error",
            "message": f"internal stack trace with Bearer {secret_jwt} " + ("x" * 700),
        }
    )
    assert out1 == [("error", DIFY_ERROR_EVENT_PAYLOAD)]
    assert p1.failed is True
    assert p1.error_reason == "dify_error_provider_error"
    assert p1.error_detail is not None
    assert len(p1.error_detail) <= 500
    assert p1.error_detail.endswith("...")
    assert "[REDACTED_JWT]" in p1.error_detail
    assert secret_jwt not in p1.error_detail
    # Subsequent events ignored after error
    assert p1.process_event({"event": "message", "answer": "ignored"}) == []
    assert p1.process_event({"event": "node_finished", "data": {"outputs": {}}}) == []
    assert p1.process_event({"event": "message_end"}) == []
    assert p1.process_event({"event": "error", "status": 500, "message": "second"}) == []
    assert p1.error_reason == "dify_error_provider_error"

    # workflow_finished failed: captures redacted error_detail
    p2 = DifyStreamProcessor()
    out2 = p2.process_event(
        {
            "event": "workflow_finished",
            "data": {"status": "failed", "error": f"timeout calling llm token={secret_jwt}"},
        }
    )
    assert out2 == [("error", DIFY_ERROR_EVENT_PAYLOAD)]
    assert p2.failed is True
    assert p2.error_reason == "workflow_failed"
    assert p2.error_detail == "timeout calling llm token=[REDACTED_JWT]"
    assert p2.process_event({"event": "workflow_finished", "data": {"status": "failed"}}) == []

    # Error without message followed by workflow_finished with error populates error_detail once
    p2b = DifyStreamProcessor()
    assert p2b.process_event({"event": "error", "status": 500}) == [
        ("error", DIFY_ERROR_EVENT_PAYLOAD)
    ]
    assert p2b.error_detail is None
    assert (
        p2b.process_event(
            {"event": "workflow_finished", "data": {"status": "failed", "error": "node boom"}}
        )
        == []
    )
    assert p2b.error_detail == "node boom"

    # Error with numeric status only / without code or status
    p3 = DifyStreamProcessor()
    assert p3.process_event({"event": "error", "status": 504}) == [
        ("error", DIFY_ERROR_EVENT_PAYLOAD)
    ]
    assert p3.error_reason == "dify_error_504"

    p4 = DifyStreamProcessor()
    assert p4.process_event({"event": "error"}) == [("error", DIFY_ERROR_EVENT_PAYLOAD)]
    assert p4.error_reason == "dify_stream_error"

    # Malformed non-dict or non-str event safely ignored
    p5 = DifyStreamProcessor()
    assert p5.process_event({"event": 123}) == []
    assert p5.process_event({"event": "node_finished", "data": "bad"}) == []
    assert p5.process_event({"event": "node_finished", "data": {"outputs": "bad"}}) == []


def test_format_sse() -> None:
    frame = format_sse("delta", {"text": "你好"})
    assert frame == 'event: delta\ndata: {"text": "你好"}\n\n'
