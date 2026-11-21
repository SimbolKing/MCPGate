from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import approvals
from app.config import Settings
from app.main import create_app
from app.manifest import ToolManifest
from tests.conftest import MANIFEST_PATH, FakeUpstream


def _valid_refund_body(order_id: str = "A1023", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "order_id": order_id,
        "amount": 299.0,
        "reason_category": "quality_issue",
        "reason": "右耳无声，按售后制度支持退货退款",
        "ticket_id": "T5001",
    }
    payload.update(overrides)
    return payload


def test_submit_refund_intercepts_and_returns_202_without_calling_upstream_post(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    r = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023"),
        headers=auth_headers("wang"),
    )
    assert r.status_code == 202
    assert r.headers["Location"] == "/guard/v1/approvals/AP000001"
    data = r.json()
    assert data["approval_id"] == "AP000001"
    assert data["status"] == "pending"
    assert data["message"] == "已提交退款申请 AP000001，等待主管审批。退款尚未执行。"
    assert data["tool"] == "create_refund"
    assert data["order_id"] == "A1023"
    assert data["region"] == "east"
    assert data["amount"] == 299.0
    assert data["reason_category"] == "quality_issue"
    assert data["ticket_id"] == "T5001"
    assert data["applicant_sub"] == "u1001"
    assert data["applicant_name"] == "王磊"
    assert data["submitted_at"].endswith("+08:00")
    assert data["expires_at"].endswith("+08:00")
    # Null fields omitted (G6)
    assert "decided_by_sub" not in data
    assert "refund_id" not in data
    assert "failure_code" not in data

    # Upstream ONLY received GET /api/v1/orders/A1023 precheck, NEVER POST /api/v1/refunds!
    assert len(fake_upstream.requests) == 1
    assert fake_upstream.requests[0].method == "GET"
    assert fake_upstream.requests[0].path == "/api/v1/orders/A1023"
    assert not any(
        req.method == "POST" and req.path == "/api/v1/refunds" for req in fake_upstream.requests
    )

    # Check snapshot in console detail
    detail = client.get(
        "/guard/v1/console/approvals/AP000001",
        headers=auth_headers("li"),
    ).json()
    snap = detail["order_snapshot"]
    assert snap["status"] == "delivered"
    assert snap["total_amount"] == 299.0
    assert snap["refundable_amount"] == 299.0
    assert snap["delivered_at"] == "2026-11-17T14:30:00+08:00"
    assert snap["days_since_delivery"] == 3
    assert snap["customer"] == {"name": "张敏", "level": "vip"}
    assert snap["items"][0]["product_name"] == "蓝牙降噪耳机"
    assert snap["after_sale_tickets"][0]["ticket_id"] == "T5001"


def test_submit_region_comes_from_precheck_not_order_id_prefix(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Even if order_id starts with 'A', region must come from precheck response
    fake_upstream.orders["A9999"] = dict(
        fake_upstream.orders["B2087"], order_id="A9999", region="north"
    )
    r = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A9999"),
        headers=auth_headers("zhao"),
    )
    assert r.status_code == 202
    assert r.json()["region"] == "north"


def test_submit_validation_errors(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Negative amount
    r1 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=-1),
        headers=auth_headers("wang"),
    )
    assert r1.status_code == 422
    assert r1.json()["code"] == "validation_error"
    assert r1.json()["details"]

    # Too many decimal places / bool / blank reason / bad JSON / empty body / non-dict JSON
    r2 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=10.123),
        headers=auth_headers("wang"),
    )
    assert r2.status_code == 422

    r3 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=True),
        headers=auth_headers("wang"),
    )
    assert r3.status_code == 422

    r4 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", reason="   "),
        headers=auth_headers("wang"),
    )
    assert r4.status_code == 422

    r5 = client.post(
        "/api/v1/refunds",
        content=b"",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r5.status_code == 422

    r6 = client.post(
        "/api/v1/refunds",
        content=b"{bad json",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r6.status_code == 422

    r7 = client.post(
        "/api/v1/refunds",
        content=b"[1, 2, 3]",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r7.status_code == 422

    assert len(fake_upstream.requests) == 0


def test_submit_precheck_403_404_and_503_create_no_approval(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # wang (east) tries north order B2087 -> 403 order_access_denied
    r403 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("B2087", amount=10),
        headers=auth_headers("wang"),
    )
    assert r403.status_code == 403
    assert r403.json()["code"] == "order_access_denied"

    # Nonexistent order -> 404 order_not_found
    r404 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("X9999", amount=10),
        headers=auth_headers("wang"),
    )
    assert r404.status_code == 404
    assert r404.json()["code"] == "order_not_found"

    # Upstream timeout during precheck -> 503
    fake_upstream.handler = lambda _req: (_ for _ in ()).throw(httpx.ConnectTimeout("timeout"))
    r503 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=10),
        headers=auth_headers("wang"),
    )
    assert r503.status_code == 503
    assert r503.json()["code"] == "upstream_unavailable"

    # Zero approvals created
    listed = client.get("/guard/v1/approvals", headers=auth_headers("sun")).json()
    assert listed["total"] == 0


def test_submit_duplicate_order_and_idempotency(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Submit with client idempotency key
    r1 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=100, idempotency_key="idem-key-0001"),
        headers=auth_headers("wang"),
    )
    assert r1.status_code == 202
    assert r1.json()["approval_id"] == "AP000001"

    # Same idempotency key & same order_id/amount -> 200 replay of existing approval
    r2 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=100, idempotency_key="idem-key-0001"),
        headers=auth_headers("wang"),
    )
    assert r2.status_code == 200
    assert r2.json()["approval_id"] == "AP000001"

    # Same idempotency key with different amount -> 409 idempotency_conflict
    r3 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=200, idempotency_key="idem-key-0001"),
        headers=auth_headers("wang"),
    )
    assert r3.status_code == 409
    assert r3.json()["code"] == "idempotency_conflict"

    # Duplicate active approval on same order without idempotency key -> 409 approval_pending_exists
    r4 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=50),
        headers=auth_headers("wang"),
    )
    assert r4.status_code == 409
    assert r4.json()["code"] == "approval_pending_exists"
    assert "AP000001" in r4.json()["message"]

    # Supervisor submitting via agent path is also intercepted
    r5 = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1024", amount=50, ticket_id=None),
        headers=auth_headers("sun"),
    )
    assert r5.status_code == 202
    assert r5.json()["approval_id"] == "AP000002"
    assert "ticket_id" not in r5.json()


def test_concurrent_submit_same_order_only_one_succeeds(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    headers = auth_headers("wang")
    payload = _valid_refund_body("A1023", amount=80)

    def _call() -> int:
        return client.post("/api/v1/refunds", json=payload, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(lambda _: _call(), range(6)))

    assert statuses.count(202) == 1
    assert statuses.count(409) == 5


@pytest.mark.parametrize(
    ("order_id", "expected_encoded"),
    [
        ("A1023?", "A1023%3F"),
        ("A1023#x", "A1023%23x"),
        ("A1023/.", "A1023%2F."),
        ("../../../healthz", "..%2F..%2F..%2Fhealthz"),
    ],
)
def test_submit_special_order_ids_encoded_no_approval_and_audited(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    order_id: str,
    expected_encoded: str,
) -> None:
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body(order_id, amount=10),
        headers=auth_headers("wang"),
    )
    assert r.status_code == 404
    assert r.json()["code"] == "order_not_found"

    # Upstream received the percent-encoded literal segment, never /healthz or bare A1023
    sent = fake_upstream.requests[-1]
    assert sent.method == "GET"
    assert sent.raw_path == f"/api/v1/orders/{expected_encoded}"
    assert sent.query == ""

    # No approval created
    assert (
        client.get("/guard/v1/console/approvals", headers=auth_headers("sun")).json()["total"] == 0
    )

    # Exactly one audit event recorded
    after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["tool"] == "create_refund"
    assert ev["status_code"] == 404
    assert ev["order_id"] == order_id
    assert "approval_id" not in ev


@pytest.mark.parametrize("bad_order_id", [".", "..", "A1023\nx", "A1023\rx", "A1023\x00"])
def test_submit_rejects_dot_and_control_char_order_ids_without_calling_upstream(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    bad_order_id: str,
) -> None:
    before_reqs = len(fake_upstream.requests)
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body(bad_order_id, amount=10),
        headers=auth_headers("wang"),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"
    assert r.json()["details"]
    assert len(fake_upstream.requests) == before_reqs
    assert (
        client.get("/guard/v1/console/approvals", headers=auth_headers("sun")).json()["total"] == 0
    )

    after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["tool"] == "create_refund"
    assert ev["status_code"] == 422
    assert ev["error_code"] == "validation_error"
    assert ev["outcome"] == "invalid"
    assert "approval_id" not in ev


@pytest.mark.parametrize(
    ("submitted_order_id", "upstream_body"),
    [
        ("A1023", b"not-json"),
        ("A1023", b"[1, 2, 3]"),
        ("A1023", b'{"order_id": "A1023", "status": "ok"}'),
        ("A1023", b'{"order_id": "A1023", "region": "   "}'),
        ("A1023", b'{"order_id": "A1023", "region": 123}'),
        ("A1023?", b'{"order_id": "A1023", "region": "east"}'),
    ],
)
def test_submit_precheck_200_invalid_response_returns_502_and_audits(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    submitted_order_id: str,
    upstream_body: bytes,
) -> None:
    fake_upstream.handler = lambda _req: httpx.Response(200, content=upstream_body)
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body(submitted_order_id, amount=10),
        headers=auth_headers("wang"),
    )
    assert r.status_code == 502
    body = r.json()
    assert body["code"] == "upstream_invalid_response"
    assert body["message"]
    assert body["request_id"] == r.headers["X-Request-Id"]
    assert (
        client.get("/guard/v1/console/approvals", headers=auth_headers("sun")).json()["total"] == 0
    )

    after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["tool"] == "create_refund"
    assert ev["status_code"] == 502
    assert ev["error_code"] == "upstream_invalid_response"
    assert ev["outcome"] == "error"
    assert "approval_id" not in ev


def test_submit_precheck_invalid_url_returns_503_and_audits(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    def _bad_url(_req: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("invalid upstream url")

    fake_upstream.handler = _bad_url
    r = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=10),
        headers=auth_headers("wang"),
    )
    assert r.status_code == 503
    assert r.json()["code"] == "upstream_unavailable"
    assert (
        client.get("/guard/v1/console/approvals", headers=auth_headers("sun")).json()["total"] == 0
    )
    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs["total"] == 1
    assert logs["items"][0]["status_code"] == 503
    assert logs["items"][0]["error_code"] == "upstream_unavailable"


def test_submit_amount_upper_bound_rejects_huge_amounts(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    for huge in (1e20, 10_000_000.01):
        before_logs = client.get(
            "/guard/v1/console/audit-logs", headers=auth_headers("sun")
        ).json()["total"]
        r = client.post(
            "/api/v1/refunds",
            json=_valid_refund_body("A1023", amount=huge),
            headers=auth_headers("wang"),
        )
        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"
        assert len(fake_upstream.requests) == 0
        after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
        assert after_logs["total"] == before_logs + 1
        assert after_logs["items"][0]["status_code"] == 422

    # Boundary value 10_000_000.0 passes validation and creates approval
    r_max = client.post(
        "/api/v1/refunds",
        json=_valid_refund_body("A1023", amount=10_000_000.0),
        headers=auth_headers("wang"),
    )
    assert r_max.status_code == 202
    assert r_max.json()["amount"] == 10_000_000.0

    # Alias order_ids cannot bypass active-approval uniqueness to create a second pending approval
    for alias in ("A1023?", "A1023#x", "A1023/."):
        r_alias = client.post(
            "/api/v1/refunds",
            json=_valid_refund_body(alias, amount=10.0),
            headers=auth_headers("wang"),
        )
        assert r_alias.status_code == 404
    assert (
        client.get("/guard/v1/console/approvals", headers=auth_headers("sun")).json()["total"] == 1
    )


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{"order_id": "A1023", "amount": NaN, "reason_category": "other", "reason": "x"}',
        b'{"order_id": "A1023", "amount": Infinity, "reason_category": "other", "reason": "x"}',
        b'{"order_id": "A1023", "amount": -Infinity, "reason_category": "other", "reason": "x"}',
        b'{"order_id": "A1023", "amount": 1e400, "reason_category": "other", "reason": "x"}',
        b'{"order_id": "A1023", "amount": -1e400, "reason_category": "other", "reason": "x"}',
        (
            b'{"order_id": "A1023", "amount": 10, "reason_category": "other", '
            b'"reason": "x", "extra": [1e400]}'
        ),
        (
            b'{"order_id": "A1023", "amount": '
            + b"9" * 5000
            + b', "reason_category": "other", "reason": "x"}'
        ),
        b"\xff\xfe",
    ],
)
def test_submit_rejects_non_finite_and_overlong_int_with_422_and_safe_audit(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    raw_body: bytes,
) -> None:
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r = client.post(
        "/api/v1/refunds",
        content=raw_body,
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert len(fake_upstream.requests) == 0

    audit_resp = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
    assert audit_resp.status_code == 200
    after_logs = audit_resp.json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["tool"] == "create_refund"
    assert ev["status_code"] == 422
    assert ev["error_code"] == "validation_error"
    assert ev["outcome"] == "invalid"


def test_submit_parse_rejected_audit_extracts_order_id_and_sanitized_args(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. NaN amount -> 422, audit preserves order_id="A1023" and arguments["amount"] == "NaN"
    r_nan = client.post(
        "/api/v1/refunds",
        content=b'{"order_id": "A1023", "amount": NaN, "reason_category": "other", "reason": "x"}',
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_nan.status_code == 422
    assert r_nan.json()["message"] == "请求体含非法数值。"
    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
    assert logs.status_code == 200
    ev_nan = logs.json()["items"][0]
    assert ev_nan["order_id"] == "A1023"
    assert ev_nan["arguments"]["amount"] == "NaN"

    # 2. Deeply nested 600-layer list & dict -> 422, 1 audit each with placeholder + order_id
    nested_list_600 = ('{"order_id": "A1023", "x":' + "[" * 600 + "1" + "]" * 600 + "}").encode()
    nested_dict_600 = ('{"a":' * 600 + "1" + "}" * 600).encode()
    for raw_body, expect_oid in [(nested_list_600, "A1023"), (nested_dict_600, None)]:
        before = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
            "total"
        ]
        r = client.post(
            "/api/v1/refunds",
            content=raw_body,
            headers={**auth_headers("wang"), "Content-Type": "application/json"},
        )
        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"
        assert r.json()["message"] == "请求体嵌套过深。"
        after_resp = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
        assert after_resp.status_code == 200
        assert after_resp.json()["total"] == before + 1
        ev = after_resp.json()["items"][0]
        assert "[max_depth_exceeded]" in str(ev["arguments"])
        assert ev.get("order_id") == expect_oid

    # 3. Extreme nesting hitting C json.loads RecursionError -> 422, empty dict audit fallback
    extreme_nested = ('{"x":' + "[" * 12000 + "1" + "]" * 12000 + "}").encode()
    before_ext = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r_ext = client.post(
        "/api/v1/refunds",
        content=extreme_nested,
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_ext.status_code == 422
    assert r_ext.json()["message"] == "请求体嵌套过深。"
    after_ext = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
    assert after_ext.status_code == 200
    assert after_ext.json()["total"] == before_ext + 1
    assert after_ext.json()["items"][0]["arguments"] == {}

    # 4. Lone surrogate in reason / key / order_id -> 422, safe UTF-8 audit
    r_surr = client.post(
        "/api/v1/refunds",
        content=(
            b'{"order_id": "A1023", "amount": 10, "reason_category": "other", '
            b'"reason": "bad\\ud800reason"}'
        ),
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_surr.status_code == 422
    assert r_surr.json()["message"] == "请求体含非法字符。"
    ev_surr = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "items"
    ][0]
    assert ev_surr["order_id"] == "A1023"
    assert ev_surr["arguments"]["reason"] == "bad\ufffdreason"

    r_surr_key = client.post(
        "/api/v1/refunds",
        content=b'{"order_id": "A1023\\ud800", "\\ud800": "v"}',
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_surr_key.status_code == 422
    assert r_surr_key.json()["message"] == "请求体含非法字符。"
    ev_surr_key = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "items"
    ][0]
    assert "order_id" not in ev_surr_key
    assert ev_surr_key["arguments"]["\ufffd"] == "v"

    # 5. Non-dict JSON with NaN -> 422, empty dict audit fallback
    r_list_nan = client.post(
        "/api/v1/refunds",
        content=b"[NaN]",
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_list_nan.status_code == 422
    ev_list_nan = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "items"
    ][0]
    assert ev_list_nan["arguments"] == {}

    assert len(fake_upstream.requests) == 0


def test_submit_rejects_oversized_body_with_413_and_empty_audit(
    build_app: Callable[..., FastAPI],
    settings: Settings,
    auth_headers: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 256
    custom_cfg = settings.model_copy(update={"max_body_bytes": limit})
    up = FakeUpstream()
    app = build_app(custom_settings=custom_cfg, custom_upstream=up)

    with TestClient(app) as tc:
        # 1. Exact boundary payload (== max_body_bytes) passes and creates approval
        base_payload = _valid_refund_body("A1023", amount=10.0, reason="边界测试")
        encoded = json.dumps(base_payload, ensure_ascii=False).encode("utf-8")
        assert len(encoded) < limit
        pad = limit - len(encoded)
        exact_body = encoded + b" " * pad
        assert len(exact_body) == limit
        r_ok = tc.post(
            "/api/v1/refunds",
            content=exact_body,
            headers={**auth_headers("wang"), "Content-Type": "application/json"},
        )
        assert r_ok.status_code == 202

        # 2. Oversized bodies MUST NOT enter JSON parsing or upstream
        def _forbid_parse(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("Oversized body must not enter JSON parsing")

        monkeypatch.setattr(approvals, "_parse_json_dict", _forbid_parse)
        upstream_before = len(up.requests)

        cases: list[tuple[str, object, dict[str, str]]] = [
            (
                "over_limit_bytes",
                b"{" + b"a" * limit + b"}",
                {"Content-Type": "application/json"},
            ),
            (
                "over_limit_content_length_header",
                b"{}",
                {"Content-Type": "application/json", "Content-Length": str(limit + 10)},
            ),
            (
                "over_limit_chunked_stream",
                (b"x" * 100 for _ in range(4)),
                {"Content-Type": "application/json"},
            ),
            (
                "invalid_negative_content_length_fallback_stream",
                (b"y" * 100 for _ in range(4)),
                {"Content-Type": "application/json", "Content-Length": "-1"},
            ),
        ]

        for label, body_content, extra_headers in cases:
            before_logs = tc.get(
                "/guard/v1/console/audit-logs", headers=auth_headers("sun")
            ).json()["total"]
            r = tc.post(
                "/api/v1/refunds",
                content=body_content,  # type: ignore[arg-type]
                headers={**auth_headers("wang"), **extra_headers},
            )
            assert r.status_code == 413, label
            err = r.json()
            assert err["code"] == "payload_too_large"
            assert err["message"]
            assert err["request_id"] == r.headers["X-Request-Id"]

            after_logs = tc.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
            assert after_logs["total"] == before_logs + 1, label
            ev = after_logs["items"][0]
            assert ev["tool"] == "create_refund"
            assert ev["arguments"] == {}
            assert ev["status_code"] == 413
            assert ev["error_code"] == "payload_too_large"
            assert ev["outcome"] == "invalid"
            assert "order_id" not in ev

        assert len(up.requests) == upstream_before
        approvals_res = tc.get("/guard/v1/console/approvals", headers=auth_headers("sun")).json()
        assert approvals_res["total"] == 1


def test_max_body_bytes_settings_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().max_body_bytes == 65536

    for bad_value in [0, -1, -65536, float("inf"), float("-inf"), float("nan")]:
        with pytest.raises(ValidationError):
            Settings(max_body_bytes=bad_value)  # type: ignore[arg-type]

    for bad_env in ("0", "-10"):
        monkeypatch.setenv("GUARD_MAX_BODY_BYTES", bad_env)
        with pytest.raises(ValidationError):
            create_app(manifest=ToolManifest.from_file(MANIFEST_PATH))
