from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

from tests.conftest import FakeUpstream, MutableClock


def _submit(
    client: TestClient,
    headers: dict[str, str],
    order_id: str,
    amount: float = 50.0,
) -> dict[str, object]:
    r = client.post(
        "/api/v1/refunds",
        json={
            "order_id": order_id,
            "amount": amount,
            "reason_category": "quality_issue",
            "reason": "质量问题申请退款",
        },
        headers=headers,
    )
    assert r.status_code == 202
    return r.json()


def test_agent_get_and_list_approvals_visibility_and_filtering(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap_east = _submit(client, auth_headers("wang"), "A1023", amount=100)
    ap_north = _submit(client, auth_headers("zhao"), "B2087", amount=200)

    # Same region visible (both applicant and supervisor in east)
    r_wang = client.get(
        f"/guard/v1/approvals/{ap_east['approval_id']}",
        headers=auth_headers("wang"),
    )
    assert r_wang.status_code == 200
    assert r_wang.json()["approval_id"] == ap_east["approval_id"]
    # Null fields omitted (G6)
    for opt_field in (
        "ticket_id",
        "decided_by_sub",
        "decided_by_name",
        "decided_at",
        "decision_comment",
        "refund_id",
        "executed_at",
        "failure_code",
        "failure_message",
    ):
        assert opt_field not in r_wang.json()

    # Cross-region -> 403 approval_access_denied (message must NOT reveal region name)
    r_cross = client.get(
        f"/guard/v1/approvals/{ap_east['approval_id']}",
        headers=auth_headers("zhao"),
    )
    assert r_cross.status_code == 403
    assert r_cross.json()["code"] == "approval_access_denied"
    for region_word in ("east", "north", "south", "华东", "华北", "华南"):
        assert region_word not in r_cross.json()["message"].lower()

    # Nonexistent -> 404 approval_not_found
    r_404 = client.get("/guard/v1/approvals/AP999999", headers=auth_headers("wang"))
    assert r_404.status_code == 404
    assert r_404.json()["code"] == "approval_not_found"

    # list_approvals filters strictly by principal.regions
    wang_list = client.get("/guard/v1/approvals", headers=auth_headers("wang")).json()
    assert wang_list["total"] == 1
    assert wang_list["items"][0]["approval_id"] == ap_east["approval_id"]

    # Invisible order_id returns empty list (not 403)
    wang_north = client.get(
        "/guard/v1/approvals?order_id=B2087",
        headers=auth_headers("wang"),
    ).json()
    assert wang_north["total"] == 0
    assert wang_north["items"] == []

    # sun sees both, sorted newest first; pagination and status filter work
    sun_all = client.get(
        "/guard/v1/approvals?page=1&page_size=1",
        headers=auth_headers("sun"),
    ).json()
    assert sun_all["total"] == 2
    assert len(sun_all["items"]) == 1
    assert sun_all["items"][0]["approval_id"] == ap_north["approval_id"]

    sun_pending = client.get(
        "/guard/v1/approvals?status=pending&order_id=A1023",
        headers=auth_headers("sun"),
    ).json()
    assert sun_pending["total"] == 1
    assert sun_pending["items"][0]["order_id"] == "A1023"

    # Invalid query parameter -> 422 validation_error
    r_bad = client.get("/guard/v1/approvals?page_size=100", headers=auth_headers("wang"))
    assert r_bad.status_code == 422
    assert r_bad.json()["code"] == "validation_error"


def test_all_six_status_messages(
    client: TestClient,
    fake_upstream: FakeUpstream,
    clock: MutableClock,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. pending
    ap1 = _submit(client, auth_headers("wang"), "A1023", amount=50)
    assert ap1["status"] == "pending"
    assert ap1["message"] == f"已提交退款申请 {ap1['approval_id']}，等待主管审批。退款尚未执行。"

    # 2. executed
    client.post(
        f"/guard/v1/console/approvals/{ap1['approval_id']}/approve",
        json={"comment": "同意"},
        headers=auth_headers("li"),
    )
    got1 = client.get(
        f"/guard/v1/approvals/{ap1['approval_id']}",
        headers=auth_headers("wang"),
    ).json()
    assert got1["status"] == "executed"
    assert (
        got1["message"]
        == f"主管已同意退款申请 {ap1['approval_id']}，退款已执行，退款单号 R000105。"
    )

    # 3. rejected
    ap2 = _submit(client, auth_headers("wang"), "A1024", amount=50)
    client.post(
        f"/guard/v1/console/approvals/{ap2['approval_id']}/reject",
        json={"comment": "金额有误"},
        headers=auth_headers("li"),
    )
    got2 = client.get(
        f"/guard/v1/approvals/{ap2['approval_id']}",
        headers=auth_headers("wang"),
    ).json()
    assert got2["status"] == "rejected"
    assert got2["message"] == f"主管已拒绝退款申请 {ap2['approval_id']}：金额有误。退款未执行。"

    # 4. execution_failed
    ap3 = _submit(client, auth_headers("wang"), "A1029", amount=10)
    client.post(
        f"/guard/v1/console/approvals/{ap3['approval_id']}/approve",
        headers=auth_headers("li"),
    )
    got3 = client.get(
        f"/guard/v1/approvals/{ap3['approval_id']}",
        headers=auth_headers("wang"),
    ).json()
    assert got3["status"] == "execution_failed"
    assert got3["message"] == (
        f"主管已同意退款申请 {ap3['approval_id']}，但订单系统拒绝执行："
        "订单 A1029 尚未支付，不能退款。退款未执行，如仍需退款请重新提交申请。"
    )

    # 5. executing
    ap4 = _submit(client, auth_headers("wang"), "A1024", amount=20)
    fake_upstream.handler = lambda _req: httpx.Response(500, json={"code": "err", "message": "x"})
    client.post(
        f"/guard/v1/console/approvals/{ap4['approval_id']}/approve",
        headers=auth_headers("li"),
    )
    got4 = client.get(
        f"/guard/v1/approvals/{ap4['approval_id']}",
        headers=auth_headers("wang"),
    ).json()
    assert got4["status"] == "executing"
    assert (
        got4["message"] == f"主管已同意退款申请 {ap4['approval_id']}，退款正在执行，结果尚未确认。"
    )

    # 6. expired
    fake_upstream.handler = None
    ap5 = _submit(client, auth_headers("wang"), "A1023", amount=20)
    clock.advance(hours=73)
    got5 = client.get(
        f"/guard/v1/approvals/{ap5['approval_id']}",
        headers=auth_headers("wang"),
    ).json()
    assert got5["status"] == "expired"
    assert (
        got5["message"]
        == f"退款申请 {ap5['approval_id']} 超过 72 小时未审批，已自动失效。退款未执行。"
    )
