from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import approvals
from app.config import Settings
from app.log import JsonFormatter
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
            "reason": "测试退款申请",
        },
        headers=headers,
    )
    assert r.status_code == 202, r.text
    return r.json()


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize(
    ("region_order", "applicant", "actor", "expected_status", "expected_code"),
    [
        # East order submitted by wang (u1001)
        ("A1023", "wang", "wang", 403, "insufficient_role"),
        ("A1023", "wang", "zhao", 403, "insufficient_role"),
        ("A1023", "wang", "chen", 403, "insufficient_role"),
        ("A1023", "wang", "li", 200, None),
        ("A1023", "wang", "zhou", 403, "approval_access_denied"),
        ("A1023", "wang", "sun", 200, None),
        # North order submitted by zhao (u1002)
        ("B2087", "zhao", "wang", 403, "insufficient_role"),
        ("B2087", "zhao", "zhao", 403, "insufficient_role"),
        ("B2087", "zhao", "chen", 403, "insufficient_role"),
        ("B2087", "zhao", "li", 403, "approval_access_denied"),
        ("B2087", "zhao", "zhou", 200, None),
        ("B2087", "zhao", "sun", 200, None),
        # South order submitted by chen (u1003)
        ("C3001", "chen", "wang", 403, "insufficient_role"),
        ("C3001", "chen", "zhao", 403, "insufficient_role"),
        ("C3001", "chen", "chen", 403, "insufficient_role"),
        ("C3001", "chen", "li", 403, "approval_access_denied"),
        ("C3001", "chen", "zhou", 200, None),
        ("C3001", "chen", "sun", 200, None),
    ],
)
def test_permission_matrix_all_6_accounts_3_regions(
    build_app: Callable[..., FastAPI],
    auth_headers: Callable[..., dict[str, str]],
    action: str,
    region_order: str,
    applicant: str,
    actor: str,
    expected_status: int,
    expected_code: str | None,
) -> None:
    app = build_app(custom_upstream=FakeUpstream())
    with TestClient(app) as tc:
        ap = _submit(tc, auth_headers(applicant), region_order)
        body = {"comment": "审批意见"}
        r = tc.post(
            f"/guard/v1/console/approvals/{ap['approval_id']}/{action}",
            json=body,
            headers=auth_headers(actor),
        )
        assert r.status_code == expected_status
        if expected_code is not None:
            assert r.json()["code"] == expected_code
        else:
            assert r.json()["status"] == ("executed" if action == "approve" else "rejected")


def test_self_approval_forbidden_and_toggle(
    build_app: Callable[..., FastAPI],
    settings: Settings,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Default allow_self_approval=False blocks approve, reject, and retry by applicant
    app1 = build_app(custom_upstream=FakeUpstream())
    with TestClient(app1) as tc:
        ap = _submit(tc, auth_headers("sun"), "A1023")
        r_app = tc.post(
            f"/guard/v1/console/approvals/{ap['approval_id']}/approve",
            json={"comment": "自批"},
            headers=auth_headers("sun"),
        )
        assert r_app.status_code == 403
        assert r_app.json()["code"] == "self_approval_forbidden"

        r_rej = tc.post(
            f"/guard/v1/console/approvals/{ap['approval_id']}/reject",
            json={"comment": "自拒"},
            headers=auth_headers("sun"),
        )
        assert r_rej.status_code == 403
        assert r_rej.json()["code"] == "self_approval_forbidden"

    # With allow_self_approval=True, sun can approve own request
    custom_cfg = settings.model_copy(update={"allow_self_approval": True})
    app2 = build_app(custom_settings=custom_cfg, custom_upstream=FakeUpstream())
    with TestClient(app2) as tc:
        ap = _submit(tc, auth_headers("sun"), "A1023")
        r = tc.post(
            f"/guard/v1/console/approvals/{ap['approval_id']}/approve",
            headers=auth_headers("sun"),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "executed"


def test_reject_requires_comment_and_validation(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023")
    ap_id = ap["approval_id"]

    # Missing comment / blank / empty body -> 422
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/reject",
            json={},
            headers=auth_headers("li"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/reject",
            json={"comment": "   "},
            headers=auth_headers("li"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/reject",
            content=b"",
            headers=auth_headers("li"),
        ).status_code
        == 422
    )

    # Approve with too-long comment (>200) -> 422
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "x" * 201},
            headers=auth_headers("li"),
        ).status_code
        == 422
    )
    # Approve with bad JSON -> 422
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            content=b"{bad",
            headers={**auth_headers("li"), "Content-Type": "application/json"},
        ).status_code
        == 422
    )

    # Valid reject succeeds
    r = client.post(
        f"/guard/v1/console/approvals/{ap_id}/reject",
        json={"comment": "不符合无理由退货期限"},
        headers=auth_headers("li"),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert "不符合无理由退货期限" in r.json()["message"]

    # Rejecting again -> 409 approval_not_pending
    r2 = client.post(
        f"/guard/v1/console/approvals/{ap_id}/reject",
        json={"comment": "再次拒绝"},
        headers=auth_headers("li"),
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "approval_not_pending"

    # Nonexistent approval -> 404 for approve/reject/retry
    for action, payload in [("approve", {}), ("reject", {"comment": "无"}), ("retry", None)]:
        resp = client.post(
            f"/guard/v1/console/approvals/AP999999/{action}",
            json=payload,
            headers=auth_headers("li"),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "approval_not_found"


def test_approve_sends_approver_token_and_execution_key(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    wang_headers = auth_headers("wang")
    li_headers = auth_headers("li")
    ap = _submit(client, wang_headers, "A1023", amount=120.5)
    ap_id = ap["approval_id"]

    r = client.post(
        f"/guard/v1/console/approvals/{ap_id}/approve",
        json={"comment": "同意退款"},
        headers=li_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executed"
    assert body["refund_id"] == "R000105"
    assert body["decided_by_sub"] == "u2001"
    assert body["decided_by_name"] == "李娜"
    assert body["decision_comment"] == "同意退款"
    assert "R000105" in body["message"]

    exec_req = fake_upstream.requests[-1]
    assert exec_req.method == "POST"
    assert exec_req.path == "/api/v1/refunds"
    assert exec_req.headers["authorization"] == li_headers["Authorization"]
    sent_json = exec_req.json()
    assert sent_json["order_id"] == "A1023"
    assert sent_json["amount"] == 120.5
    assert sent_json["reason_category"] == "quality_issue"
    assert sent_json["reason"] == "测试退款申请"
    assert sent_json["idempotency_key"].startswith(f"{ap_id}-")
    assert len(sent_json["idempotency_key"]) == len(ap_id) + 1 + 8


def test_execution_matrix_409_422_401_403_500_timeout_and_retry(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. Business rejection 409 (unpaid order A1029) -> execution_failed
    ap1 = _submit(client, auth_headers("wang"), "A1029", amount=10)
    r1 = client.post(
        f"/guard/v1/console/approvals/{ap1['approval_id']}/approve",
        json={"comment": "同意"},
        headers=auth_headers("li"),
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "execution_failed"
    assert r1.json()["failure_code"] == "order_not_refundable"
    assert "拒绝执行" in r1.json()["message"]

    # 2. Upstream 422 -> execution_failed
    ap2 = _submit(client, auth_headers("wang"), "A1023", amount=10)
    fake_upstream.handler = lambda req: httpx.Response(
        422,
        json={"code": "validation_error", "message": "参数错误", "request_id": "r422"},
    )
    r2 = client.post(
        f"/guard/v1/console/approvals/{ap2['approval_id']}/approve",
        headers=auth_headers("li"),
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "execution_failed"
    assert r2.json()["failure_code"] == "validation_error"

    # 3. First execute 401 and 403 -> rollback to pending, verbatim status returned
    fake_upstream.handler = None
    ap3 = _submit(client, auth_headers("wang"), "A1024", amount=10)
    ap3_id = ap3["approval_id"]

    for code_num, err_code in [(401, "token_expired"), (403, "order_access_denied")]:
        fake_upstream.handler = lambda req, s=code_num, c=err_code: httpx.Response(
            s,
            json={"code": c, "message": "拒绝", "request_id": "r-auth"},
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'} if s == 401 else {},
        )
        r_auth = client.post(
            f"/guard/v1/console/approvals/{ap3_id}/approve",
            json={"comment": "尝试同意"},
            headers=auth_headers("li"),
        )
        assert r_auth.status_code == code_num
        assert r_auth.json()["code"] == err_code
        # Approval rolled back cleanly to pending
        detail = client.get(
            f"/guard/v1/console/approvals/{ap3_id}",
            headers=auth_headers("li"),
        ).json()
        assert detail["status"] == "pending"
        assert detail["attempts"] == 0
        assert "decided_by_sub" not in detail
        assert "decision_comment" not in detail

    # 4. Upstream 500 -> stays in executing; then retry timeout -> stays executing;
    #    then retry 403 -> STAYS executing (does NOT roll back to pending!);
    #    then retry 201 -> executed with SAME execution_key & incremented attempts!
    fake_upstream.handler = lambda req: httpx.Response(
        500,
        json={"code": "internal_error", "message": "后端故障", "request_id": "r500"},
    )
    r_500 = client.post(
        f"/guard/v1/console/approvals/{ap3_id}/approve",
        json={"comment": "同意"},
        headers=auth_headers("li"),
    )
    assert r_500.status_code == 200
    assert r_500.json()["status"] == "executing"
    first_exec_key = fake_upstream.requests[-1].json()["idempotency_key"]

    # Retry while executing -> timeout -> stays executing, attempts == 2
    def _timeout(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    fake_upstream.handler = _timeout
    r_to = client.post(
        f"/guard/v1/console/approvals/{ap3_id}/retry",
        headers=auth_headers("li"),
    )
    assert r_to.status_code == 200
    assert r_to.json()["status"] == "executing"
    assert fake_upstream.requests[-1].json()["idempotency_key"] == first_exec_key

    # Retry authorization checks while executing: agent -> 403, cross-region -> 403
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap3_id}/retry",
            headers=auth_headers("wang"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap3_id}/retry",
            headers=auth_headers("zhou"),
        ).status_code
        == 403
    )

    # Retry gets 403 from upstream -> MUST NOT roll back to pending!
    fake_upstream.handler = lambda req: httpx.Response(
        403,
        json={"code": "order_access_denied", "message": "上游拒绝", "request_id": "r403"},
    )
    r_r403 = client.post(
        f"/guard/v1/console/approvals/{ap3_id}/retry",
        headers=auth_headers("li"),
    )
    assert r_r403.status_code == 403
    detail_after_r403 = client.get(
        f"/guard/v1/console/approvals/{ap3_id}",
        headers=auth_headers("li"),
    ).json()
    assert detail_after_r403["status"] == "executing"
    assert detail_after_r403["attempts"] == 3

    # Final retry succeeds with sun's JWT using the SAME execution_key -> executed
    fake_upstream.handler = None
    r_ok = client.post(
        f"/guard/v1/console/approvals/{ap3_id}/retry",
        headers=auth_headers("sun"),
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "executed"
    assert r_ok.json()["refund_id"] == "R000105"
    assert fake_upstream.requests[-1].json()["idempotency_key"] == first_exec_key

    detail_final = client.get(
        f"/guard/v1/console/approvals/{ap3_id}",
        headers=auth_headers("sun"),
    ).json()
    assert detail_final["attempts"] == 4

    # Retry on non-executing (executed) -> 409 approval_not_pending
    r_again = client.post(
        f"/guard/v1/console/approvals/{ap3_id}/retry",
        headers=auth_headers("sun"),
    )
    assert r_again.status_code == 409
    assert r_again.json()["code"] == "approval_not_pending"


def test_concurrent_approve_only_one_succeeds(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=50)
    ap_id = ap["approval_id"]
    li_headers = auth_headers("li")
    sun_headers = auth_headers("sun")

    def _approve(hdrs: dict[str, str]) -> int:
        return client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "并发同意"},
            headers=hdrs,
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_approve, [li_headers, sun_headers, li_headers, sun_headers]))

    assert results.count(200) == 1
    assert results.count(409) == 3


def test_expiry_pending_expires_but_executing_never_expires(
    client: TestClient,
    fake_upstream: FakeUpstream,
    clock: MutableClock,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap_pending = _submit(client, auth_headers("wang"), "A1023", amount=30)
    ap_exec = _submit(client, auth_headers("wang"), "A1024", amount=40)

    # Put ap_exec into executing via upstream 500
    fake_upstream.handler = lambda req: httpx.Response(500, json={"code": "err", "message": "down"})
    r_exec = client.post(
        f"/guard/v1/console/approvals/{ap_exec['approval_id']}/approve",
        headers=auth_headers("li"),
    )
    assert r_exec.json()["status"] == "executing"

    # Advance clock past 72h TTL
    clock.advance(hours=73)

    # Approving expired pending approval -> 409 approval_not_pending
    r_exp = client.post(
        f"/guard/v1/console/approvals/{ap_pending['approval_id']}/approve",
        headers=auth_headers("li"),
    )
    assert r_exp.status_code == 409
    assert r_exp.json()["code"] == "approval_not_pending"
    assert "expired" in r_exp.json()["message"]

    # Check expired approval message
    got = client.get(
        f"/guard/v1/approvals/{ap_pending['approval_id']}",
        headers=auth_headers("wang"),
    ).json()
    assert got["status"] == "expired"
    assert "72 小时" in got["message"]

    # Meanwhile executing approval did NOT expire and can still be retried to completion
    fake_upstream.handler = None
    r_retry = client.post(
        f"/guard/v1/console/approvals/{ap_exec['approval_id']}/retry",
        headers=auth_headers("li"),
    )
    assert r_retry.status_code == 200
    assert r_retry.json()["status"] == "executed"

    # Also test lazy expiry commit when looking up a non-existent approval after TTL
    ap_pending2 = _submit(client, auth_headers("wang"), "A1023", amount=35)
    clock.advance(hours=73)
    assert (
        client.post(
            "/guard/v1/console/approvals/AP999999/approve",
            headers=auth_headers("li"),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/guard/v1/approvals/{ap_pending2['approval_id']}",
            headers=auth_headers("wang"),
        ).json()["status"]
        == "expired"
    )
    # Same for reject and retry hitting 404 while expiring another pending
    ap_pending3 = _submit(client, auth_headers("wang"), "A1023", amount=36)
    clock.advance(hours=73)
    assert (
        client.post(
            "/guard/v1/console/approvals/AP999999/reject",
            json={"comment": "x"},
            headers=auth_headers("li"),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/guard/v1/approvals/{ap_pending3['approval_id']}",
            headers=auth_headers("wang"),
        ).json()["status"]
        == "expired"
    )
    ap_pending4 = _submit(client, auth_headers("wang"), "A1023", amount=37)
    clock.advance(hours=73)
    assert (
        client.post(
            "/guard/v1/console/approvals/AP999999/retry",
            headers=auth_headers("li"),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/guard/v1/approvals/{ap_pending4['approval_id']}",
            headers=auth_headers("wang"),
        ).json()["status"]
        == "expired"
    )


def _replay_audit_status(events: list[dict[str, Any]]) -> str:
    ordered = sorted(events, key=lambda e: e["id"])
    state = ""
    for ev in ordered:
        if ev["event_type"] == "tool_call" and ev["outcome"] == "pending_approval":
            state = "pending"
        elif ev["event_type"].startswith("approval_") and ev.get("result"):
            res = ev["result"]
            assert res["from"] == state, f"Unexpected transition {res} from {state}"
            state = res["to"]
    return state


def test_first_execute_timeout_stays_executing_and_returns_200(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=60)
    ap_id = ap["approval_id"]

    def _timeout(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("first execute timeout")

    fake_upstream.handler = _timeout
    r = client.post(
        f"/guard/v1/console/approvals/{ap_id}/approve",
        json={"comment": "首次执行超时"},
        headers=auth_headers("li"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executing"
    assert "超时" in body["failure_message"]

    detail = client.get(
        f"/guard/v1/console/approvals/{ap_id}",
        headers=auth_headers("li"),
    ).json()
    assert detail["status"] == "executing"
    assert detail["attempts"] == 1

    # Retrying later completes to executed
    fake_upstream.handler = None
    r_retry = client.post(
        f"/guard/v1/console/approvals/{ap_id}/retry",
        headers=auth_headers("li"),
    )
    assert r_retry.status_code == 200
    assert r_retry.json()["status"] == "executed"
    assert r_retry.json()["refund_id"] == "R000105"


def test_interleaved_retry_2xx_then_first_timeout_preserves_executed(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=70)
    ap_id = ap["approval_id"]

    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def _interleaved(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if req.method == "POST" and req.url.path == "/api/v1/refunds":
            with lock:
                call_count += 1
                seq = call_count
            if seq == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
                raise httpx.ReadTimeout("late timeout")
            return httpx.Response(
                201,
                json={
                    "refund_id": "R000105",
                    "order_id": "A1023",
                    "region": "east",
                    "amount": 70.0,
                    "created_by": "u2003",
                },
            )
        raise AssertionError(f"Unexpected upstream request: {req.method} {req.url}")

    fake_upstream.handler = _interleaved
    li_headers = auth_headers("li")
    sun_headers = auth_headers("sun")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_first = pool.submit(
            client.post,
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "首次同意"},
            headers=li_headers,
        )
        assert first_started.wait(timeout=5)

        r_retry = client.post(
            f"/guard/v1/console/approvals/{ap_id}/retry",
            headers=sun_headers,
        )
        assert r_retry.status_code == 200
        assert r_retry.json()["status"] == "executed"
        assert r_retry.json()["refund_id"] == "R000105"

        release_first.set()
        r_first = f_first.result(timeout=5)

    assert r_first.status_code == 200
    assert r_first.json()["status"] == "executed"
    assert r_first.json()["refund_id"] == "R000105"
    assert "failure_message" not in r_first.json()

    detail = client.get(
        f"/guard/v1/console/approvals/{ap_id}",
        headers=sun_headers,
    ).json()
    assert detail["status"] == "executed"
    assert detail["refund_id"] == "R000105"
    assert "failure_message" not in detail

    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=sun_headers,
    ).json()["items"]
    assert _replay_audit_status(chain) == "executed"
    late_events = [e for e in chain if (e.get("result") or {}).get("stale") is True]
    assert len(late_events) == 1
    assert late_events[0]["event_type"] == "approval_execute"
    assert "status_code" not in late_events[0]
    assert late_events[0]["result"]["from"] == "executed"
    assert late_events[0]["result"]["to"] == "executed"


def test_interleaved_retry_2xx_then_first_401_preserves_executed(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=80)
    ap_id = ap["approval_id"]

    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def _interleaved(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if req.method == "POST" and req.url.path == "/api/v1/refunds":
            with lock:
                call_count += 1
                seq = call_count
            if seq == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
                return httpx.Response(
                    401,
                    json={"code": "token_expired", "message": "令牌已过期", "request_id": "r-401"},
                )
            return httpx.Response(
                201,
                json={
                    "refund_id": "R000105",
                    "order_id": "A1023",
                    "region": "east",
                    "amount": 80.0,
                    "created_by": "u2003",
                },
            )
        raise AssertionError(f"Unexpected upstream request: {req.method} {req.url}")

    fake_upstream.handler = _interleaved
    li_headers = auth_headers("li")
    sun_headers = auth_headers("sun")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_first = pool.submit(
            client.post,
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "首次同意"},
            headers=li_headers,
        )
        assert first_started.wait(timeout=5)

        r_retry = client.post(
            f"/guard/v1/console/approvals/{ap_id}/retry",
            headers=sun_headers,
        )
        assert r_retry.status_code == 200
        assert r_retry.json()["status"] == "executed"
        assert r_retry.json()["refund_id"] == "R000105"

        release_first.set()
        r_first = f_first.result(timeout=5)

    assert r_first.status_code == 200
    assert r_first.json()["status"] == "executed"
    assert r_first.json()["refund_id"] == "R000105"

    detail = client.get(
        f"/guard/v1/console/approvals/{ap_id}",
        headers=sun_headers,
    ).json()
    assert detail["status"] == "executed"
    assert detail["refund_id"] == "R000105"

    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=sun_headers,
    ).json()["items"]
    assert _replay_audit_status(chain) == "executed"
    late_events = [e for e in chain if (e.get("result") or {}).get("stale") is True]
    assert len(late_events) == 1
    assert late_events[0]["event_type"] == "approval_execute"
    assert late_events[0]["status_code"] == 401
    assert late_events[0]["error_code"] == "token_expired"
    assert late_events[0]["result"]["from"] == "executed"
    assert late_events[0]["result"]["to"] == "executed"


def test_interleaved_first_401_before_retry_rolls_back_and_retry_gets_409(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=90)
    ap_id = ap["approval_id"]

    fake_upstream.handler = lambda _req: httpx.Response(
        401,
        json={"code": "token_expired", "message": "令牌已过期", "request_id": "r-401"},
    )
    r_first = client.post(
        f"/guard/v1/console/approvals/{ap_id}/approve",
        json={"comment": "首次同意"},
        headers=auth_headers("li"),
    )
    assert r_first.status_code == 401

    r_retry = client.post(
        f"/guard/v1/console/approvals/{ap_id}/retry",
        headers=auth_headers("sun"),
    )
    assert r_retry.status_code == 409
    assert r_retry.json()["code"] == "approval_not_pending"

    detail = client.get(
        f"/guard/v1/console/approvals/{ap_id}",
        headers=auth_headers("sun"),
    ).json()
    assert detail["status"] == "pending"
    assert detail["attempts"] == 0

    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=auth_headers("sun"),
    ).json()["items"]
    assert _replay_audit_status(chain) == "pending"


def test_interleaved_first_401_while_retry_in_flight_does_not_rollback_to_pending(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=95)
    ap_id = ap["approval_id"]

    first_started = threading.Event()
    retry_started = threading.Event()
    release_first = threading.Event()
    release_retry = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def _interleaved(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if req.method == "POST" and req.url.path == "/api/v1/refunds":
            with lock:
                call_count += 1
                seq = call_count
            if seq == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
                return httpx.Response(
                    401,
                    json={"code": "token_expired", "message": "令牌过期", "request_id": "r-401"},
                )
            retry_started.set()
            assert release_retry.wait(timeout=5)
            return httpx.Response(
                201,
                json={
                    "refund_id": "R000105",
                    "order_id": "A1023",
                    "region": "east",
                    "amount": 95.0,
                    "created_by": "u2003",
                },
            )
        raise AssertionError(f"Unexpected upstream request: {req.method} {req.url}")

    fake_upstream.handler = _interleaved
    li_headers = auth_headers("li")
    sun_headers = auth_headers("sun")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_first = pool.submit(
            client.post,
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "首次同意"},
            headers=li_headers,
        )
        assert first_started.wait(timeout=5)

        f_retry = pool.submit(
            client.post,
            f"/guard/v1/console/approvals/{ap_id}/retry",
            headers=sun_headers,
        )
        assert retry_started.wait(timeout=5)

        # First request returns 401 while attempts == 2 -> MUST stay executing (NOT pending)
        release_first.set()
        r_first = f_first.result(timeout=5)
        assert r_first.status_code == 401

        mid_detail = client.get(
            f"/guard/v1/console/approvals/{ap_id}",
            headers=sun_headers,
        ).json()
        assert mid_detail["status"] == "executing"
        assert mid_detail["attempts"] == 2
        assert mid_detail["failure_status"] == 401

        # Now retry succeeds -> executed
        release_retry.set()
        r_retry = f_retry.result(timeout=5)
        assert r_retry.status_code == 200
        assert r_retry.json()["status"] == "executed"
        assert r_retry.json()["refund_id"] == "R000105"

    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=sun_headers,
    ).json()["items"]
    assert _replay_audit_status(chain) == "executed"


def test_access_denied_message_never_reveals_region_name(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap_east = _submit(client, auth_headers("wang"), "A1023", amount=50)
    ap_id = ap_east["approval_id"]

    # Put a second east approval into executing to also test retry denial
    ap_exec = _submit(client, auth_headers("wang"), "A1024", amount=50)
    fake_upstream.handler = lambda _req: httpx.Response(500, json={"code": "err", "message": "x"})
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_exec['approval_id']}/approve",
            headers=auth_headers("li"),
        ).status_code
        == 200
    )

    forbidden_responses = [
        client.get(f"/guard/v1/approvals/{ap_id}", headers=auth_headers("zhao")),
        client.get(f"/guard/v1/console/approvals/{ap_id}", headers=auth_headers("zhou")),
        client.get(f"/guard/v1/console/approvals/{ap_id}/order", headers=auth_headers("zhou")),
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "跨区同意"},
            headers=auth_headers("zhou"),
        ),
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/reject",
            json={"comment": "跨区拒绝"},
            headers=auth_headers("zhou"),
        ),
        client.post(
            f"/guard/v1/console/approvals/{ap_exec['approval_id']}/retry",
            headers=auth_headers("zhou"),
        ),
    ]
    region_tokens = ("east", "north", "south", "华东", "华北", "华南")
    for resp in forbidden_responses:
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "approval_access_denied"
        msg = body["message"].lower()
        for token in region_tokens:
            assert token not in msg, f"Region {token!r} leaked in message: {body['message']!r}"


def test_execution_timestamp_uses_post_upstream_clock(
    client: TestClient,
    fake_upstream: FakeUpstream,
    clock: MutableClock,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=50)
    ap_id = ap["approval_id"]

    # Advance clock during upstream POST /api/v1/refunds
    def _slow_refund(req: httpx.Request) -> httpx.Response:
        clock.advance(seconds=15)
        return httpx.Response(
            201,
            json={
                "refund_id": "R000105",
                "order_id": "A1023",
                "region": "east",
                "amount": 50.0,
                "created_by": "u2001",
            },
        )

    fake_upstream.handler = _slow_refund
    r = client.post(
        f"/guard/v1/console/approvals/{ap_id}/approve",
        json={"comment": "时钟先后测试"},
        headers={**auth_headers("li"), "X-Request-Id": "rid-clock-order"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decided_at"] == "2026-11-20T12:00:00+08:00"
    assert body["executed_at"] == "2026-11-20T12:00:15+08:00"

    logs = sorted(
        client.get(
            "/guard/v1/console/audit-logs?request_id=rid-clock-order",
            headers=auth_headers("sun"),
        ).json()["items"],
        key=lambda e: e["id"],
    )
    assert len(logs) == 2
    assert logs[0]["event_type"] == "approval_approve"
    assert logs[0]["ts"] == "2026-11-20T12:00:00+08:00"
    assert logs[1]["event_type"] == "approval_execute"
    assert logs[1]["ts"] == "2026-11-20T12:00:15+08:00"
    assert logs[1]["ts"] > logs[0]["ts"]

    # Also verify retry path advances execution timestamp after upstream returns
    fake_upstream.handler = None
    ap2 = _submit(client, auth_headers("wang"), "A1024", amount=40)
    ap2_id = ap2["approval_id"]
    fake_upstream.handler = lambda _req: httpx.Response(500, json={"code": "err", "message": "x"})
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap2_id}/approve",
            headers=auth_headers("li"),
        ).status_code
        == 200
    )

    def _slow_retry(req: httpx.Request) -> httpx.Response:
        clock.advance(seconds=20)
        return httpx.Response(
            201,
            json={
                "refund_id": "R000106",
                "order_id": "A1024",
                "region": "east",
                "amount": 40.0,
                "created_by": "u2001",
            },
        )

    fake_upstream.handler = _slow_retry
    r_retry = client.post(
        f"/guard/v1/console/approvals/{ap2_id}/retry",
        headers={**auth_headers("li"), "X-Request-Id": "rid-retry-clock"},
    )
    assert r_retry.status_code == 200
    assert r_retry.json()["executed_at"] == "2026-11-20T12:00:35+08:00"
    retry_logs = client.get(
        "/guard/v1/console/audit-logs?request_id=rid-retry-clock",
        headers=auth_headers("sun"),
    ).json()["items"]
    assert len(retry_logs) == 1
    assert retry_logs[0]["ts"] == "2026-11-20T12:00:35+08:00"


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize(
    ("raw_body", "expected_arg_check"),
    [
        (b'{"comment": NaN}', lambda args: args.get("comment") == "NaN"),
        (b'{"comment": Infinity}', lambda args: args.get("comment") == "Infinity"),
        (b'{"comment": 1e400}', lambda args: args.get("comment") == "Infinity"),
        (
            b'{"comment": ' + b"9" * 5000 + b"}",
            lambda args: isinstance(args.get("comment"), str) and len(args["comment"]) == 500,
        ),
        (
            ('{"x":' + "[" * 600 + "1" + "]" * 600 + "}").encode(),
            lambda args: "[max_depth_exceeded]" in str(args),
        ),
        (b'{"comment": "\\ud800"}', lambda args: args.get("comment") == "\ufffd"),
    ],
)
def test_approve_and_reject_non_finite_and_overlong_int_return_422_and_safe_audit(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    action: str,
    raw_body: bytes,
    expected_arg_check: Callable[[dict[str, Any]], bool],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=50)
    ap_id = ap["approval_id"]
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]

    r = client.post(
        f"/guard/v1/console/approvals/{ap_id}/{action}",
        content=raw_body,
        headers={**auth_headers("li"), "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"

    audit_resp = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
    assert audit_resp.status_code == 200
    assert audit_resp.json()["total"] == before_logs + 1
    ev = audit_resp.json()["items"][0]
    assert ev["status_code"] == 422
    assert ev["error_code"] == "validation_error"
    assert expected_arg_check(ev["arguments"])

    detail_resp = client.get(f"/guard/v1/console/approvals/{ap_id}", headers=auth_headers("li"))
    assert detail_resp.status_code == 200
    assert detail_resp.json()["status"] == "pending"


def test_interleaved_4xx_execution_failed_then_late_2xx_logs_conflict_and_surfaces_notice(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=85)
    ap_id = ap["approval_id"]

    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def _interleaved(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if req.method == "POST" and req.url.path == "/api/v1/refunds":
            with lock:
                call_count += 1
                seq = call_count
            if seq == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
                return httpx.Response(
                    201,
                    json={
                        "refund_id": "R000105",
                        "order_id": "A1023",
                        "region": "east",
                        "amount": 85.0,
                        "created_by": "u2001",
                    },
                )
            return httpx.Response(
                409,
                json={
                    "code": "order_not_refundable",
                    "message": "订单状态已变更，不可退款。",
                    "request_id": "r-409",
                },
            )
        raise AssertionError(f"Unexpected upstream request: {req.method} {req.url}")

    captured_records: list[tuple[int, dict[str, Any]]] = []
    formatter = JsonFormatter(tz_name="Asia/Shanghai")

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append((record.levelno, json.loads(formatter.format(record))))

    capture = _CaptureHandler()
    guard_logger = logging.getLogger("guard")
    guard_logger.addHandler(capture)

    fake_upstream.handler = _interleaved
    li_headers = {**auth_headers("li"), "X-Request-Id": "rid-late-2xx-conflict"}
    sun_headers = auth_headers("sun")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_first = pool.submit(
                client.post,
                f"/guard/v1/console/approvals/{ap_id}/approve",
                json={"comment": "首次同意"},
                headers=li_headers,
            )
            assert first_started.wait(timeout=5)

            # Second attempt fails with 409 -> terminal execution_failed
            r_retry = client.post(
                f"/guard/v1/console/approvals/{ap_id}/retry",
                headers=sun_headers,
            )
            assert r_retry.status_code == 200
            assert r_retry.json()["status"] == "execution_failed"

            # Before late 2xx arrives, console detail & list on execution_failed have no conflict
            mid_detail = client.get(
                f"/guard/v1/console/approvals/{ap_id}",
                headers=sun_headers,
            ).json()
            assert mid_detail["status"] == "execution_failed"
            assert mid_detail["has_late_execution_conflict"] is False
            assert "late_executed_refund_id" not in mid_detail
            assert "late_execution_notice" not in mid_detail

            mid_list = client.get("/guard/v1/console/approvals", headers=sun_headers).json()
            assert mid_list["items"][0]["approval_id"] == ap_id
            assert mid_list["items"][0]["has_late_execution_conflict"] is False

            # Release first attempt which succeeds with 201 -> stale 2xx on execution_failed
            release_first.set()
            r_first = f_first.result(timeout=5)
            assert r_first.status_code == 200
            assert r_first.json()["status"] == "execution_failed"
    finally:
        guard_logger.removeHandler(capture)

    # Agent API remains strictly on the 22-field Approval schema (no console-only conflict fields)
    agent_detail = client.get(f"/guard/v1/approvals/{ap_id}", headers=auth_headers("wang")).json()
    assert agent_detail["status"] == "execution_failed"
    assert "has_late_execution_conflict" not in agent_detail
    assert "late_executed_refund_id" not in agent_detail
    assert "late_execution_notice" not in agent_detail

    # Console detail & list surface conflict flag, late_executed_refund_id, and conflict notice
    console_detail = client.get(
        f"/guard/v1/console/approvals/{ap_id}",
        headers=sun_headers,
    ).json()
    assert console_detail["status"] == "execution_failed"
    assert console_detail["has_late_execution_conflict"] is True
    assert console_detail["late_executed_refund_id"] == "R000105"
    assert (
        console_detail["late_execution_notice"]
        == "订单系统另有一次执行成功，退款单号 R000105，请人工核对"
    )

    console_list = client.get("/guard/v1/console/approvals", headers=sun_headers).json()
    assert console_list["items"][0]["approval_id"] == ap_id
    assert console_list["items"][0]["has_late_execution_conflict"] is True

    # Audit chain replays to execution_failed and stale event captures refund_id + created_by
    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=sun_headers,
    ).json()["items"]
    assert _replay_audit_status(chain) == "execution_failed"
    stale_events = [e for e in chain if (e.get("result") or {}).get("stale") is True]
    assert len(stale_events) == 1
    assert stale_events[0]["event_type"] == "approval_execute"
    assert stale_events[0]["status_code"] == 201
    assert stale_events[0]["result"] == {
        "from": "execution_failed",
        "to": "execution_failed",
        "stale": True,
        "note": "迟到的执行结果，未改变状态",
        "refund_id": "R000105",
        "created_by": "u2001",
    }

    # Structured ERROR log execution_result_conflict emitted with approval_id, refund_id, request_id
    conflict_logs = [
        payload
        for lvl, payload in captured_records
        if lvl == logging.ERROR and payload.get("event") == "execution_result_conflict"
    ]
    assert len(conflict_logs) == 1
    assert conflict_logs[0]["approval_id"] == ap_id
    assert conflict_logs[0]["refund_id"] == "R000105"
    assert conflict_logs[0]["request_id"] == "rid-late-2xx-conflict"


def test_interleaved_2xx_executed_then_late_2xx_records_refund_id_without_conflict(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1024", amount=60)
    ap_id = ap["approval_id"]

    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def _interleaved(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if req.method == "POST" and req.url.path == "/api/v1/refunds":
            with lock:
                call_count += 1
                seq = call_count
            if seq == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
                return httpx.Response(
                    200,
                    json={
                        "refund_id": "R000106",
                        "order_id": "A1024",
                        "region": "east",
                        "amount": 60.0,
                        "created_by": "u2001",
                    },
                )
            return httpx.Response(
                201,
                json={
                    "refund_id": "R000105",
                    "order_id": "A1024",
                    "region": "east",
                    "amount": 60.0,
                    "created_by": "u2003",
                },
            )
        raise AssertionError(f"Unexpected upstream request: {req.method} {req.url}")

    captured_records: list[tuple[int, dict[str, Any]]] = []
    formatter = JsonFormatter(tz_name="Asia/Shanghai")

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append((record.levelno, json.loads(formatter.format(record))))

    capture = _CaptureHandler()
    guard_logger = logging.getLogger("guard")
    guard_logger.addHandler(capture)

    fake_upstream.handler = _interleaved
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_first = pool.submit(
                client.post,
                f"/guard/v1/console/approvals/{ap_id}/approve",
                json={"comment": "首次同意"},
                headers=auth_headers("li"),
            )
            assert first_started.wait(timeout=5)

            r_retry = client.post(
                f"/guard/v1/console/approvals/{ap_id}/retry",
                headers=auth_headers("sun"),
            )
            assert r_retry.status_code == 200
            assert r_retry.json()["status"] == "executed"

            release_first.set()
            r_first = f_first.result(timeout=5)
            assert r_first.status_code == 200
            assert r_first.json()["status"] == "executed"
    finally:
        guard_logger.removeHandler(capture)

    detail = client.get(
        f"/guard/v1/console/approvals/{ap_id}",
        headers=auth_headers("sun"),
    ).json()
    assert detail["status"] == "executed"
    assert detail["refund_id"] == "R000105"
    assert "late_executed_refund_id" not in detail
    assert "late_execution_notice" not in detail

    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=auth_headers("sun"),
    ).json()["items"]
    stale_events = [e for e in chain if (e.get("result") or {}).get("stale") is True]
    assert len(stale_events) == 1
    assert stale_events[0]["result"]["refund_id"] == "R000106"
    assert stale_events[0]["result"]["created_by"] == "u2001"
    assert not any(
        payload.get("event") == "execution_result_conflict" for _, payload in captured_records
    )


@pytest.mark.parametrize(
    "late_response",
    [
        httpx.Response(201, json={"order_id": "A1023", "status": "succeeded"}),
        httpx.Response(200, content=b"<html>ok-but-not-json</html>"),
    ],
)
def test_interleaved_4xx_then_late_2xx_without_refund_id_surfaces_unknown_conflict(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    late_response: httpx.Response,
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=85)
    ap_id = ap["approval_id"]

    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def _interleaved(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if req.method == "POST" and req.url.path == "/api/v1/refunds":
            with lock:
                call_count += 1
                seq = call_count
            if seq == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
                return late_response
            return httpx.Response(
                409,
                json={
                    "code": "order_not_refundable",
                    "message": "订单状态已变更，不可退款。",
                    "request_id": "r-409",
                },
            )
        raise AssertionError(f"Unexpected upstream request: {req.method} {req.url}")

    captured_records: list[tuple[int, dict[str, Any]]] = []
    formatter = JsonFormatter(tz_name="Asia/Shanghai")

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append((record.levelno, json.loads(formatter.format(record))))

    capture = _CaptureHandler()
    guard_logger = logging.getLogger("guard")
    guard_logger.addHandler(capture)

    fake_upstream.handler = _interleaved
    li_headers = {**auth_headers("li"), "X-Request-Id": "rid-late-2xx-no-refund-id"}
    sun_headers = auth_headers("sun")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_first = pool.submit(
                client.post,
                f"/guard/v1/console/approvals/{ap_id}/approve",
                json={"comment": "首次同意"},
                headers=li_headers,
            )
            assert first_started.wait(timeout=5)

            r_retry = client.post(
                f"/guard/v1/console/approvals/{ap_id}/retry",
                headers=sun_headers,
            )
            assert r_retry.status_code == 200
            assert r_retry.json()["status"] == "execution_failed"

            release_first.set()
            r_first = f_first.result(timeout=5)
            assert r_first.status_code == 200
            assert r_first.json()["status"] == "execution_failed"
    finally:
        guard_logger.removeHandler(capture)

    # Console detail surfaces conflict flag & unknown-refund-id notice
    detail = client.get(f"/guard/v1/console/approvals/{ap_id}", headers=sun_headers).json()
    assert detail["status"] == "execution_failed"
    assert detail["has_late_execution_conflict"] is True
    assert "late_executed_refund_id" not in detail
    assert (
        detail["late_execution_notice"]
        == "订单系统另有一次执行成功，退款单号未知，请到订单系统人工核对"
    )

    # Console list also marks has_late_execution_conflict=True
    listed = client.get("/guard/v1/console/approvals", headers=sun_headers).json()
    assert listed["items"][0]["approval_id"] == ap_id
    assert listed["items"][0]["has_late_execution_conflict"] is True

    # Agent API remains strictly on the 22-field contract
    agent_detail = client.get(f"/guard/v1/approvals/{ap_id}", headers=auth_headers("wang")).json()
    assert "has_late_execution_conflict" not in agent_detail
    assert "late_execution_notice" not in agent_detail

    # Structured ERROR log emitted even without refund_id
    conflict_logs = [
        payload
        for lvl, payload in captured_records
        if lvl == logging.ERROR and payload.get("event") == "execution_result_conflict"
    ]
    assert len(conflict_logs) == 1
    assert conflict_logs[0]["approval_id"] == ap_id
    assert conflict_logs[0]["refund_id"] == ""
    assert conflict_logs[0]["request_id"] == "rid-late-2xx-no-refund-id"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_approve_and_reject_oversized_body_returns_413_and_empty_audit(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    ap = _submit(client, auth_headers("wang"), "A1023", amount=50)
    ap_id = ap["approval_id"]

    def _forbid_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Oversized body must not enter JSON parsing")

    monkeypatch.setattr(approvals, "_parse_json_dict", _forbid_parse)
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]

    oversized = b'{"comment": "' + b"a" * 65536 + b'"}'
    r = client.post(
        f"/guard/v1/console/approvals/{ap_id}/{action}",
        content=oversized,
        headers={**auth_headers("li"), "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    body = r.json()
    assert body["code"] == "payload_too_large"
    assert body["message"]
    assert body["request_id"] == r.headers["X-Request-Id"]

    after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["event_type"] == f"approval_{action}"
    assert ev["approval_id"] == ap_id
    assert ev["arguments"] == {}
    assert ev["status_code"] == 413
    assert ev["error_code"] == "payload_too_large"
    assert ev["outcome"] == "invalid"

    detail = client.get(f"/guard/v1/console/approvals/{ap_id}", headers=auth_headers("li")).json()
    assert detail["status"] == "pending"
