from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import repository
from app.auth import Principal
from app.errors import AccessDenied
from app.models import Refund
from tests.conftest import FIXED_NOW

# 6 账号 × 3 区域代表订单的访问矩阵：(account, order_id, expected_get_status)
ORDER_ACCESS_MATRIX = [
    ("wang", "A1023", 200),
    ("wang", "B2087", 403),
    ("wang", "C3051", 403),
    ("zhao", "A1023", 403),
    ("zhao", "B2087", 200),
    ("zhao", "C3051", 403),
    ("chen", "A1023", 403),
    ("chen", "B2087", 403),
    ("chen", "C3051", 200),
    ("li", "A1023", 200),
    ("li", "B2087", 403),
    ("li", "C3051", 403),
    ("zhou", "A1023", 403),
    ("zhou", "B2087", 200),
    ("zhou", "C3051", 200),
    ("sun", "A1023", 200),
    ("sun", "B2087", 200),
    ("sun", "C3051", 200),
]

# 6 账号 × 既有退款单（R000101 华东、R000104 华南）的访问矩阵
REFUND_ACCESS_MATRIX = [
    ("wang", "R000101", 200),
    ("wang", "R000104", 403),
    ("zhao", "R000101", 403),
    ("zhao", "R000104", 403),
    ("chen", "R000101", 403),
    ("chen", "R000104", 200),
    ("li", "R000101", 200),
    ("li", "R000104", 403),
    ("zhou", "R000101", 403),
    ("zhou", "R000104", 200),
    ("sun", "R000101", 200),
    ("sun", "R000104", 200),
]

# 6 账号的 list_orders 与 list_refunds 期望
LIST_MATRIX = [
    ("wang", {"east"}, 14, 403, None),
    ("zhao", {"north"}, 8, 403, None),
    ("chen", {"south"}, 6, 403, None),
    ("li", {"east"}, 14, 200, 3),
    ("zhou", {"north", "south"}, 14, 200, 1),
    ("sun", {"east", "north", "south"}, 28, 200, 4),
]


@pytest.mark.parametrize(("account", "order_id", "expected_status"), ORDER_ACCESS_MATRIX)
def test_get_order_permission_matrix(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    account: str,
    order_id: str,
    expected_status: int,
) -> None:
    resp = client.get(f"/api/v1/orders/{order_id}", headers=auth_headers(account))
    assert resp.status_code == expected_status
    if expected_status == 403:
        body = resp.json()
        assert body["code"] == "order_access_denied"
        assert set(body.keys()) == {"code", "message", "request_id"}
        # 403 不泄露目标订单所属区域及任何业务字段
        for forbidden in (
            "east",
            "north",
            "south",
            "华东",
            "华北",
            "华南",
            "平板",
            "蓝牙",
            "连衣裙",
        ):
            assert forbidden not in body["message"]


@pytest.mark.parametrize(("account", "order_id", "expected_get_status"), ORDER_ACCESS_MATRIX)
def test_create_refund_permission_matrix(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    account: str,
    order_id: str,
    expected_get_status: int,
) -> None:
    # 写接口先做行级权限判定（R4）
    resp = client.post(
        "/api/v1/refunds",
        headers=auth_headers(account),
        json={
            "order_id": order_id,
            "amount": 1.0,
            "reason_category": "other",
            "reason": "权限矩阵验证",
        },
    )
    if expected_get_status == 403:
        assert resp.status_code == 403
        assert resp.json()["code"] == "order_access_denied"
    else:
        assert resp.status_code == 201


@pytest.mark.parametrize(("account", "refund_id", "expected_status"), REFUND_ACCESS_MATRIX)
def test_get_refund_permission_matrix(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    account: str,
    refund_id: str,
    expected_status: int,
) -> None:
    resp = client.get(f"/api/v1/refunds/{refund_id}", headers=auth_headers(account))
    assert resp.status_code == expected_status
    if expected_status == 403:
        body = resp.json()
        assert body["code"] == "refund_access_denied"
        assert set(body.keys()) == {"code", "message", "request_id"}
        for forbidden in ("east", "north", "south", "华东", "华北", "华南", "A1031", "C3055"):
            assert forbidden not in body["message"]


@pytest.mark.parametrize(
    (
        "account",
        "allowed_regions",
        "expected_order_total",
        "refund_list_status",
        "expected_refunds",
    ),
    LIST_MATRIX,
)
def test_list_orders_and_refunds_matrix(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    account: str,
    allowed_regions: set[str],
    expected_order_total: int,
    refund_list_status: int,
    expected_refunds: int | None,
) -> None:
    resp_orders = client.get("/api/v1/orders?page=1&page_size=50", headers=auth_headers(account))
    assert resp_orders.status_code == 200
    body_orders = resp_orders.json()
    assert body_orders["total"] == expected_order_total
    assert len(body_orders["items"]) == expected_order_total
    assert all(item["region"] in allowed_regions for item in body_orders["items"])

    resp_refunds = client.get("/api/v1/refunds?page=1&page_size=50", headers=auth_headers(account))
    assert resp_refunds.status_code == refund_list_status
    if refund_list_status == 403:
        assert resp_refunds.json()["code"] == "insufficient_role"
    else:
        body_refunds = resp_refunds.json()
        assert body_refunds["total"] == expected_refunds
        assert len(body_refunds["items"]) == expected_refunds
        assert all(item["region"] in allowed_regions for item in body_refunds["items"])


def test_cross_region_customer_c0003_sliced_by_caller(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    wang_orders = client.get(
        "/api/v1/orders?customer_id=C0003", headers=auth_headers("wang")
    ).json()
    assert wang_orders["total"] == 3
    assert {o["order_id"] for o in wang_orders["items"]} == {"A1026", "A1030", "A1035"}

    zhao_orders = client.get(
        "/api/v1/orders?customer_id=C0003", headers=auth_headers("zhao")
    ).json()
    assert zhao_orders["total"] == 1
    assert {o["order_id"] for o in zhao_orders["items"]} == {"B2089"}

    chen_orders = client.get(
        "/api/v1/orders?customer_id=C0003", headers=auth_headers("chen")
    ).json()
    assert chen_orders["total"] == 0
    assert chen_orders["items"] == []

    sun_orders = client.get("/api/v1/orders?customer_id=C0003", headers=auth_headers("sun")).json()
    assert sun_orders["total"] == 4
    assert {o["order_id"] for o in sun_orders["items"]} == {"A1026", "A1030", "A1035", "B2089"}


def test_all_public_repository_functions_require_principal() -> None:
    public_funcs = [
        (name, fn)
        for name, fn in inspect.getmembers(repository, inspect.isfunction)
        if not name.startswith("_") and fn.__module__ == repository.__name__
    ]
    assert public_funcs, "repository 模块应存在公开数据访问函数"
    for name, fn in public_funcs:
        sig = inspect.signature(fn)
        assert "principal" in sig.parameters, f"repository.{name} 缺少 principal 参数"
        param = sig.parameters["principal"]
        assert param.default is inspect.Parameter.empty, (
            f"repository.{name} 的 principal 必须为必填参数"
        )


def test_repository_add_refund_rejects_foreign_region(client: TestClient, app: FastAPI) -> None:
    _ = client
    east_principal = Principal(
        sub="u1001", username="wang", role="agent", regions=frozenset({"east"})
    )
    foreign_refund = Refund(
        refund_id="R999999",
        order_id="B2087",
        region="north",
        ticket_id=None,
        amount_cents=100,
        reason_category="other",
        reason="越权写入防线测试",
        status="succeeded",
        idempotency_key=None,
        created_by="u1001",
        created_by_role="agent",
        created_at=FIXED_NOW,
        completed_at=FIXED_NOW,
    )
    with app.state.session_factory() as session, pytest.raises(AccessDenied) as exc_info:
        repository.add_refund(session, east_principal, foreign_refund)
    assert exc_info.value.code == "order_access_denied"
