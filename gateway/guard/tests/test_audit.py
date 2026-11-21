from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import audit, json_utils
from app.config import Settings
from app.log import JsonFormatter
from app.models import ApprovalModel, AuditEventModel
from tests.conftest import FakeUpstream, MutableClock


def _replay_status(events: list[dict[str, Any]]) -> str:
    """Replay approval audit events in ascending id order to compute final state."""
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


def test_status_to_outcome_complete_mapping() -> None:
    assert audit.status_to_outcome(202, pending_approval=True) == "pending_approval"
    assert audit.status_to_outcome(None) == "error"
    assert audit.status_to_outcome(200) == "success"
    assert audit.status_to_outcome(201) == "success"
    assert audit.status_to_outcome(401) == "auth_failed"
    assert audit.status_to_outcome(403) == "access_denied"
    assert audit.status_to_outcome(404) == "not_found"
    assert audit.status_to_outcome(409) == "conflict"
    assert audit.status_to_outcome(413) == "invalid"
    assert audit.status_to_outcome(422) == "invalid"
    assert audit.status_to_outcome(400) == "invalid"
    assert audit.status_to_outcome(500) == "error"
    assert audit.status_to_outcome(503) == "error"


def test_audit_recording_points_chain_and_state_replay(
    client: TestClient,
    fake_upstream: FakeUpstream,
    clock: MutableClock,
    make_token: Callable[..., str],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    used_tokens = [
        make_token("wang"),
        make_token("zhao"),
        make_token("li"),
        make_token("zhou"),
        make_token("sun"),
    ]
    wang_h = {"Authorization": f"Bearer {used_tokens[0]}"}
    li_h = {"Authorization": f"Bearer {used_tokens[2]}"}
    zhou_h = {"Authorization": f"Bearer {used_tokens[3]}"}
    sun_h = {"Authorization": f"Bearer {used_tokens[4]}"}

    # #1 Proxy read success + access_denied
    r_ok = client.get("/api/v1/orders/A1023", headers={**wang_h, "X-Request-Id": "rid-read-ok"})
    assert r_ok.status_code == 200
    r_deny = client.get(
        "/api/v1/orders/B2087",
        headers={**wang_h, "X-Request-Id": "rid-read-deny"},
    )
    assert r_deny.status_code == 403

    # #2 Write intercepted -> AP000001
    r_sub = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 100,
            "reason_category": "quality_issue",
            "reason": "损坏退款",
        },
        headers={**wang_h, "X-Request-Id": "rid-submit-1"},
    )
    assert r_sub.status_code == 202
    ap1_id = r_sub.json()["approval_id"]

    # #4 Agent get_approval & list_approvals
    assert client.get(f"/guard/v1/approvals/{ap1_id}", headers=wang_h).status_code == 200
    assert client.get("/guard/v1/approvals", headers=wang_h).status_code == 200

    # #5 Console live order status
    assert (
        client.get(f"/guard/v1/console/approvals/{ap1_id}/order", headers=li_h).status_code == 200
    )

    # #6 Denied approve attempts (wang -> insufficient_role, zhou -> approval_access_denied)
    assert (
        client.post(f"/guard/v1/console/approvals/{ap1_id}/approve", headers=wang_h).status_code
        == 403
    )
    assert (
        client.post(f"/guard/v1/console/approvals/{ap1_id}/approve", headers=zhou_h).status_code
        == 403
    )

    # #6 + #8 First execute gets 401 -> executing -> pending (same request_id on both)
    fake_upstream.handler = lambda _req: httpx.Response(
        401,
        json={"code": "token_expired", "message": "过期", "request_id": "rid-401"},
    )
    r_401 = client.post(
        f"/guard/v1/console/approvals/{ap1_id}/approve",
        json={"comment": "首次401"},
        headers={**li_h, "X-Request-Id": "rid-approve-401"},
    )
    assert r_401.status_code == 401
    assert r_401.headers["X-Request-Id"] == "rid-approve-401"

    logs_401 = client.get(
        "/guard/v1/console/audit-logs?request_id=rid-approve-401",
        headers=sun_h,
    ).json()["items"]
    logs_401_asc = sorted(logs_401, key=lambda e: e["id"])
    assert len(logs_401_asc) == 2
    assert logs_401_asc[0]["event_type"] == "approval_approve"
    assert logs_401_asc[0]["status_code"] == 200
    assert logs_401_asc[0]["result"] == {"from": "pending", "to": "executing"}
    assert logs_401_asc[1]["event_type"] == "approval_execute"
    assert logs_401_asc[1]["status_code"] == 401
    assert logs_401_asc[1]["outcome"] == "auth_failed"
    assert logs_401_asc[1]["result"] == {"from": "executing", "to": "pending"}

    # Now approve & succeed with same request_id
    fake_upstream.handler = None
    r_app = client.post(
        f"/guard/v1/console/approvals/{ap1_id}/approve",
        json={"comment": "同意"},
        headers={**li_h, "X-Request-Id": "rid-approve-ok"},
    )
    assert r_app.status_code == 200
    assert r_app.json()["status"] == "executed"

    # Replay all events for AP000001 -> must equal 'executed'
    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap1_id}",
        headers=sun_h,
    ).json()["items"]
    assert _replay_status(chain) == "executed"

    # Verify the two records from rid-approve-ok share request_id and carry on_behalf_of_sub
    ok_pair = sorted(
        [e for e in chain if e["request_id"] == "rid-approve-ok"],
        key=lambda e: e["id"],
    )
    assert len(ok_pair) == 2
    assert ok_pair[0]["event_type"] == "approval_approve"
    assert ok_pair[1]["event_type"] == "approval_execute"
    assert ok_pair[1]["on_behalf_of_sub"] == "u1001"
    assert ok_pair[1]["result"] == {
        "from": "executing",
        "to": "executed",
        "refund_id": "R000105",
    }

    # #7 Reject chain replay -> 'rejected'
    ap2_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1024",
            "amount": 50,
            "reason_category": "no_reason",
            "reason": "不想要了",
        },
        headers=wang_h,
    ).json()["approval_id"]
    client.post(
        f"/guard/v1/console/approvals/{ap2_id}/reject",
        json={},
        headers=li_h,
    )
    client.post(
        f"/guard/v1/console/approvals/{ap2_id}/reject",
        json={"comment": "超期"},
        headers=li_h,
    )
    chain2 = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap2_id}",
        headers=sun_h,
    ).json()["items"]
    assert _replay_status(chain2) == "rejected"
    assert any(e["event_type"] == "approval_reject" and e["status_code"] == 422 for e in chain2)

    # #9 Expire chain replay -> 'expired'
    ap3_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1024",
            "amount": 50,
            "reason_category": "no_reason",
            "reason": "过期测试",
        },
        headers=wang_h,
    ).json()["approval_id"]
    clock.advance(hours=73)
    assert client.get(f"/guard/v1/approvals/{ap3_id}", headers=wang_h).json()["status"] == "expired"
    chain3 = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap3_id}",
        headers=sun_h,
    ).json()["items"]
    assert _replay_status(chain3) == "expired"
    exp_ev = next(e for e in chain3 if e["event_type"] == "approval_expire")
    assert exp_ev["actor_sub"] == "system"
    assert exp_ev["channel"] == "system"
    assert exp_ev["resource_region"] == "east"

    # Nonexistent approval denied by role check -> 403 and NO audit record created (3.9.2)
    before_count = client.get("/guard/v1/console/audit-logs", headers=sun_h).json()["total"]
    r_miss_role = client.post("/guard/v1/console/approvals/AP999999/approve", headers=wang_h)
    assert r_miss_role.status_code == 403
    assert r_miss_role.json()["code"] == "insufficient_role"
    after_count = client.get("/guard/v1/console/audit-logs", headers=sun_h).json()["total"]
    assert after_count == before_count

    # Verify NO token fragments appear anywhere in DB tables
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        all_audits = session.scalars(select(AuditEventModel)).all()
        all_approvals = session.scalars(select(ApprovalModel)).all()
        blob = "\n".join(
            [str(vars(row)) for row in all_audits] + [str(vars(row)) for row in all_approvals]
        )
        for tok in used_tokens:
            sig_part = tok.split(".")[2]
            assert tok not in blob
            assert sig_part not in blob


def test_audit_truncation_and_redaction(
    build_app: Callable[..., FastAPI],
    settings: Settings,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    custom_cfg = settings.model_copy(update={"audit_redact_fields": "reason,secret_field"})
    app = build_app(custom_settings=custom_cfg, custom_upstream=FakeUpstream())
    with TestClient(app) as tc:
        long_note = "A" * 650
        tc.get(
            f"/api/v1/orders?note={long_note}&secret_field=topsecret", headers=auth_headers("wang")
        )
        tc.post(
            "/api/v1/refunds",
            json={
                "order_id": "A1023",
                "amount": 10,
                "reason_category": "other",
                "reason": "敏感原因内容",
            },
            headers=auth_headers("wang"),
        )

        logs = tc.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()["items"]
        submit_log = next(e for e in logs if e["tool"] == "create_refund")
        assert submit_log["arguments"]["reason"] == "***"
        assert "敏感原因内容" not in str(submit_log)

        list_log = next(e for e in logs if e["tool"] == "list_orders")
        assert list_log["arguments"]["secret_field"] == "***"
        assert len(list_log["arguments"]["note"]) == 500

    # Unit check nested list/dict sanitization
    sanitized = audit.sanitize_value(
        {"items": [{"secret": "v", "long": "x" * 510, "num": 42}]},
        frozenset({"secret"}),
    )
    assert sanitized == {"items": [{"secret": "***", "long": "x" * 500, "num": 42}]}


def test_audit_query_filters_visibility_after_id_and_role_restriction(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # wang (east) reads A1023 (200) and B2087 (403)
    client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    r_deny = client.get("/api/v1/orders/B2087", headers=auth_headers("wang"))
    deny_rid = r_deny.json()["request_id"]
    # zhao (north) reads B2087 (200)
    client.get("/api/v1/orders/B2087", headers=auth_headers("zhao"))

    # Agent cannot query audit-logs
    r_agent = client.get("/guard/v1/console/audit-logs", headers=auth_headers("wang"))
    assert r_agent.status_code == 403
    assert r_agent.json()["code"] == "insufficient_role"

    # zhou (north,south) cannot see wang's records (even wang's denied attempt on B2087)
    r_zhou_wang = client.get(
        "/guard/v1/console/audit-logs?actor=u1001",
        headers=auth_headers("zhou"),
    ).json()
    assert r_zhou_wang["total"] == 0

    # li (east) sees both of wang's records (including B2087 access_denied without resource_region)
    r_li_wang = client.get(
        "/guard/v1/console/audit-logs?actor=wang",
        headers=auth_headers("li"),
    ).json()
    assert r_li_wang["total"] == 2
    b_ev = next(e for e in r_li_wang["items"] if e["order_id"] == "B2087")
    assert b_ev["outcome"] == "access_denied"
    assert "resource_region" not in b_ev

    # li cannot see zhao's record
    assert (
        client.get(
            "/guard/v1/console/audit-logs?actor=u1002",
            headers=auth_headers("li"),
        ).json()["total"]
        == 0
    )

    # sun sees all 3 records and can filter by all parameters including from/to/after_id
    all_sun = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert all_sun["total"] == 3
    first_id = min(e["id"] for e in all_sun["items"])

    after_res = client.get(
        f"/guard/v1/console/audit-logs?after_id={first_id}",
        headers=auth_headers("sun"),
    ).json()
    assert after_res["total"] == 2
    assert all(e["id"] > first_id for e in after_res["items"])

    filtered = client.get(
        f"/guard/v1/console/audit-logs?from=2026-11-20T00:00:00%2B08:00"
        f"&to=2026-11-20T23:59:59%2B08:00&event_type=tool_call&channel=agent"
        f"&tool=get_order&outcome=access_denied&order_id=B2087&request_id={deny_rid}",
        headers=auth_headers("sun"),
    ).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["request_id"] == deny_rid

    # Invalid ISO date -> 422
    assert (
        client.get(
            "/guard/v1/console/audit-logs?from=not-a-date",
            headers=auth_headers("sun"),
        ).status_code
        == 422
    )


def test_audit_write_failure_blocks_read_and_write_responses(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    make_token: Callable[..., str],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # First create one approval so console order endpoint can also be tested
    ap_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 10,
            "reason_category": "other",
            "reason": "前置申请",
        },
        headers=auth_headers("wang"),
    ).json()["approval_id"]

    # Attach an in-memory JSON log capture handler to the "guard" logger
    captured_records: list[tuple[int, dict[str, Any], str]] = []
    formatter = JsonFormatter(tz_name="Asia/Shanghai")

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rendered = formatter.format(record)
            captured_records.append((record.levelno, json.loads(rendered), rendered))

    capture = _CaptureHandler()
    guard_logger = logging.getLogger("guard")
    guard_logger.addHandler(capture)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    wang_tok = make_token("wang")
    li_tok = make_token("li")
    wang_h = {"Authorization": f"Bearer {wang_tok}"}
    li_h = {"Authorization": f"Bearer {li_tok}"}
    secret_reason = "SECRET_BODY_SHOULD_NOT_BE_LOGGED"

    try:
        monkeypatch.setattr(audit, "record_event", _boom)

        # 1. Read proxy fails with 500 and NEVER returns upstream order data
        r_read = client.get(
            "/api/v1/orders/A1023",
            headers={**wang_h, "X-Request-Id": "rid-err-proxy"},
        )
        assert r_read.status_code == 500
        assert r_read.json()["code"] == "internal_error"
        assert "total_amount" not in r_read.text

        # 2. Write approval fails with 500 and rolls back approval creation
        r_write = client.post(
            "/api/v1/refunds",
            json={
                "order_id": "A1024",
                "amount": 10,
                "reason_category": "other",
                "reason": secret_reason,
            },
            headers={**wang_h, "X-Request-Id": "rid-err-write"},
        )
        assert r_write.status_code == 500
        assert r_write.json()["code"] == "internal_error"

        # 3. Agent get_approval audit write failure -> 500 + error log
        r_agent = client.get(
            f"/guard/v1/approvals/{ap_id}",
            headers={**wang_h, "X-Request-Id": "rid-err-agent"},
        )
        assert r_agent.status_code == 500

        # 4. Console live order audit write failure -> 500 + error log
        r_console = client.get(
            f"/guard/v1/console/approvals/{ap_id}/order",
            headers={**li_h, "X-Request-Id": "rid-err-console"},
        )
        assert r_console.status_code == 500
    finally:
        guard_logger.removeHandler(capture)
        monkeypatch.undo()

    # Rollback confirmed: A1024 approval was NOT persisted (still only the 1 pre-created approval)
    assert client.get("/guard/v1/approvals", headers=auth_headers("sun")).json()["total"] == 1

    # Verify structured ERROR logs for all 4 failures, with NO token or request body leaked
    err_logs = [
        (lvl, payload, raw)
        for lvl, payload, raw in captured_records
        if lvl == logging.ERROR and payload.get("event") == "internal_error"
    ]
    by_rid = {payload["request_id"]: (payload, raw) for _, payload, raw in err_logs}
    assert set(by_rid.keys()) >= {
        "rid-err-proxy",
        "rid-err-write",
        "rid-err-agent",
        "rid-err-console",
    }
    assert by_rid["rid-err-proxy"][0]["error_type"] == "RuntimeError"
    assert by_rid["rid-err-proxy"][0]["operation"] == "proxy_audit_write"
    assert by_rid["rid-err-write"][0]["error_type"] == "RuntimeError"
    assert by_rid["rid-err-write"][0]["operation"] == "submit_refund_approval"
    assert by_rid["rid-err-agent"][0]["error_type"] == "RuntimeError"
    assert by_rid["rid-err-agent"][0]["operation"] == "agent_audit_write"
    assert by_rid["rid-err-console"][0]["error_type"] == "RuntimeError"
    assert by_rid["rid-err-console"][0]["operation"] == "console_order_audit_write"

    for _, raw in by_rid.values():
        assert wang_tok not in raw
        assert li_tok not in raw
        assert secret_reason not in raw
        assert "disk full" not in raw


def test_audit_retention_cleanup_on_write_after_24h(
    client: TestClient,
    clock: MutableClock,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Create an audit event at T=0
    assert client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).status_code == 200
    assert (
        client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()["total"] == 1
    )

    # Advance 1 hour (<24h): no cleanup triggered
    clock.advance(hours=1)
    assert client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).status_code == 200
    assert (
        client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()["total"] == 2
    )

    # Advance 181 days (>180 days retention and >24h since last cleanup):
    # next write triggers automatic retention cleanup deleting the 2 old rows
    clock.advance(days=181)
    assert client.get("/api/v1/orders/A1023", headers=auth_headers("wang")).status_code == 200
    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs["total"] == 1

    # Also verify maybe_cleanup_expired_events with retention_state=None is a no-op
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert (
            audit.maybe_cleanup_expired_events(
                session,
                now=clock(),
                settings=app.state.settings,
                retention_state=None,
            )
            == 0
        )


def test_audit_retry_denied_on_non_executing_records_409_execute_event(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    ap = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 50,
            "reason_category": "quality_issue",
            "reason": "重试被拒审计测试",
        },
        headers=auth_headers("wang"),
    ).json()
    ap_id = ap["approval_id"]

    # 1. Retry on pending approval -> 409 and exactly 1 new approval_execute audit (status_code=409)
    before_pending = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r_pending = client.post(
        f"/guard/v1/console/approvals/{ap_id}/retry",
        headers={**auth_headers("li"), "X-Request-Id": "rid-retry-pending-409"},
    )
    assert r_pending.status_code == 409
    assert r_pending.json()["code"] == "approval_not_pending"

    after_pending = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_pending["total"] == before_pending + 1
    ev1 = after_pending["items"][0]
    assert ev1["event_type"] == "approval_execute"
    assert ev1["status_code"] == 409
    assert ev1["error_code"] == "approval_not_pending"
    assert ev1["outcome"] == "conflict"
    assert ev1["on_behalf_of_sub"] == "u1001"
    assert ev1["request_id"] == "rid-retry-pending-409"
    assert "result" not in ev1

    # 2. Approve to executed, then retry on executed -> 409 and 1 new approval_execute audit
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            headers=auth_headers("li"),
        ).status_code
        == 200
    )
    before_exec = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r_exec = client.post(
        f"/guard/v1/console/approvals/{ap_id}/retry",
        headers={**auth_headers("li"), "X-Request-Id": "rid-retry-executed-409"},
    )
    assert r_exec.status_code == 409
    after_exec = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_exec["total"] == before_exec + 1
    ev2 = after_exec["items"][0]
    assert ev2["event_type"] == "approval_execute"
    assert ev2["status_code"] == 409
    assert ev2["error_code"] == "approval_not_pending"
    assert "result" not in ev2


def test_audit_all_nine_recording_points_and_key_fields(
    client: TestClient,
    clock: MutableClock,
    make_token: Callable[..., str],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    wang_tok = make_token("wang")
    wang_jti = jwt.decode(wang_tok, options={"verify_signature": False})["jti"]
    wang_h = {"Authorization": f"Bearer {wang_tok}"}

    li_tok = make_token("li")
    li_jti = jwt.decode(li_tok, options={"verify_signature": False})["jti"]
    li_h = {"Authorization": f"Bearer {li_tok}"}

    zhao_tok = make_token("zhao")
    zhao_jti = jwt.decode(zhao_tok, options={"verify_signature": False})["jti"]
    zhao_h = {"Authorization": f"Bearer {zhao_tok}"}

    sun_h = auth_headers("sun")

    def _fetch_all() -> dict[str, Any]:
        return client.get("/guard/v1/console/audit-logs?page_size=100", headers=sun_h).json()

    # Point #1: Proxy read with path param
    assert client.get("/api/v1/orders/A1023", headers=wang_h).status_code == 200
    logs = _fetch_all()
    assert logs["total"] == 1
    ev = logs["items"][0]
    assert ev["event_type"] == "tool_call"
    assert ev["channel"] == "agent"
    assert ev["tool"] == "get_order"
    assert ev["http_method"] == "GET"
    assert ev["http_path"] == "/api/v1/orders/A1023"
    assert ev["arguments"] == {"order_id": "A1023"}
    assert ev["order_id"] == "A1023"
    assert ev["resource_region"] == "east"
    assert ev["token_jti"] == wang_jti
    assert ev["actor_sub"] == "u1001"
    assert ev["actor_username"] == "wang"
    assert ev["actor_name"] == "王磊"
    assert ev["actor_role"] == "agent"
    assert ev["actor_regions"] == "east"

    # Point #3: Pre-creation write rejections (403, 404, 422)
    r403 = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "B2087",
            "amount": 10,
            "reason_category": "other",
            "reason": "越权提交",
        },
        headers=wang_h,
    )
    assert r403.status_code == 403
    logs = _fetch_all()
    assert logs["total"] == 2
    ev403 = logs["items"][0]
    assert ev403["tool"] == "create_refund"
    assert ev403["status_code"] == 403
    assert ev403["outcome"] == "access_denied"
    assert ev403["error_code"] == "order_access_denied"
    assert ev403["http_path"] == "/api/v1/refunds"
    assert ev403["token_jti"] == wang_jti
    assert "approval_id" not in ev403
    assert "resource_region" not in ev403

    r404 = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "X9999",
            "amount": 10,
            "reason_category": "other",
            "reason": "不存在订单",
        },
        headers=wang_h,
    )
    assert r404.status_code == 404
    logs = _fetch_all()
    assert logs["total"] == 3
    ev404 = logs["items"][0]
    assert ev404["status_code"] == 404
    assert ev404["outcome"] == "not_found"
    assert ev404["error_code"] == "order_not_found"
    assert "approval_id" not in ev404

    r422 = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": -5,
            "reason_category": "other",
            "reason": "负数金额",
        },
        headers=wang_h,
    )
    assert r422.status_code == 422
    logs = _fetch_all()
    assert logs["total"] == 4
    ev422 = logs["items"][0]
    assert ev422["status_code"] == 422
    assert ev422["outcome"] == "invalid"
    assert ev422["error_code"] == "validation_error"
    assert "approval_id" not in ev422

    # Point #2: Submit creates approval (202) + idempotent replay (200)
    r202 = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 50,
            "reason_category": "quality_issue",
            "reason": "正常提交",
            "idempotency_key": "idem-audit-0001",
        },
        headers=wang_h,
    )
    assert r202.status_code == 202
    ap_id = r202.json()["approval_id"]
    logs = _fetch_all()
    assert logs["total"] == 5
    ev202 = logs["items"][0]
    assert ev202["status_code"] == 202
    assert ev202["outcome"] == "pending_approval"
    assert ev202["approval_id"] == ap_id
    assert ev202["resource_region"] == "east"
    assert ev202["token_jti"] == wang_jti

    r_replay = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 50,
            "reason_category": "quality_issue",
            "reason": "正常提交",
            "idempotency_key": "idem-audit-0001",
        },
        headers=wang_h,
    )
    assert r_replay.status_code == 200
    logs = _fetch_all()
    assert logs["total"] == 6
    ev_replay = logs["items"][0]
    assert ev_replay["status_code"] == 200
    assert ev_replay["outcome"] == "success"
    assert ev_replay["approval_id"] == ap_id
    assert ev_replay["resource_region"] == "east"

    # Point #3 (continued): 409 idempotency_conflict & 409 approval_pending_exists
    assert (
        client.post(
            "/api/v1/refunds",
            json={
                "order_id": "A1023",
                "amount": 60,
                "reason_category": "quality_issue",
                "reason": "幂等冲突",
                "idempotency_key": "idem-audit-0001",
            },
            headers=wang_h,
        ).status_code
        == 409
    )
    logs = _fetch_all()
    assert logs["total"] == 7
    assert logs["items"][0]["status_code"] == 409
    assert logs["items"][0]["error_code"] == "idempotency_conflict"
    assert logs["items"][0]["outcome"] == "conflict"
    assert "approval_id" not in logs["items"][0]

    assert (
        client.post(
            "/api/v1/refunds",
            json={
                "order_id": "A1023",
                "amount": 30,
                "reason_category": "quality_issue",
                "reason": "重复提交",
            },
            headers=wang_h,
        ).status_code
        == 409
    )
    logs = _fetch_all()
    assert logs["total"] == 8
    assert logs["items"][0]["status_code"] == 409
    assert logs["items"][0]["error_code"] == "approval_pending_exists"
    assert "approval_id" not in logs["items"][0]

    # Point #4: get_approval (200, 403, 404) and list_approvals (200, 422)
    assert client.get(f"/guard/v1/approvals/{ap_id}", headers=wang_h).status_code == 200
    logs = _fetch_all()
    assert logs["total"] == 9
    ev_ga = logs["items"][0]
    assert ev_ga["tool"] == "get_approval"
    assert ev_ga["http_path"] == f"/guard/v1/approvals/{ap_id}"
    assert ev_ga["arguments"] == {"approval_id": ap_id}
    assert ev_ga["approval_id"] == ap_id
    assert ev_ga["order_id"] == "A1023"
    assert ev_ga["resource_region"] == "east"
    assert ev_ga["token_jti"] == wang_jti

    assert client.get(f"/guard/v1/approvals/{ap_id}", headers=zhao_h).status_code == 403
    logs = _fetch_all()
    assert logs["total"] == 10
    ev_ga403 = logs["items"][0]
    assert ev_ga403["tool"] == "get_approval"
    assert ev_ga403["status_code"] == 403
    assert ev_ga403["error_code"] == "approval_access_denied"
    assert ev_ga403["actor_sub"] == "u1002"
    assert ev_ga403["token_jti"] == zhao_jti
    assert "resource_region" not in ev_ga403

    assert client.get("/guard/v1/approvals/AP999999", headers=wang_h).status_code == 404
    logs = _fetch_all()
    assert logs["total"] == 11
    ev_ga404 = logs["items"][0]
    assert ev_ga404["tool"] == "get_approval"
    assert ev_ga404["status_code"] == 404
    assert ev_ga404["arguments"] == {"approval_id": "AP999999"}

    assert client.get("/guard/v1/approvals?status=pending", headers=wang_h).status_code == 200
    logs = _fetch_all()
    assert logs["total"] == 12
    ev_la = logs["items"][0]
    assert ev_la["tool"] == "list_approvals"
    assert ev_la["http_path"] == "/guard/v1/approvals"
    assert ev_la["result"] == {"total": 1}
    assert ev_la["token_jti"] == wang_jti
    assert "approval_id" not in ev_la

    assert client.get("/guard/v1/approvals?page_size=999", headers=wang_h).status_code == 422
    logs = _fetch_all()
    assert logs["total"] == 13
    assert logs["items"][0]["tool"] == "list_approvals"
    assert logs["items"][0]["status_code"] == 422

    # Point #5: Console live order
    assert client.get(f"/guard/v1/console/approvals/{ap_id}/order", headers=li_h).status_code == 200
    logs = _fetch_all()
    assert logs["total"] == 14
    ev_ord = logs["items"][0]
    assert ev_ord["channel"] == "console"
    assert ev_ord["tool"] == "get_order"
    assert ev_ord["http_path"] == "/api/v1/orders/A1023"
    assert ev_ord["arguments"] == {"order_id": "A1023"}
    assert ev_ord["approval_id"] == ap_id
    assert ev_ord["token_jti"] == li_jti
    assert ev_ord["actor_sub"] == "u2001"
    assert ev_ord["actor_name"] == "李娜"

    # Points #6 & #8: Approve (200) + execute (201)
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "批准"},
            headers=li_h,
        ).status_code
        == 200
    )
    logs = _fetch_all()
    assert logs["total"] == 16
    ev_exec = logs["items"][0]
    ev_appr = logs["items"][1]
    assert ev_appr["event_type"] == "approval_approve"
    assert ev_appr["http_path"] == f"/guard/v1/console/approvals/{ap_id}/approve"
    assert ev_appr["token_jti"] == li_jti
    assert ev_exec["event_type"] == "approval_execute"
    assert ev_exec["http_path"] == "/api/v1/refunds"
    assert ev_exec["token_jti"] == li_jti

    # Point #7: Reject
    ap2_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1024",
            "amount": 20,
            "reason_category": "no_reason",
            "reason": "拒绝审计",
        },
        headers=wang_h,
    ).json()["approval_id"]
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap2_id}/reject",
            json={"comment": "不同意"},
            headers=li_h,
        ).status_code
        == 200
    )
    logs = _fetch_all()
    assert logs["total"] == 18
    ev_rej = logs["items"][0]
    assert ev_rej["event_type"] == "approval_reject"
    assert ev_rej["http_path"] == f"/guard/v1/console/approvals/{ap2_id}/reject"
    assert ev_rej["token_jti"] == li_jti

    # Point #9: Expire
    ap3_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1024",
            "amount": 20,
            "reason_category": "no_reason",
            "reason": "过期审计",
        },
        headers=wang_h,
    ).json()["approval_id"]
    clock.advance(hours=73)
    assert client.get(f"/guard/v1/console/approvals/{ap3_id}", headers=li_h).status_code == 200
    logs = _fetch_all()
    assert logs["total"] == 20
    ev_exp = logs["items"][0]
    assert ev_exp["event_type"] == "approval_expire"
    assert ev_exp["channel"] == "system"
    assert ev_exp["actor_sub"] == "system"


def test_audit_replay_covers_execution_failed_and_executing_endings(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    sun_h = auth_headers("sun")

    # 1. execution_failed (unpaid order A1029 -> 409 order_not_refundable)
    ap_fail = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1029",
            "amount": 10,
            "reason_category": "other",
            "reason": "未支付订单测试",
        },
        headers=auth_headers("wang"),
    ).json()["approval_id"]
    r_fail = client.post(
        f"/guard/v1/console/approvals/{ap_fail}/approve",
        headers=auth_headers("li"),
    )
    assert r_fail.status_code == 200
    assert r_fail.json()["status"] == "execution_failed"
    chain_fail = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_fail}",
        headers=sun_h,
    ).json()["items"]
    assert _replay_status(chain_fail) == "execution_failed"

    # 2. Stays in executing (upstream 500 + retry timeout)
    ap_exec = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 10,
            "reason_category": "other",
            "reason": "执行中重放测试",
        },
        headers=auth_headers("wang"),
    ).json()["approval_id"]
    fake_upstream.handler = lambda _req: httpx.Response(
        500, json={"code": "internal_error", "message": "后端错误"}
    )
    r_500 = client.post(
        f"/guard/v1/console/approvals/{ap_exec}/approve",
        headers=auth_headers("li"),
    )
    assert r_500.status_code == 200
    assert r_500.json()["status"] == "executing"

    def _timeout(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    fake_upstream.handler = _timeout
    r_to = client.post(
        f"/guard/v1/console/approvals/{ap_exec}/retry",
        headers=auth_headers("li"),
    )
    assert r_to.status_code == 200
    assert r_to.json()["status"] == "executing"

    chain_exec = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_exec}",
        headers=sun_h,
    ).json()["items"]
    assert _replay_status(chain_exec) == "executing"


def test_audit_query_pagination_defaults_max_empty_page_and_desc_order(
    client: TestClient,
    clock: MutableClock,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        for idx in range(105):
            audit.record_event(
                session,
                settings=app.state.settings,
                now=clock(),
                request_id=f"rid-page-{idx}",
                event_type="tool_call",
                channel="agent",
                actor_sub="u1001",
                actor_username="wang",
                actor_name="王磊",
                actor_role="agent",
                actor_regions="east",
                token_jti="jti-page",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/A1023",
                arguments={"order_id": "A1023"},
                order_id="A1023",
                resource_region="east",
                outcome="success",
                status_code=200,
                duration_ms=1,
            )
        session.commit()

    sun_h = auth_headers("sun")

    # Default page_size == 50, descending by id
    p1_default = client.get("/guard/v1/console/audit-logs", headers=sun_h).json()
    assert p1_default["total"] == 105
    assert p1_default["page"] == 1
    assert p1_default["page_size"] == 50
    assert len(p1_default["items"]) == 50
    ids_default = [item["id"] for item in p1_default["items"]]
    assert ids_default == sorted(ids_default, reverse=True)

    # Max page_size == 100
    p1_max = client.get(
        "/guard/v1/console/audit-logs?page=1&page_size=100",
        headers=sun_h,
    ).json()
    assert p1_max["total"] == 105
    assert p1_max["page_size"] == 100
    assert len(p1_max["items"]) == 100

    p2_max = client.get(
        "/guard/v1/console/audit-logs?page=2&page_size=100",
        headers=sun_h,
    ).json()
    assert p2_max["total"] == 105
    assert len(p2_max["items"]) == 5
    all_ids = [item["id"] for item in p1_max["items"]] + [item["id"] for item in p2_max["items"]]
    assert len(all_ids) == 105
    assert all_ids == sorted(set(all_ids), reverse=True)

    # Exceeding max page_size (101) -> 422
    r_over = client.get(
        "/guard/v1/console/audit-logs?page_size=101",
        headers=sun_h,
    )
    assert r_over.status_code == 422
    assert r_over.json()["code"] == "validation_error"

    # Out-of-range page -> empty items, total unchanged
    p_empty = client.get(
        "/guard/v1/console/audit-logs?page=9&page_size=50",
        headers=sun_h,
    ).json()
    assert p_empty["total"] == 105
    assert p_empty["items"] == []


def test_audit_replay_matches_final_status_under_interleaved_executions(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    wang_h = auth_headers("wang")
    li_h = auth_headers("li")
    sun_h = auth_headers("sun")

    ap_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 50,
            "reason_category": "quality_issue",
            "reason": "并发迟到审计重放",
        },
        headers=wang_h,
    ).json()["approval_id"]

    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0
    call_lock = threading.Lock()

    def _handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        with call_lock:
            calls += 1
            seq = calls
        if seq == 1:
            first_started.set()
            assert release_first.wait(timeout=5)
            return httpx.Response(
                401,
                json={"code": "token_expired", "message": "expired", "request_id": "r-late-401"},
            )
        return httpx.Response(
            201,
            json={
                "refund_id": "R000105",
                "order_id": "A1023",
                "region": "east",
                "amount": 50.0,
                "created_by": "u2003",
            },
        )

    fake_upstream.handler = _handler
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(
            client.post,
            f"/guard/v1/console/approvals/{ap_id}/approve",
            json={"comment": "首次"},
            headers=li_h,
        )
        assert first_started.wait(timeout=5)
        r_retry = client.post(f"/guard/v1/console/approvals/{ap_id}/retry", headers=sun_h)
        assert r_retry.status_code == 200
        assert r_retry.json()["status"] == "executed"
        release_first.set()
        r_first = f1.result(timeout=5)
        assert r_first.status_code == 200
        assert r_first.json()["status"] == "executed"

    detail = client.get(f"/guard/v1/console/approvals/{ap_id}", headers=sun_h).json()
    assert detail["status"] == "executed"
    assert detail["refund_id"] == "R000105"

    chain = client.get(
        f"/guard/v1/console/audit-logs?approval_id={ap_id}",
        headers=sun_h,
    ).json()["items"]
    assert _replay_status(chain) == detail["status"]
    stale_evs = [e for e in chain if (e.get("result") or {}).get("stale") is True]
    assert len(stale_evs) == 1
    assert stale_evs[0]["status_code"] == 401
    assert stale_evs[0]["result"]["from"] == "executed"
    assert stale_evs[0]["result"]["to"] == "executed"


def test_audit_sanitize_non_finite_floats_and_corrupted_db_rows_fallback(
    client: TestClient,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. sanitize_value recursively converts NaN / Infinity / -Infinity to string placeholders
    sanitized = audit.sanitize_value(
        {
            "nan": float("nan"),
            "pos_inf": float("inf"),
            "neg_inf": float("-inf"),
            "nested": [float("nan"), {"v": float("inf")}, 1.5],
        },
        frozenset(),
    )
    assert sanitized == {
        "nan": "NaN",
        "pos_inf": "Infinity",
        "neg_inf": "-Infinity",
        "nested": ["NaN", {"v": "Infinity"}, 1.5],
    }

    # 2. record_event sanitizes both arguments and result before allow_nan=False json.dumps
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        row = audit.record_event(
            session,
            settings=app.state.settings,
            now=clock(),
            request_id="rid-sanitized-nonfinite",
            event_type="tool_call",
            channel="agent",
            actor_sub="u1001",
            actor_username="wang",
            actor_name="王磊",
            actor_role="agent",
            actor_regions="east",
            token_jti=None,
            tool="create_refund",
            http_method="POST",
            http_path="/api/v1/refunds",
            arguments={"amount": float("nan"), "extra": [float("inf"), float("-inf")]},
            order_id="A1023",
            resource_region="east",
            outcome="invalid",
            status_code=422,
            error_code="validation_error",
            duration_ms=1,
            result={"score": float("inf")},
        )
        session.commit()
        assert json.loads(row.arguments) == {
            "amount": "NaN",
            "extra": ["Infinity", "-Infinity"],
        }
        assert json.loads(row.result or "") == {"score": "Infinity"}

    # 3. Create one normal approval, then manually inject corrupted DB rows
    ap_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 50,
            "reason_category": "quality_issue",
            "reason": "正常审批单",
        },
        headers=auth_headers("wang"),
    ).json()["approval_id"]

    corrupted_specs = [
        ("rid-bad-nan", '{"amount": NaN}', None, None),
        ("rid-bad-overflow", '{"amount": 1e400}', None, None),
        ("rid-bad-syntax", "{broken-json", None, None),
        ("rid-bad-list", "[1, 2, 3]", None, None),
        ("rid-bad-huge-int", '{"n": ' + "9" * 5000 + "}", None, None),
        ("rid-bad-deep-600", '{"x":' + "[" * 600 + "1" + "]" * 600 + "}", None, None),
        ("rid-bad-deep-12000", '{"x":' + "[" * 12000 + "1" + "]" * 12000 + "}", None, None),
        ("rid-bad-surrogate", '{"reason": "\\ud800"}', None, None),
        ("rid-bad-result", '{"ok": true}', '{"refund_id": Infinity}', ap_id),
    ]
    with app.state.session_factory() as session:
        for rid, raw_args, raw_res, linked_ap in corrupted_specs:
            session.add(
                AuditEventModel(
                    ts=clock().replace(tzinfo=None),
                    request_id=rid,
                    event_type="approval_execute" if linked_ap else "tool_call",
                    channel="console" if linked_ap else "agent",
                    actor_sub="u1001",
                    actor_username="wang",
                    actor_name="王磊",
                    actor_role="agent",
                    actor_regions="east",
                    token_jti=None,
                    tool="create_refund",
                    http_method="POST",
                    http_path="/api/v1/refunds",
                    arguments=raw_args,
                    order_id="A1023",
                    resource_region="east",
                    outcome="invalid",
                    status_code=422,
                    error_code="validation_error",
                    duration_ms=0,
                    approval_id=linked_ap,
                    result=raw_res,
                )
            )
        session.commit()

    captured_records: list[tuple[int, dict[str, Any]]] = []
    formatter = JsonFormatter(tz_name="Asia/Shanghai")

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append((record.levelno, json.loads(formatter.format(record))))

    capture = _CaptureHandler()
    guard_logger = logging.getLogger("guard")
    guard_logger.addHandler(capture)

    try:
        # Audit log list MUST return 200 despite corrupted DB rows
        resp_logs = client.get(
            "/guard/v1/console/audit-logs?page_size=50",
            headers=auth_headers("sun"),
        )
        assert resp_logs.status_code == 200
        by_rid = {item["request_id"]: item for item in resp_logs.json()["items"]}
        assert by_rid["rid-sanitized-nonfinite"]["arguments"] == {
            "amount": "NaN",
            "extra": ["Infinity", "-Infinity"],
        }
        for rid in (
            "rid-bad-nan",
            "rid-bad-overflow",
            "rid-bad-syntax",
            "rid-bad-list",
            "rid-bad-huge-int",
            "rid-bad-deep-600",
            "rid-bad-deep-12000",
            "rid-bad-surrogate",
        ):
            assert by_rid[rid]["arguments"]["_corrupted"] is True
        assert by_rid["rid-bad-result"]["arguments"] == {"ok": True}
        assert by_rid["rid-bad-result"]["result"]["_corrupted"] is True

        # Approval detail linked to a corrupted audit row MUST also return 200
        resp_detail = client.get(
            f"/guard/v1/console/approvals/{ap_id}",
            headers=auth_headers("li"),
        )
        assert resp_detail.status_code == 200
        bad_ev = next(
            e for e in resp_detail.json()["events"] if e["request_id"] == "rid-bad-result"
        )
        assert bad_ev["result"]["_corrupted"] is True

        # 4. Row-level serialization guard catches ValueError, UnicodeEncodeError, RecursionError
        orig_query_events = audit.query_events

        def _inject_bad_events(*args: Any, **kwargs: Any) -> Any:
            res = orig_query_events(*args, **kwargs)
            res.items[0].arguments = {"amount": float("nan")}
            res.items[0].result = {"val": float("inf")}
            res.items[1].arguments = {"reason": "\ud800"}
            res.items[1].result = None
            deep_dict: dict[str, Any] = {}
            cur = deep_dict
            for _ in range(12000):
                nxt: dict[str, Any] = {}
                cur["a"] = nxt
                cur = nxt
            res.items[2].arguments = deep_dict
            res.items[2].result = None
            return res

        monkeypatch.setattr(audit, "query_events", _inject_bad_events)
        resp_guarded = client.get(
            "/guard/v1/console/audit-logs?page_size=5",
            headers=auth_headers("sun"),
        )
        assert resp_guarded.status_code == 200
        first_item = resp_guarded.json()["items"][0]
        assert first_item["arguments"]["_corrupted"] is True
        assert first_item["result"]["_corrupted"] is True
        second_item = resp_guarded.json()["items"][1]
        assert second_item["arguments"]["_corrupted"] is True
        assert "result" not in second_item
        third_item = resp_guarded.json()["items"][2]
        assert third_item["arguments"]["_corrupted"] is True
    finally:
        guard_logger.removeHandler(capture)

    corrupted_logs = [
        payload
        for lvl, payload in captured_records
        if lvl == logging.ERROR and payload.get("event") == "audit_row_corrupted"
    ]
    assert len(corrupted_logs) >= len(corrupted_specs)
    serialize_err_logs = [
        payload
        for lvl, payload in captured_records
        if lvl == logging.ERROR and payload.get("event") == "audit_row_serialize_failed"
    ]
    assert len(serialize_err_logs) == 3
    assert {e["error_type"] for e in serialize_err_logs} == {
        "ValueError",
        "UnicodeEncodeError",
        "RecursionError",
    }


def test_json_utils_and_conflict_lookup_unit(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # sanitize_json_value on scalar, list root, dict/list depth cap, surrogate key & value
    assert json_utils.sanitize_json_value(float("nan")) == "NaN"
    assert json_utils.sanitize_json_value("a\ud800b", max_len=10) == "a\ufffdb"
    assert json_utils.sanitize_json_value(["ok", float("-inf"), {"k\ud800": "v"}]) == [
        "ok",
        "-Infinity",
        {"k\ufffd": "v"},
    ]
    assert json_utils.sanitize_json_value(
        {"a": {"b": {"c": 1}}, "x": [[1]]},
        max_depth=2,
    ) == {"a": {"b": "[max_depth_exceeded]"}, "x": ["[max_depth_exceeded]"]}

    # validate_strict_json_value allows depth == max_depth and rejects depth == max_depth + 1
    json_utils.validate_strict_json_value({"a": {"b": 1}}, max_depth=2)
    with pytest.raises(json_utils.MaxDepthExceededError):
        json_utils.validate_strict_json_value({"a": {"b": {"c": 1}}}, max_depth=2)
    with pytest.raises(json_utils.MaxDepthExceededError):
        json_utils.validate_strict_json_value([[[1]]], max_depth=2)

    # extract_valid_order_id covers all rejection paths and valid strings
    for bad_args in [
        {},
        {"order_id": 123},
        {"order_id": "   "},
        {"order_id": "A" * 33},
        {"order_id": "."},
        {"order_id": ".."},
        {"order_id": "A1023\n"},
        {"order_id": "A1023\ufffd"},
        {"order_id": "A1023\ud800"},
    ]:
        assert json_utils.extract_valid_order_id(bad_args) is None
    assert json_utils.extract_valid_order_id({"order_id": "A1023"}) == "A1023"

    # find_late_execution_conflict_approval_ids ignores non-stale 2xx (executed) rows
    ap_id = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 10,
            "reason_category": "other",
            "reason": "正常执行",
        },
        headers=auth_headers("wang"),
    ).json()["approval_id"]
    assert (
        client.post(
            f"/guard/v1/console/approvals/{ap_id}/approve",
            headers=auth_headers("li"),
        ).status_code
        == 200
    )
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert audit.find_late_execution_conflict_approval_ids(session, approval_ids=[]) == set()
        assert (
            audit.find_late_execution_conflict_approval_ids(session, approval_ids=[ap_id]) == set()
        )


def test_audit_key_length_and_dict_key_count_bounds(
    client: TestClient,
    settings: Settings,
    clock: MutableClock,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # 1. Unit checks: key length truncation (including short cap <= suffix & redaction before trunc)
    long_secret_key = "secret_" + "s" * 200
    long_plain_key = "field_" + "k" * 200
    sanitized_keys = audit.sanitize_value(
        {long_secret_key: "top-secret-val", long_plain_key: "ok"},
        frozenset({long_secret_key}),
    )
    expected_secret_key = long_secret_key[:125] + "..."
    expected_plain_key = long_plain_key[:125] + "..."
    assert sanitized_keys == {
        expected_secret_key: "***",
        expected_plain_key: "ok",
    }
    assert all(len(k) <= json_utils.MAX_AUDIT_KEY_LEN for k in sanitized_keys)

    # Short max_key_len <= len("...") truncates cleanly without exceeding limit
    assert json_utils.sanitize_json_value({"abcdef": 1}, max_key_len=2) == {"ab": 1}

    # 2. Unit checks: exact boundary (64 keys -> no placeholder) & overflow (top-level + nested)
    exact_64 = {f"k{i:03d}": i for i in range(json_utils.MAX_AUDIT_DICT_KEYS)}
    sanitized_64 = audit.sanitize_value(exact_64, frozenset())
    assert len(sanitized_64) == json_utils.MAX_AUDIT_DICT_KEYS
    assert json_utils.TRUNCATED_KEYS_FIELD not in sanitized_64

    nested_many = {f"n{i:03d}": i for i in range(80)}
    wide_dict: dict[str, Any] = {"nested": nested_many}
    for i in range(119):
        wide_dict[f"k{i:03d}_" + "x" * 150] = i
    sanitized_wide = audit.sanitize_value(wide_dict, frozenset())
    assert sanitized_wide[json_utils.TRUNCATED_KEYS_FIELD] == 120 - json_utils.MAX_AUDIT_DICT_KEYS
    assert len(sanitized_wide) == json_utils.MAX_AUDIT_DICT_KEYS + 1
    assert all(len(k) <= json_utils.MAX_AUDIT_KEY_LEN for k in sanitized_wide)
    nested_out = sanitized_wide["nested"]
    assert nested_out[json_utils.TRUNCATED_KEYS_FIELD] == 80 - json_utils.MAX_AUDIT_DICT_KEYS
    assert len(nested_out) == json_utils.MAX_AUDIT_DICT_KEYS + 1

    # 3. End-to-end HTTP submit (within body size limit, 100 keys + overlong key -> 422)
    payload: dict[str, Any] = {"long_" + "a" * 200: "v"}
    for i in range(99):
        payload[f"extra_{i:03d}"] = i
    r_422 = client.post(
        "/api/v1/refunds",
        json=payload,
        headers={**auth_headers("wang"), "X-Request-Id": "rid-many-keys-http"},
    )
    assert r_422.status_code == 422

    # 4. Direct record_event with 500 keys & 400-char key names stays strictly bounded in DB + API
    app = client.app
    assert isinstance(app, FastAPI)
    huge_args = {f"key_{i:04d}_" + "z" * 400: "v" * 600 for i in range(500)}
    with app.state.session_factory() as session:
        audit.record_event(
            session,
            settings=settings,
            now=clock(),
            request_id="rid-many-keys-direct",
            event_type="tool_call",
            channel="agent",
            actor_sub="u1001",
            actor_username="wang",
            actor_name="王磊",
            actor_role="agent",
            actor_regions="east",
            token_jti=None,
            tool="create_refund",
            http_method="POST",
            http_path="/api/v1/refunds",
            arguments=huge_args,
            order_id=None,
            resource_region="east",
            outcome="invalid",
            status_code=422,
            error_code="validation_error",
            duration_ms=0,
        )
        session.commit()

    resp_logs = client.get("/guard/v1/console/audit-logs?page_size=20", headers=auth_headers("sun"))
    assert resp_logs.status_code == 200
    by_rid = {item["request_id"]: item for item in resp_logs.json()["items"]}

    http_args = by_rid["rid-many-keys-http"]["arguments"]
    assert http_args[json_utils.TRUNCATED_KEYS_FIELD] == 100 - json_utils.MAX_AUDIT_DICT_KEYS
    assert len(http_args) == json_utils.MAX_AUDIT_DICT_KEYS + 1
    assert all(len(k) <= json_utils.MAX_AUDIT_KEY_LEN for k in http_args)
    assert any(k.startswith("long_") and k.endswith("...") for k in http_args)

    direct_args = by_rid["rid-many-keys-direct"]["arguments"]
    assert direct_args[json_utils.TRUNCATED_KEYS_FIELD] == 500 - json_utils.MAX_AUDIT_DICT_KEYS
    assert len(direct_args) == json_utils.MAX_AUDIT_DICT_KEYS + 1
    assert all(len(k) <= json_utils.MAX_AUDIT_KEY_LEN for k in direct_args)
    assert all(
        (isinstance(v, str) and len(v) == audit.MAX_AUDIT_STR_LEN)
        for k, v in direct_args.items()
        if k != json_utils.TRUNCATED_KEYS_FIELD
    )
