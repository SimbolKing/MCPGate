from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.clock import get_clock
from app.config import Settings
from app.main import create_app
from tests.conftest import StaticKeyResolver, take_db_snapshot


def _assert_no_null_values(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert v is not None, f"字段 {k} 不应输出 null"
            _assert_no_null_values(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_null_values(item)


def test_get_order_detail_complete_and_phone_masked(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    resp = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert resp.status_code == 200
    body = resp.json()
    _assert_no_null_values(body)

    assert body["order_id"] == "A1023"
    assert body["region"] == "east"
    assert body["status"] == "delivered"
    assert body["created_at"].endswith("+08:00")
    assert body["paid_at"].endswith("+08:00")
    assert body["shipped_at"].endswith("+08:00")
    assert body["delivered_at"] == "2026-11-02T14:30:00+08:00"
    assert body["days_since_delivery"] == 18
    assert body["total_amount"] == 299.0
    assert body["refunded_amount"] == 0.0
    assert body["refundable_amount"] == 299.0
    assert body["refund_status"] == "none"

    assert body["customer"] == {
        "customer_id": "C0001",
        "name": "张敏",
        "phone_masked": "138****5678",
        "level": "regular",
    }
    assert "phone" not in body["customer"]
    assert "scenario" not in body

    assert body["items"] == [
        {
            "sku": "EL-1001",
            "product_name": "蓝牙耳机",
            "category": "electronics",
            "quantity": 1,
            "unit_price": 299.0,
        }
    ]
    assert len(body["after_sale_tickets"]) == 1
    ticket = body["after_sale_tickets"][0]
    assert ticket["ticket_id"] == "T5001"
    assert ticket["type"] == "return_refund"
    assert ticket["reason_category"] == "quality_issue"
    assert ticket["description"] == "右耳没有声音"
    assert ticket["requested_amount"] == 299.0
    assert ticket["status"] == "open"
    assert "resolved_at" not in ticket
    assert body["refunds"] == []


def test_get_order_omits_null_fields_and_computes_refundable(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 未支付订单省略 paid_at / shipped_at / delivered_at / days_since_delivery
    resp_unpaid = client.get("/api/v1/orders/A1029", headers=auth_headers("wang"))
    assert resp_unpaid.status_code == 200
    unpaid = resp_unpaid.json()
    _assert_no_null_values(unpaid)
    for absent in ("paid_at", "shipped_at", "delivered_at", "days_since_delivery"):
        assert absent not in unpaid

    # 部分已退 A1031：total 458 - refunded 158 = refundable 300
    resp_partial = client.get("/api/v1/orders/A1031", headers=auth_headers("wang"))
    partial = resp_partial.json()
    assert partial["total_amount"] == 458.0
    assert partial["refunded_amount"] == 158.0
    assert partial["refundable_amount"] == 300.0
    assert partial["refund_status"] == "partial"
    assert len(partial["items"]) == 2
    assert len(partial["refunds"]) == 1
    assert partial["refunds"][0]["refund_id"] == "R000101"

    # 处理中退款 A1035：processing 699 占满可退额度
    resp_proc = client.get("/api/v1/orders/A1035", headers=auth_headers("wang"))
    proc = resp_proc.json()
    assert proc["refunded_amount"] == 0.0
    assert proc["refundable_amount"] == 0.0
    assert proc["refund_status"] == "none"

    # 已全额退款 A1034 + 已解决工单含 resolved_at
    resp_full = client.get("/api/v1/orders/A1034", headers=auth_headers("wang"))
    full = resp_full.json()
    assert full["refund_status"] == "full"
    assert full["refundable_amount"] == 0.0
    assert full["after_sale_tickets"][0]["resolved_at"].endswith("+08:00")


def test_days_since_delivery_natural_day_boundary(
    app: FastAPI,
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # A1023 签收于 2026-11-02T14:30:00+08:00
    # 签收当天 23:59:59+08:00（UTC 15:59:59）→ 0 天
    same_day = datetime(2026, 11, 2, 15, 59, 59, tzinfo=UTC)
    app.dependency_overrides[get_clock] = lambda: lambda: same_day
    resp0 = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert resp0.json()["days_since_delivery"] == 0

    # 次日 00:00:01+08:00（2026-11-02T16:00:01Z）→ 1 天
    next_day = datetime(2026, 11, 2, 16, 0, 1, tzinfo=UTC)
    app.dependency_overrides[get_clock] = lambda: lambda: next_day
    resp1 = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert resp1.json()["days_since_delivery"] == 1


def test_get_order_not_found_without_format_validation(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    for missing_id in ("X9999", "not-an-order-format"):
        resp = client.get(f"/api/v1/orders/{missing_id}", headers=auth_headers("sun"))
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "order_not_found"
        assert body["request_id"] == resp.headers["X-Request-Id"]


def test_list_orders_filters_sorting_and_pagination(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 按下单时间倒序
    resp_all = client.get("/api/v1/orders?page=1&page_size=50", headers=auth_headers("wang"))
    assert resp_all.status_code == 200
    body_all = resp_all.json()
    _assert_no_null_values(body_all)
    created_times = [item["created_at"] for item in body_all["items"]]
    assert created_times == sorted(created_times, reverse=True)

    # 按状态筛选
    resp_cancelled = client.get("/api/v1/orders?status=cancelled", headers=auth_headers("sun"))
    body_cancelled = resp_cancelled.json()
    assert body_cancelled["total"] == 2
    assert {o["order_id"] for o in body_cancelled["items"]} == {"A1030", "C3056"}
    for item in body_cancelled["items"]:
        assert "delivered_at" not in item
        assert "days_since_delivery" not in item

    # 按 has_open_ticket 筛选
    resp_open = client.get("/api/v1/orders?has_open_ticket=true", headers=auth_headers("wang"))
    open_body = resp_open.json()
    assert open_body["total"] == 10
    assert all(o["has_open_ticket"] is True for o in open_body["items"])

    resp_no_open = client.get("/api/v1/orders?has_open_ticket=false", headers=auth_headers("wang"))
    no_open_body = resp_no_open.json()
    assert no_open_body["total"] == 4
    assert all(o["has_open_ticket"] is False for o in no_open_body["items"])

    # 分页
    p1 = client.get("/api/v1/orders?page=1&page_size=5", headers=auth_headers("wang")).json()
    p2 = client.get("/api/v1/orders?page=2&page_size=5", headers=auth_headers("wang")).json()
    assert p1["total"] == 14 and len(p1["items"]) == 5 and p1["page"] == 1 and p1["page_size"] == 5
    assert p2["total"] == 14 and len(p2["items"]) == 5 and p2["page"] == 2
    assert {i["order_id"] for i in p1["items"]}.isdisjoint({i["order_id"] for i in p2["items"]})


@pytest.mark.parametrize(
    "query", ["page=0", "page=-1", "page_size=0", "page_size=51", "status=bad"]
)
def test_list_orders_invalid_query_returns_422(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    query: str,
) -> None:
    resp = client.get(f"/api/v1/orders?{query}", headers=auth_headers("wang"))
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["details"], list) and len(body["details"]) >= 1
    assert "field" in body["details"][0] and "message" in body["details"][0]


def test_request_id_propagation_and_healthz(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    resp_health = client.get("/healthz")
    assert resp_health.status_code == 200
    assert resp_health.json() == {"status": "ok"}
    assert resp_health.headers["X-Request-Id"]

    custom_rid = "req-trace-12345"
    resp = client.get(
        "/api/v1/orders/A1023",
        headers={**auth_headers("wang"), "X-Request-Id": custom_rid},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == custom_rid


def test_get_order_endpoints_have_no_side_effects(
    app: FastAPI,
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    before = take_db_snapshot(app)
    assert client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).status_code == 200
    assert client.get("/api/v1/orders", headers=auth_headers("sun")).status_code == 200
    assert client.get("/api/v1/orders/X9999", headers=auth_headers("sun")).status_code == 404
    after = take_db_snapshot(app)
    assert after == before


def test_unexpected_exception_returns_g8_500_and_logs_error_code(
    settings: Settings,
    key_resolver: StaticKeyResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    err_app = create_app(
        settings=settings,
        init_database=False,
        signing_key_resolver=key_resolver,
    )

    @err_app.get("/_boom", include_in_schema=False)
    def boom() -> None:
        raise RuntimeError("sensitive internal detail")

    mock_api_logger = logging.getLogger("mock_api")
    orig_propagate = mock_api_logger.propagate
    mock_api_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="mock_api"):
            with TestClient(err_app, raise_server_exceptions=False) as tc:
                resp = tc.get("/_boom", headers={"X-Request-Id": "rid-500"})
        assert resp.status_code == 500
        assert resp.headers["X-Request-Id"] == "rid-500"
        body = resp.json()
        assert body["code"] == "internal_error"
        assert body["request_id"] == "rid-500"
        assert "sensitive" not in body["message"]

        http_logs = [
            getattr(r, "fields", {})
            for r in caplog.records
            if getattr(r, "fields", {}).get("event") == "http_request"
        ]
        assert len(http_logs) == 1
        assert http_logs[0]["status"] == 500
        assert http_logs[0]["error_code"] == "internal_error"
        assert http_logs[0]["request_id"] == "rid-500"
    finally:
        mock_api_logger.propagate = orig_propagate


def test_framework_http_exceptions_return_g8_format(
    settings: Settings,
    key_resolver: StaticKeyResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_app = create_app(
        settings=settings,
        init_database=True,
        signing_key_resolver=key_resolver,
    )

    @test_app.get("/_teapot", include_in_schema=False)
    def teapot() -> None:
        raise StarletteHTTPException(status_code=418, detail="I'm a teapot")

    mock_api_logger = logging.getLogger("mock_api")
    orig_propagate = mock_api_logger.propagate
    mock_api_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="mock_api"):
            with TestClient(test_app) as tc:
                # 1. 未知路径 → 404 not_found
                resp_404 = tc.get("/api/v1/unknown-path", headers={"X-Request-Id": "rid-404"})
                assert resp_404.status_code == 404
                assert resp_404.headers["X-Request-Id"] == "rid-404"
                body_404 = resp_404.json()
                assert set(body_404.keys()) == {"code", "message", "request_id"}
                assert body_404["code"] == "not_found"
                assert body_404["request_id"] == "rid-404"

                # 2. 不支持的方法 → 405 method_not_allowed 且保留 Allow 响应头
                resp_405 = tc.delete("/api/v1/refunds/R000101", headers={"X-Request-Id": "rid-405"})
                assert resp_405.status_code == 405
                assert resp_405.headers["X-Request-Id"] == "rid-405"
                assert "GET" in resp_405.headers.get("Allow", "")
                body_405 = resp_405.json()
                assert set(body_405.keys()) == {"code", "message", "request_id"}
                assert body_405["code"] == "method_not_allowed"
                assert body_405["request_id"] == "rid-405"

                # 3. 其余 Starlette HTTPException → 通用 http_<status> 与中文 message
                resp_418 = tc.get("/_teapot", headers={"X-Request-Id": "rid-418"})
                assert resp_418.status_code == 418
                assert resp_418.headers["X-Request-Id"] == "rid-418"
                body_418 = resp_418.json()
                assert set(body_418.keys()) == {"code", "message", "request_id"}
                assert body_418["code"] == "http_418"
                assert body_418["request_id"] == "rid-418"

        http_logs = {
            getattr(r, "fields", {}).get("request_id"): getattr(r, "fields", {})
            for r in caplog.records
            if getattr(r, "fields", {}).get("event") == "http_request"
        }
        assert http_logs["rid-404"]["error_code"] == "not_found"
        assert http_logs["rid-405"]["error_code"] == "method_not_allowed"
        assert http_logs["rid-418"]["error_code"] == "http_418"
    finally:
        mock_api_logger.propagate = orig_propagate


def test_list_orders_huge_page_returns_empty_without_overflow(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    huge_page = 10**25
    resp = client.get(f"/api/v1/orders?page={huge_page}&page_size=20", headers=auth_headers("wang"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 14
    assert body["page"] == huge_page
    assert body["page_size"] == 20
