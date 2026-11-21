from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from tests.conftest import take_db_snapshot


def test_create_refund_success_partial_then_full_and_resolves_ticket(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. 重置后新建首张退款单必为 R000105（部分退款，暂不关联工单）
    resp1 = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1023",
            "amount": 100.5,
            "reason_category": "quality_issue",
            "reason": "右耳无声，先退部分金额",
        },
    )
    assert resp1.status_code == 201
    assert resp1.headers["Location"] == "/api/v1/refunds/R000105"
    body1 = resp1.json()
    assert body1["refund_id"] == "R000105"
    assert body1["order_id"] == "A1023"
    assert body1["region"] == "east"
    assert "ticket_id" not in body1
    assert body1["amount"] == 100.5
    assert body1["status"] == "succeeded"
    assert body1["created_by"] == "u1001"
    assert body1["created_by_role"] == "agent"
    assert body1["created_at"].endswith("+08:00")
    assert body1["completed_at"].endswith("+08:00")

    order_mid = client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).json()
    assert order_mid["refunded_amount"] == 100.5
    assert order_mid["refundable_amount"] == 198.5
    assert order_mid["refund_status"] == "partial"
    assert order_mid["after_sale_tickets"][0]["status"] == "open"

    # 2. 退完剩余余额并关联工单 T5001，工单置为 resolved
    resp2 = client.post(
        "/api/v1/refunds",
        headers=auth_headers("li"),
        json={
            "order_id": "A1023",
            "amount": 198.5,
            "reason_category": "quality_issue",
            "reason": "质量问题补齐剩余全额退款",
            "ticket_id": "T5001",
        },
    )
    assert resp2.status_code == 201
    assert resp2.headers["Location"] == "/api/v1/refunds/R000106"
    body2 = resp2.json()
    assert body2["refund_id"] == "R000106"
    assert body2["ticket_id"] == "T5001"
    assert body2["created_by"] == "u2001"
    assert body2["created_by_role"] == "supervisor"

    order_after = client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).json()
    assert order_after["refunded_amount"] == 299.0
    assert order_after["refundable_amount"] == 0.0
    assert order_after["refund_status"] == "full"
    assert order_after["after_sale_tickets"][0]["status"] == "resolved"
    assert order_after["after_sale_tickets"][0]["resolved_at"].endswith("+08:00")
    assert len(order_after["refunds"]) == 2


def test_create_refund_order_not_found(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    resp = client.post(
        "/api/v1/refunds",
        headers=auth_headers("sun"),
        json={
            "order_id": "X9999",
            "amount": 10.0,
            "reason_category": "other",
            "reason": "不存在的订单",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "order_not_found"


def test_create_refund_five_409_conflicts(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1) order_not_refundable: pending_payment & cancelled
    for oid in ("A1029", "A1030"):
        resp = client.post(
            "/api/v1/refunds",
            headers=auth_headers("wang"),
            json={
                "order_id": oid,
                "amount": 10.0,
                "reason_category": "other",
                "reason": "状态不可退",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "order_not_refundable"

    # 2) refund_in_progress: A1035 已有 processing 退款单 R000103
    resp_proc = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1035",
            "amount": 10.0,
            "reason_category": "quality_issue",
            "reason": "处理中冲突",
        },
    )
    assert resp_proc.status_code == 409
    assert resp_proc.json()["code"] == "refund_in_progress"

    # 3) refund_amount_exceeded: A1031 可退 300.00，申请 300.01；A1034 已全额退款
    resp_exceed = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1031",
            "amount": 300.01,
            "reason_category": "no_reason",
            "reason": "超额退款",
        },
    )
    assert resp_exceed.status_code == 409
    assert resp_exceed.json()["code"] == "refund_amount_exceeded"
    assert "300.00" in resp_exceed.json()["message"]

    resp_full = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1034",
            "amount": 1.0,
            "reason_category": "quality_issue",
            "reason": "已全额退款再退",
        },
    )
    assert resp_full.status_code == 409
    assert resp_full.json()["code"] == "refund_amount_exceeded"
    assert "0.00" in resp_full.json()["message"]

    # 4) ticket_not_applicable: 不存在 / 属于其他订单 / 非 open 状态（B2093 T5017 rejected）
    for account, oid, tid in [
        ("wang", "A1023", "T9999"),
        ("wang", "A1023", "T5002"),
        ("zhao", "B2093", "T5017"),
    ]:
        resp_ticket = client.post(
            "/api/v1/refunds",
            headers=auth_headers(account),
            json={
                "order_id": oid,
                "amount": 10.0,
                "reason_category": "other",
                "reason": "工单不可用",
                "ticket_id": tid,
            },
        )
        assert resp_ticket.status_code == 409
        assert resp_ticket.json()["code"] == "ticket_not_applicable"

    # 5) idempotency_conflict: 相同幂等键但金额或订单号不同
    first = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1024",
            "amount": 100.0,
            "reason_category": "no_reason",
            "reason": "首次幂等提交",
            "idempotency_key": "idem_conflict_001",
        },
    )
    assert first.status_code == 201

    diff_amount = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1024",
            "amount": 200.0,
            "reason_category": "no_reason",
            "reason": "金额不同",
            "idempotency_key": "idem_conflict_001",
        },
    )
    assert diff_amount.status_code == 409
    assert diff_amount.json()["code"] == "idempotency_conflict"

    diff_order = client.post(
        "/api/v1/refunds",
        headers=auth_headers("wang"),
        json={
            "order_id": "A1023",
            "amount": 100.0,
            "reason_category": "no_reason",
            "reason": "订单不同",
            "idempotency_key": "idem_conflict_001",
        },
    )
    assert diff_order.status_code == 409
    assert diff_order.json()["code"] == "idempotency_conflict"


def test_idempotency_replay_returns_same_refund_even_after_full_refund(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    payload = {
        "order_id": "A1023",
        "amount": 299.0,
        "reason_category": "quality_issue",
        "reason": "质量问题全额退款",
        "ticket_id": "T5001",
        "idempotency_key": "idem_replay_a1023",
    }
    r1 = client.post("/api/v1/refunds", headers=auth_headers("wang"), json=payload)
    r2 = client.post("/api/v1/refunds", headers=auth_headers("wang"), json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json() == r2.json()
    assert r1.headers["Location"] == r2.headers["Location"] == "/api/v1/refunds/R000105"

    order = client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).json()
    assert order["refunded_amount"] == 299.0
    assert len(order["refunds"]) == 1


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"order_id": "A1023", "amount": 0, "reason_category": "other", "reason": "零金额"},
        {"order_id": "A1023", "amount": -10, "reason_category": "other", "reason": "负金额"},
        {"order_id": "A1023", "amount": 10.123, "reason_category": "other", "reason": "三位小数"},
        {"order_id": "A1023", "amount": 0.001, "reason_category": "other", "reason": "超精度小额"},
        {"order_id": "A1023", "amount": True, "reason_category": "other", "reason": "布尔金额"},
        {"order_id": "A1023", "amount": 10.0, "reason_category": "other", "reason": "   "},
        {"order_id": "A1023", "amount": 10.0, "reason_category": "bad_cat", "reason": "非法类别"},
        {
            "order_id": "A1023",
            "amount": 10.0,
            "reason_category": "other",
            "reason": "幂等键过短",
            "idempotency_key": "short",
        },
        {
            "order_id": "A1023",
            "amount": 10.0,
            "reason_category": "other",
            "reason": "幂等键含非法字符",
            "idempotency_key": "bad key!@#",
        },
    ],
)
def test_create_refund_validation_errors_return_422(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    bad_payload: dict[str, Any],
) -> None:
    resp = client.post("/api/v1/refunds", headers=auth_headers("wang"), json=bad_payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["details"], list) and len(body["details"]) >= 1


@pytest.mark.parametrize("raw_amount", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_create_refund_non_finite_amount_returns_422(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    raw_amount: str,
) -> None:
    raw_body = (
        f'{{"order_id": "A1023", "amount": {raw_amount}, '
        '"reason_category": "quality_issue", "reason": "非有限数值回归测试"}'
    ).encode()
    resp = client.post(
        "/api/v1/refunds",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
        content=raw_body,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["details"], list) and len(body["details"]) >= 1


@pytest.mark.parametrize(
    "raw_body",
    [
        ('{"x":' + "[" * 1200 + "1" + "]" * 1200 + "}").encode(),
        (
            '{"order_id":'
            + "[" * 1200
            + '"A1023"'
            + "]" * 1200
            + ', "amount": 10.0, "reason_category": "other", "reason": "深嵌套"}'
        ).encode(),
        ('{"x":' + "[" * 5000 + "1" + "]" * 5000 + "}").encode(),
        (b'{"order_id": "A1023", "amount": 10.0, "reason_category": "other", "reason": "\\ud800"}'),
    ],
)
def test_create_refund_deep_nesting_and_lone_surrogate_return_g8_422(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    raw_body: bytes,
) -> None:
    resp = client.post(
        "/api/v1/refunds",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
        content=raw_body,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["message"]
    assert body["request_id"] == resp.headers["X-Request-Id"]
    assert isinstance(body["details"], list) and len(body["details"]) >= 1


def test_create_refund_extreme_deep_nesting_and_body_parse_400_mapping(
    app: FastAPI,
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. 12000-layer nested JSON returns 4xx G8 (422 validation_error when FastAPI/Pydantic rejects)
    extreme_nested = ('{"x":' + "[" * 12000 + "1" + "]" * 12000 + "}").encode()
    resp = client.post(
        "/api/v1/refunds",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
        content=extreme_nested,
    )
    assert 400 <= resp.status_code < 500
    body = resp.json()
    assert body["code"]
    assert body["message"]
    assert body["request_id"] == resp.headers["X-Request-Id"]
    assert resp.status_code == 422
    assert body["code"] == "validation_error"
    assert body["message"] == "请求参数不合法，请检查输入字段。"
    assert isinstance(body["details"], list) and len(body["details"]) >= 1

    # 2. FastAPI body-parse 400 maps to 422 validation_error; ordinary 400 stays 400
    @app.get("/_test_body_parse_400")
    def _body_parse_400() -> None:
        raise StarletteHTTPException(status_code=400, detail="There was an error parsing the body")

    @app.get("/_test_plain_400")
    def _plain_400() -> None:
        raise StarletteHTTPException(status_code=400, detail="custom bad request")

    r_parse_400 = client.get("/_test_body_parse_400")
    assert r_parse_400.status_code == 422
    b_parse = r_parse_400.json()
    assert b_parse["code"] == "validation_error"
    assert b_parse["message"] == "请求参数不合法，请检查输入字段。"
    assert b_parse["request_id"] == r_parse_400.headers["X-Request-Id"]
    assert isinstance(b_parse["details"], list) and len(b_parse["details"]) >= 1

    r_plain_400 = client.get("/_test_plain_400")
    assert r_plain_400.status_code == 400
    b_plain = r_plain_400.json()
    assert b_plain["code"] == "http_400"
    assert b_plain["message"] == "请求处理失败。"
    assert b_plain["request_id"] == r_plain_400.headers["X-Request-Id"]
    assert "custom bad request" not in r_plain_400.text


def test_concurrent_refunds_never_over_refund(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    headers = auth_headers("wang")

    def _submit(idx: int) -> int:
        resp = client.post(
            "/api/v1/refunds",
            headers=headers,
            json={
                "order_id": "A1023",
                "amount": 200.0,
                "reason_category": "quality_issue",
                "reason": f"并发请求 {idx}",
            },
        )
        return resp.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(_submit, range(6)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 5

    order = client.get("/api/v1/orders/A1023", headers=headers).json()
    assert order["refunded_amount"] == 200.0
    assert order["refundable_amount"] == 99.0
    assert len(order["refunds"]) == 1


def test_get_refund_and_list_refunds_filters_and_role_restriction(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # get_refund 200（R000103 为 processing，省略 completed_at）
    resp_proc = client.get("/api/v1/refunds/R000103", headers=auth_headers("wang"))
    assert resp_proc.status_code == 200
    proc_body = resp_proc.json()
    assert proc_body["refund_id"] == "R000103"
    assert proc_body["status"] == "processing"
    assert proc_body["ticket_id"] == "T5010"
    assert "completed_at" not in proc_body

    # get_refund 404
    resp_404 = client.get("/api/v1/refunds/R999999", headers=auth_headers("sun"))
    assert resp_404.status_code == 404
    assert resp_404.json()["code"] == "refund_not_found"

    # list_refunds 客服被拒
    resp_agent = client.get("/api/v1/refunds", headers=auth_headers("wang"))
    assert resp_agent.status_code == 403
    assert resp_agent.json()["code"] == "insufficient_role"

    # list_refunds 主管按 order_id 与 status 筛选
    resp_by_order = client.get("/api/v1/refunds?order_id=A1031", headers=auth_headers("li"))
    assert resp_by_order.status_code == 200
    assert resp_by_order.json()["total"] == 1
    assert resp_by_order.json()["items"][0]["refund_id"] == "R000101"

    resp_by_status = client.get("/api/v1/refunds?status=processing", headers=auth_headers("sun"))
    assert resp_by_status.status_code == 200
    assert resp_by_status.json()["total"] == 1
    assert resp_by_status.json()["items"][0]["refund_id"] == "R000103"

    # order_id 指向不可见或不存在订单返回空列表，不报错（R2）
    resp_hidden = client.get("/api/v1/refunds?order_id=C3055", headers=auth_headers("li"))
    assert resp_hidden.status_code == 200
    assert resp_hidden.json()["total"] == 0
    assert resp_hidden.json()["items"] == []

    # 倒序排列与分页
    resp_all = client.get("/api/v1/refunds?page=1&page_size=2", headers=auth_headers("sun"))
    body_all = resp_all.json()
    assert body_all["total"] == 4
    assert len(body_all["items"]) == 2
    created_times = [item["created_at"] for item in body_all["items"]]
    assert created_times == sorted(created_times, reverse=True)

    # 超大 page 不触发 SQLite OverflowError，直接返回空列表
    huge_page = 10**25
    resp_huge = client.get(
        f"/api/v1/refunds?page={huge_page}&page_size=20", headers=auth_headers("sun")
    )
    assert resp_huge.status_code == 200
    huge_body = resp_huge.json()
    assert huge_body["items"] == []
    assert huge_body["total"] == 4
    assert huge_body["page"] == huge_page


def test_refund_read_endpoints_have_no_side_effects(
    app: FastAPI,
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    before = take_db_snapshot(app)
    assert client.get("/api/v1/refunds/R000101", headers=auth_headers("wang")).status_code == 200
    assert client.get("/api/v1/refunds", headers=auth_headers("sun")).status_code == 200
    assert client.get("/api/v1/refunds/R999999", headers=auth_headers("sun")).status_code == 404
    after = take_db_snapshot(app)
    assert after == before
