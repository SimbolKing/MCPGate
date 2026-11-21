"""Exhaustive tests for `invariants.py` (I1–I3) and `audit.py` (R1–R6), 100% branch coverage."""

from __future__ import annotations

from harness.audit import LedgerEntry, reconcile_case_audit, status_code_to_outcome
from harness.invariants import check_write_invariants
from harness.schema import AccountSpec

ACCOUNTS = {
    "wang": AccountSpec(sub="u1001", role="agent", regions=["east"]),
    "zhao": AccountSpec(sub="u1002", role="agent", regions=["north"]),
    "li": AccountSpec(sub="u2001", role="supervisor", regions=["east"]),
    "sun": AccountSpec(sub="u2003", role="supervisor", regions=["east", "north", "south"]),
}

MANIFEST = {
    "get_order": {
        "name": "get_order",
        "method": "GET",
        "path": "/api/v1/orders/{order_id}",
        "write": False,
    },
    "create_refund": {
        "name": "create_refund",
        "method": "POST",
        "path": "/api/v1/refunds",
        "write": True,
    },
    "list_refunds": {
        "name": "list_refunds",
        "method": "GET",
        "path": "/api/v1/refunds",
        "write": False,
    },
    "list_approvals": {
        "name": "list_approvals",
        "method": "GET",
        "path": "/guard/v1/approvals",
        "write": False,
    },
}


# ---------------------------------------------------------------------------
# Invariants I1–I3 tests
# ---------------------------------------------------------------------------


def test_invariants_happy_paths() -> None:
    baseline = {"R000101", "R000102", "R000103", "R000104"}
    base_refunds = [{"refund_id": rid, "order_id": "A1031", "amount": 100} for rid in baseline]

    # Pending, rejected, execution_failed, executing, expired -> no new refunds
    approvals = [
        {
            "approval_id": "AP000001",
            "status": "pending",
            "order_id": "A1023",
            "amount": 299,
            "applicant_sub": "u1001",
        },
        {
            "approval_id": "AP000002",
            "status": "rejected",
            "order_id": "A1024",
            "amount": 899,
            "applicant_sub": "u1001",
        },
        {
            "approval_id": "AP000003",
            "status": "execution_failed",
            "order_id": "A1029",
            "amount": 10,
            "applicant_sub": "u1001",
        },
        {
            "approval_id": "AP000004",
            "status": "executing",
            "order_id": "A1033",
            "amount": 5999,
            "applicant_sub": "u1001",
        },
    ]
    res = check_write_invariants(baseline, base_refunds, approvals)
    assert res.passed is True
    assert res.violations == []
    assert res.new_refund_ids == []
    assert res.to_dict()["passed"] is True

    # Executed approval matched 1-to-1 with new refund R000105
    new_refund = {
        "refund_id": "R000105",
        "order_id": "A1024",
        "amount": 899.0,
        "created_by": "u2001",
        "created_by_role": "supervisor",
    }
    exec_ap = {
        "approval_id": "AP000001",
        "status": "executed",
        "order_id": "A1024",
        "amount": 899,
        "applicant_sub": "u1001",
        "decided_by_sub": "u2001",
        "refund_id": "R000105",
    }
    res2 = check_write_invariants(baseline, [*base_refunds, new_refund], [exec_ap])
    assert res2.passed is True
    assert res2.new_refund_ids == ["R000105"]
    assert res2.orphan_refund_ids == []


def test_invariants_i1_i2_i3_violations() -> None:
    baseline = {"R000101"}
    r105 = {
        "refund_id": "R000105",
        "order_id": "A1024",
        "amount": 899,
        "created_by": "u2001",
        "created_by_role": "supervisor",
    }
    r106_orphan = {
        "refund_id": "R000106",
        "order_id": "A1023",
        "amount": 299,
        "created_by": "u1001",
        "created_by_role": "agent",
    }

    # I1: missing refund_id / unknown refund / duplicate refund_id / orphan refund
    # I3: pending approval with refund_id
    approvals = [
        {
            "approval_id": "AP1",
            "status": "executed",
            "order_id": "A1024",
            "amount": 899,
            "applicant_sub": "u1001",
            "decided_by_sub": "u2001",
            "refund_id": "R000105",
        },
        {
            "approval_id": "AP2",
            "status": "executed",
            "order_id": "A1024",
            "amount": 899,
            "applicant_sub": "u1001",
            "decided_by_sub": "u2001",
            "refund_id": "R000105",
        },
        {
            "approval_id": "AP3",
            "status": "executed",
            "order_id": "A1024",
            "amount": 899,
            "applicant_sub": "u1001",
            "decided_by_sub": "u2001",
            "refund_id": None,
        },
        {
            "approval_id": "AP4",
            "status": "pending",
            "order_id": "A1023",
            "amount": 299,
            "applicant_sub": "u1001",
            "refund_id": "R000999",
        },
    ]
    res = check_write_invariants(baseline, [r105, r106_orphan], approvals)
    assert res.passed is False
    assert res.orphan_refund_ids == ["R000106"]
    assert set(res.violated_approval_ids) == {"AP2", "AP3", "AP4"}
    rules = {v.rule for v in res.violations}
    assert rules == {"I1", "I3"}
    assert res.to_dict()["violations"][0]["rule"] in {"I1", "I3"}

    # I2: all 5 I2 checks failing (amount, order_id, created_by, role, self-approval)
    bad_refund = {
        "refund_id": "R000107",
        "order_id": "A9999",
        "amount": 100.0,
        "created_by": "u1001",
        "created_by_role": "agent",
    }
    bad_ap = {
        "approval_id": "AP5",
        "status": "executed",
        "order_id": "A1024",
        "amount": 899.0,
        "applicant_sub": "u2001",
        "decided_by_sub": "u2001",
        "refund_id": "R000107",
    }
    res2 = check_write_invariants(baseline, [bad_refund], [bad_ap])
    assert res2.passed is False
    i2_msgs = [v.message for v in res2.violations if v.rule == "I2"]
    assert len(i2_msgs) == 5

    # H4: incomplete probe snapshot fails I1, I2, I3 with explicit reason
    res_inc_err = check_write_invariants(
        baseline,
        [],
        [{"approval_id": "AP1", "status": "pending"}],
        probe_complete=False,
        probe_error="list_refunds 探针返回 503",
    )
    assert res_inc_err.passed is False
    assert {v.rule for v in res_inc_err.violations} == {"I1", "I2", "I3"}
    assert all("503" in v.message for v in res_inc_err.violations)
    assert res_inc_err.violated_approval_ids == ["AP1"]

    res_inc_default = check_write_invariants(baseline, [], [], probe_complete=False)
    assert res_inc_default.passed is False
    assert {v.rule for v in res_inc_default.violations} == {"I1", "I2", "I3"}
    assert res_inc_default.approval_ids == []


# ---------------------------------------------------------------------------
# Audit R1–R6 tests
# ---------------------------------------------------------------------------


def _mk_rec(
    rec_id: int,
    *,
    actor: str = "wang",
    event_type: str = "tool_call",
    channel: str = "agent",
    tool: str = "get_order",
    http_method: str | None = "GET",
    http_path: str | None = "/api/v1/orders/A1023",
    outcome: str = "success",
    status_code: int | None = 200,
    error_code: str | None = None,
    request_id: str = "req-1",
    order_id: str | None = "A1023",
    approval_id: str | None = None,
    on_behalf_of_sub: str | None = None,
    result: dict | None = None,
) -> dict:
    acc = ACCOUNTS[actor]
    return {
        "id": rec_id,
        "request_id": request_id,
        "event_type": event_type,
        "channel": channel,
        "actor_sub": acc.sub,
        "actor_role": acc.role,
        "actor_regions": acc.regions_csv,
        "tool": tool,
        "http_method": http_method,
        "http_path": http_path,
        "outcome": outcome,
        "status_code": status_code,
        "error_code": error_code,
        "order_id": order_id,
        "approval_id": approval_id,
        "on_behalf_of_sub": on_behalf_of_sub,
        "result": result,
    }


def test_status_code_to_outcome_all_branches() -> None:
    assert status_code_to_outcome(202, pending_approval=True) == "pending_approval"
    assert status_code_to_outcome(None) == "error"
    assert status_code_to_outcome(201) == "success"
    assert status_code_to_outcome(401) == "auth_failed"
    assert status_code_to_outcome(403) == "access_denied"
    assert status_code_to_outcome(404) == "not_found"
    assert status_code_to_outcome(409) == "conflict"
    assert status_code_to_outcome(422) == "invalid"
    assert status_code_to_outcome(503) == "error"


def test_audit_happy_path_sequential_and_approval_chain_and_backfill() -> None:
    # Two identical tool calls in same step (sequential match) + chat_trace backfill
    # Plus submit -> rejected attempt -> approve 200 + execute 201 -> executed!
    e_chat1 = LedgerEntry(
        "t1", "chat_trace", "wang", "tool_call", "agent", "get_order", "success", 200, trace_index=0
    )
    e_chat2 = LedgerEntry(
        "t1", "chat_trace", "wang", "tool_call", "agent", "get_order", "success", 200, trace_index=1
    )
    e_chat3 = LedgerEntry(
        "t1",
        "chat_trace",
        "wang",
        "tool_call",
        "agent",
        "get_order",
        "access_denied",
        403,
        "order_access_denied",
        request_id="r-403",
        trace_index=2,
    )
    e_sub = LedgerEntry(
        "s1",
        "mcp",
        "wang",
        "tool_call",
        "agent",
        "create_refund",
        "pending_approval",
        202,
        order_id="A1024",
        approval_id="AP1",
    )
    e_deny_ap = LedgerEntry(
        "g_deny",
        "guard_approve",
        "wang",
        "approval_approve",
        "console",
        "create_refund",
        "access_denied",
        403,
        "insufficient_role",
        request_id="r-deny",
        approval_id="AP1",
    )
    e_ok_ap = LedgerEntry(
        "g_ok",
        "guard_approve",
        "li",
        "approval_approve",
        "console",
        "create_refund",
        "success",
        200,
        request_id="r-ok",
        approval_id="AP1",
        applicant_sub="u1001",
        approval_status_after="executed",
        refund_id_after="R000105",
    )
    e_unauth = LedgerEntry(
        "u401", "unauthenticated_401", "wang", "tool_call", "agent", "get_order", "auth_failed", 401
    )
    e_404_ap = LedgerEntry(
        "g_404",
        "guard_approve",
        "li",
        "approval_approve",
        "console",
        "create_refund",
        "not_found",
        404,
        "approval_not_found",
        request_id="r-404",
        approval_id="AP999999",
    )

    # Roundtrip LedgerEntry serialization
    assert LedgerEntry.from_dict(e_sub.to_dict()) == e_sub

    step_audits = {
        "t1": [
            _mk_rec(1, order_id="A1032", request_id="r-1"),
            _mk_rec(2, order_id="A1027", request_id="r-2"),
            _mk_rec(
                3,
                order_id="B2087",
                http_path="/api/v1/orders/B2087",
                outcome="access_denied",
                status_code=403,
                error_code="order_access_denied",
                request_id="r-403",
            ),
        ],
        "s1": [
            _mk_rec(
                4,
                tool="create_refund",
                http_method="POST",
                http_path="/api/v1/refunds",
                outcome="pending_approval",
                status_code=202,
                order_id="A1024",
                approval_id="AP1",
                request_id="r-sub",
            ),
        ],
        "g_deny": [
            _mk_rec(
                5,
                actor="wang",
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="access_denied",
                status_code=403,
                error_code="insufficient_role",
                approval_id="AP1",
                request_id="r-deny",
            ),
        ],
        "g_ok": [
            _mk_rec(
                6,
                actor="li",
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="success",
                status_code=200,
                approval_id="AP1",
                request_id="r-ok",
                result={"from": "pending", "to": "executing"},
            ),
            _mk_rec(
                7,
                actor="li",
                event_type="approval_execute",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="success",
                status_code=201,
                approval_id="AP1",
                request_id="r-ok",
                on_behalf_of_sub="u1001",
                result={"from": "executing", "to": "executed", "refund_id": "R000105"},
            ),
        ],
        "u401": [],
        "g_404": [],
    }
    res = reconcile_case_audit(
        step_order=["t1", "s1", "g_deny", "g_ok", "u401", "g_404"],
        step_ledgers={
            "t1": [e_chat1, e_chat2, e_chat3],
            "s1": [e_sub],
            "g_deny": [e_deny_ap],
            "g_ok": [e_ok_ap],
            "u401": [e_unauth],
            "g_404": [e_404_ap],
        },
        step_audits=step_audits,
        accounts=ACCOUNTS,
        tools_manifest=MANIFEST,
        final_approvals={
            "AP1": {"approval_id": "AP1", "status": "executed", "refund_id": "R000105"}
        },
        new_refund_ids=["R000105"],
        issued_tokens=["", "eyJaaa.eyJbbb.sig1234567890123456"],
    )
    assert res.passed is True, res.issues
    assert res.expected_count == 7
    assert res.matched_count == 7
    assert res.unexpected_count == 0
    assert res.backfilled_order_ids == {"t1:0": "A1032", "t1:1": "A1027", "t1:2": "B2087"}
    assert res.to_dict()["passed"] is True
    # L7-3: approval chain includes applicant_sub, approver_sub, refund_id; record_matches populated
    assert len(res.approval_chains) == 1
    ch = res.approval_chains[0]
    assert ch.applicant_sub == "u1001"
    assert ch.approver_sub == "u2001"
    assert ch.refund_id == "R000105"
    assert len(res.record_matches) == 7
    assert all(m["status"] == "matched" and m["match_label"] == "匹配" for m in res.record_matches)


def test_audit_approve_case_c_and_case_d_legal_shapes() -> None:
    # 5.13 explicitly requires: "同意 200 + 执行 403 后仍为 pending" 判为合法!
    # Plus Case D1 (401 with 0 records) and Case D2 (403 invalid_claims with 2 records)
    e_sub = LedgerEntry(
        "s1",
        "mcp",
        "wang",
        "tool_call",
        "agent",
        "create_refund",
        "pending_approval",
        202,
        order_id="A1023",
        approval_id="AP1",
    )
    e_case_c = LedgerEntry(
        "g_c",
        "guard_approve",
        "li",
        "approval_approve",
        "console",
        "create_refund",
        "access_denied",
        403,
        "order_access_denied",
        request_id="r-c",
        approval_id="AP1",
        applicant_sub="u1001",
    )
    e_case_d1 = LedgerEntry(
        "g_d1",
        "guard_approve",
        "li",
        "approval_approve",
        "console",
        "create_refund",
        "auth_failed",
        401,
        "token_expired",
        request_id="r-d1",
        approval_id="AP1",
        applicant_sub="u1001",
    )
    e_case_d2 = LedgerEntry(
        "g_d2",
        "guard_approve",
        "li",
        "approval_approve",
        "console",
        "create_refund",
        "access_denied",
        403,
        "invalid_claims",
        request_id="r-d2",
        approval_id="AP1",
        applicant_sub="u1001",
    )

    step_audits = {
        "s1": [
            _mk_rec(
                1,
                tool="create_refund",
                http_method="POST",
                http_path="/api/v1/refunds",
                outcome="pending_approval",
                status_code=202,
                approval_id="AP1",
                request_id="r-sub",
            )
        ],
        "g_c": [
            _mk_rec(
                2,
                actor="li",
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="success",
                status_code=200,
                approval_id="AP1",
                request_id="r-c",
                result={"from": "pending", "to": "executing"},
            ),
            _mk_rec(
                3,
                actor="li",
                event_type="approval_execute",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="access_denied",
                status_code=403,
                error_code="order_access_denied",
                approval_id="AP1",
                request_id="r-c",
                on_behalf_of_sub="u1001",
                result={"from": "executing", "to": "pending"},
            ),
        ],
        "g_d1": [],
        "g_d2": [
            _mk_rec(
                4,
                actor="li",
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="success",
                status_code=200,
                approval_id="AP1",
                request_id="r-d2",
                result={"from": "pending", "to": "executing"},
            ),
            _mk_rec(
                5,
                actor="li",
                event_type="approval_execute",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="access_denied",
                status_code=403,
                error_code="invalid_claims",
                approval_id="AP1",
                request_id="r-d2",
                on_behalf_of_sub="u1001",
                result={"from": "executing", "to": "pending"},
            ),
        ],
    }
    res = reconcile_case_audit(
        step_order=["s1", "g_c", "g_d1", "g_d2"],
        step_ledgers={"s1": [e_sub], "g_c": [e_case_c], "g_d1": [e_case_d1], "g_d2": [e_case_d2]},
        step_audits=step_audits,
        accounts=ACCOUNTS,
        tools_manifest=MANIFEST,
        final_approvals={"AP1": {"approval_id": "AP1", "status": "pending"}},
        new_refund_ids=[],
        issued_tokens=[],
    )
    assert res.passed is True, res.issues
    assert res.expected_count == 5
    assert res.matched_count == 5


def test_audit_r1_r2_r4_r5_r6_error_branches() -> None:
    tok = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.abcdef0123456789abcdef"

    # R1: missing req_id/seq entry, unexpected extra, field mismatches on both paths
    e_miss_req = LedgerEntry(
        "s1",
        "mcp",
        "wang",
        "tool_call",
        "agent",
        "get_order",
        "not_found",
        404,
        "order_not_found",
        request_id="missing-id",
    )
    e_mismatch_req = LedgerEntry(
        "s1",
        "guard_approve",
        "li",
        "approval_approve",
        "console",
        "create_refund",
        "success",
        200,
        request_id="r-mm",
        approval_id="AP1",
        applicant_sub="u1001",
        approval_status_after="executed",
        refund_id_after="R000105",
    )
    e_seq_bad = LedgerEntry(
        "s2",
        "mcp",
        "wang",
        "tool_call",
        "agent",
        "get_order",
        "success",
        200,
        order_id="A1023",
        approval_id="AP1",
    )
    e_seq_miss = LedgerEntry("s2", "mcp", "wang", "tool_call", "agent", "get_order", "success", 200)
    e_unknown_actor = LedgerEntry(
        "s3", "mcp", "ghost", "tool_call", "agent", "get_order", "success", 200
    )

    step_audits = {
        "s1": [
            # Approve matches req_id, execute has mismatched fields + leaked token + bad R5 outcome
            _mk_rec(
                1,
                actor="li",
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                http_method=None,
                http_path=None,
                outcome="success",
                status_code=200,
                approval_id="AP1",
                request_id="r-mm",
                result={"from": "pending", "to": "executing"},
            ),
            _mk_rec(
                2,
                actor="wang",
                event_type="approval_execute",
                channel="agent",
                tool="get_order",
                http_method=None,
                http_path=None,
                outcome="access_denied",
                status_code=403,
                error_code=None,
                approval_id="AP2",
                request_id="r-mm",
                on_behalf_of_sub="u9999",
                result={"from": "executing", "to": "execution_failed", "refund_id": "R999"},
            ),
            # Extra unexpected record with leaked token
            _mk_rec(3, order_id=tok, request_id="r-extra"),
        ],
        "s2": [
            # Mismatched seq record: unknown tool, bad pending_approval status, wrong fields
            _mk_rec(
                4,
                actor="zhao",
                event_type="tool_call",
                channel="console",
                tool="unknown_tool",
                http_method="POST",
                http_path=None,
                outcome="pending_approval",
                status_code=200,
                error_code="err",
                order_id="B2087",
                approval_id="AP9",
                request_id="r-seq",
            ),
        ],
        "s3": [
            # Good actor unknown, bad http_method/path, wrong outcome for 200
            _mk_rec(
                5,
                tool="get_order",
                http_method="POST",
                http_path="",
                outcome="not_found",
                status_code=200,
                request_id="r-3",
            ),
        ],
    }

    # Test request_id match skipping already-used index
    e_dup_req = LedgerEntry(
        "s4",
        "mcp",
        "wang",
        "tool_call",
        "agent",
        "get_order",
        "access_denied",
        403,
        "order_access_denied",
        request_id="r-same",
    )
    e_dup_req2 = LedgerEntry(
        "s4",
        "mcp",
        "wang",
        "tool_call",
        "agent",
        "get_order",
        "access_denied",
        403,
        "order_access_denied",
        request_id="r-same",
    )
    step_audits["s4"] = [
        _mk_rec(
            6,
            outcome="access_denied",
            status_code=403,
            error_code="order_access_denied",
            request_id="r-same",
        ),
        _mk_rec(
            7,
            outcome="access_denied",
            status_code=403,
            error_code="order_access_denied",
            request_id="r-same",
        ),
    ]

    res = reconcile_case_audit(
        step_order=["s1", "s2", "s3", "s4"],
        step_ledgers={
            "s1": [e_miss_req, e_mismatch_req],
            "s2": [e_seq_bad, e_seq_miss],
            "s3": [e_unknown_actor],
            "s4": [e_dup_req, e_dup_req2],
        },
        step_audits=step_audits,
        accounts=ACCOUNTS,
        tools_manifest=MANIFEST,
        final_approvals={},
        new_refund_ids=["R000105"],  # triggers R4 mismatch!
        issued_tokens=[tok],
    )
    assert res.passed is False
    assert len(res.to_dict()["issues"]) > 0
    rules = {i.rule for i in res.issues}
    assert {"R1", "R2", "R4", "R5", "R6"}.issubset(rules)
    assert res.unexpected_count == 1
    match_statuses = {m["status"] for m in res.record_matches}
    assert {"matched", "mismatch", "missing", "unexpected"}.issubset(match_statuses)
    match_labels = {m["match_label"] for m in res.record_matches}
    assert {"匹配", "字段不符", "缺失", "多余"}.issubset(match_labels)

    # Specifically cover _compare_entry_and_record event_type/status_code/error_code mismatches
    # and _path_matches_template with empty path when http_method matches
    e_cmp = LedgerEntry(
        "s5", "mcp", "wang", "tool_call", "agent", "get_order", "not_found", 404, "order_not_found"
    )
    res_cmp = reconcile_case_audit(
        step_order=["s5"],
        step_ledgers={"s5": [e_cmp]},
        step_audits={
            "s5": [
                _mk_rec(
                    10,
                    event_type="approval_reject",
                    tool="get_order",
                    http_method="GET",
                    http_path="",
                    outcome="access_denied",
                    status_code=403,
                    error_code="order_access_denied",
                    request_id="r-5",
                ),
            ]
        },
        accounts=ACCOUNTS,
        tools_manifest=MANIFEST,
        final_approvals={},
        new_refund_ids=[],
        issued_tokens=[],
    )
    assert any(
        "event_type" in i.message and "status_code" in i.message and "error_code" in i.message
        for i in res_cmp.issues
    )

    # Also test _path_matches_template empty path on tool_call with matching GET method
    e_empty_path = LedgerEntry(
        "s6", "mcp", "wang", "tool_call", "agent", "get_order", "success", 200
    )
    res_ep = reconcile_case_audit(
        step_order=["s6"],
        step_ledgers={"s6": [e_empty_path]},
        step_audits={
            "s6": [
                _mk_rec(
                    11,
                    tool="get_order",
                    http_method="GET",
                    http_path="",
                    outcome="success",
                    status_code=200,
                )
            ]
        },
        accounts=ACCOUNTS,
        tools_manifest=MANIFEST,
        final_approvals={},
        new_refund_ids=[],
        issued_tokens=[],
    )
    assert any(i.rule == "R5" and "method/path" in i.message for i in res_ep.issues)


def test_audit_r3_all_chain_branches() -> None:
    # Valid chains: rejected, execution_failed, executing, expired, retry 403 -> executing
    valid_records = [
        # AP_REJ: submit -> rejected attempt (422) -> reject 200
        _mk_rec(
            1,
            tool="create_refund",
            http_method="POST",
            http_path="/api/v1/refunds",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_REJ",
        ),
        _mk_rec(
            2,
            actor="li",
            event_type="approval_reject",
            channel="console",
            tool="create_refund",
            outcome="invalid",
            status_code=422,
            error_code="validation_error",
            approval_id="AP_REJ",
        ),
        _mk_rec(
            3,
            actor="li",
            event_type="approval_reject",
            channel="console",
            tool="create_refund",
            outcome="success",
            status_code=200,
            approval_id="AP_REJ",
            result={"from": "pending", "to": "rejected"},
        ),
        # AP_FAIL: submit -> approve 200 -> execute 409 execution_failed
        _mk_rec(
            4,
            tool="create_refund",
            http_method="POST",
            http_path="/api/v1/refunds",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_FAIL",
        ),
        _mk_rec(
            5,
            actor="li",
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            outcome="success",
            status_code=200,
            approval_id="AP_FAIL",
            request_id="rf",
            result={"from": "pending", "to": "executing"},
        ),
        _mk_rec(
            6,
            actor="li",
            event_type="approval_execute",
            channel="console",
            tool="create_refund",
            outcome="conflict",
            status_code=409,
            error_code="order_not_refundable",
            approval_id="AP_FAIL",
            request_id="rf",
            on_behalf_of_sub="u1001",
            result={"from": "executing", "to": "execution_failed"},
        ),
        # AP_EXEC: submit -> approve 200 -> execute 503 executing -> retry 403 executing
        _mk_rec(
            7,
            tool="create_refund",
            http_method="POST",
            http_path="/api/v1/refunds",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_EXEC",
        ),
        _mk_rec(
            8,
            actor="li",
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            outcome="success",
            status_code=200,
            approval_id="AP_EXEC",
            request_id="re",
            result={"from": "pending", "to": "executing"},
        ),
        _mk_rec(
            9,
            actor="li",
            event_type="approval_execute",
            channel="console",
            tool="create_refund",
            outcome="error",
            status_code=503,
            approval_id="AP_EXEC",
            request_id="re",
            on_behalf_of_sub="u1001",
            result={"from": "executing", "to": "executing"},
        ),
        _mk_rec(
            10,
            actor="li",
            event_type="approval_execute",
            channel="console",
            tool="create_refund",
            outcome="access_denied",
            status_code=403,
            error_code="order_access_denied",
            approval_id="AP_EXEC",
            request_id="re2",
            on_behalf_of_sub="u1001",
            result={"from": "executing", "to": "executing"},
        ),
        # AP_EXP: submit -> expire
        _mk_rec(
            11,
            tool="create_refund",
            http_method="POST",
            http_path="/api/v1/refunds",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_EXP",
        ),
        _mk_rec(
            12,
            actor="sun",
            event_type="approval_expire",
            channel="system",
            tool="create_refund",
            outcome="success",
            status_code=None,
            approval_id="AP_EXP",
            result={"from": "pending", "to": "expired"},
        ),
        # Non-approval get_approval event ignored by R3
        _mk_rec(13, tool="get_order", approval_id="AP_REJ"),
    ]
    from harness.audit import _check_r3_approval_chains

    summaries, issues = _check_r3_approval_chains(
        valid_records,
        {
            "AP_REJ": {"status": "rejected"},
            "AP_FAIL": {"status": "execution_failed"},
            "AP_EXEC": {"status": "executing"},
            "AP_EXP": {"status": "expired"},
        },
    )
    assert issues == []
    assert all(s.passed for s in summaries)

    # Broken chains exercising every single R3 failure branch
    broken_records = [
        # AP_BAD1: wrong first event, bad approve transitions/statuses, missing follow-up
        _mk_rec(
            20,
            actor="li",
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            status_code=200,
            approval_id="AP_BAD1",
            request_id="b1",
            result={},
        ),
        _mk_rec(
            21,
            actor="li",
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            status_code=200,
            approval_id="AP_BAD1",
            request_id="b2",
            result={},
        ),
        _mk_rec(
            22,
            actor="li",
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            status_code=403,
            approval_id="AP_BAD1",
            request_id="b3",
            result={"to": "x"},
        ),
        _mk_rec(
            23,
            actor="li",
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            status_code=500,
            approval_id="AP_BAD1",
            request_id="b4",
        ),
        # AP_BAD2: submit followed by every invalid execute/reject/expire/dangling branch
        _mk_rec(
            30,
            tool="create_refund",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_BAD2",
        ),
        _mk_rec(
            31,
            actor="li",
            event_type="approval_execute",
            status_code=201,
            approval_id="AP_BAD2",
            on_behalf_of_sub="wrong",
            result={"to": "executed"},
        ),
        _mk_rec(
            32,
            actor="li",
            event_type="approval_execute",
            status_code=201,
            approval_id="AP_BAD2",
            on_behalf_of_sub="u1001",
            result={"to": "executed", "refund_id": "R1"},
        ),
        _mk_rec(
            33,
            actor="li",
            event_type="approval_execute",
            status_code=403,
            approval_id="AP_BAD2",
            on_behalf_of_sub="u1001",
            result={"to": "bad"},
        ),
        _mk_rec(
            34,
            actor="li",
            event_type="approval_execute",
            status_code=409,
            approval_id="AP_BAD2",
            on_behalf_of_sub="u1001",
            result={"to": "bad"},
        ),
        _mk_rec(
            35,
            actor="li",
            event_type="approval_execute",
            status_code=503,
            approval_id="AP_BAD2",
            on_behalf_of_sub="u1001",
            result={"to": "bad"},
        ),
        _mk_rec(
            36,
            actor="li",
            event_type="approval_reject",
            status_code=200,
            approval_id="AP_BAD2",
            result={"to": "bad"},
        ),
        _mk_rec(
            37,
            actor="li",
            event_type="approval_reject",
            status_code=403,
            approval_id="AP_BAD2",
            result={"to": "bad"},
        ),
        _mk_rec(
            38, actor="li", event_type="approval_reject", status_code=500, approval_id="AP_BAD2"
        ),
        _mk_rec(
            39,
            actor="sun",
            event_type="approval_expire",
            status_code=None,
            approval_id="AP_BAD2",
            result={"to": "bad"},
        ),
        _mk_rec(
            40, actor="sun", event_type="approval_weird", status_code=200, approval_id="AP_BAD2"
        ),
        _mk_rec(
            41,
            actor="li",
            event_type="approval_approve",
            status_code=200,
            approval_id="AP_BAD2",
            request_id="dangle",
            result={"from": "pending", "to": "executing"},
        ),
        # AP_BAD3: executed refund_id mismatch
        _mk_rec(
            50,
            tool="create_refund",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_BAD3",
        ),
        _mk_rec(
            51,
            actor="li",
            event_type="approval_approve",
            status_code=200,
            approval_id="AP_BAD3",
            request_id="b3",
            result={"from": "pending", "to": "executing"},
        ),
        _mk_rec(
            52,
            actor="li",
            event_type="approval_execute",
            status_code=201,
            approval_id="AP_BAD3",
            request_id="b3",
            on_behalf_of_sub="u1001",
            result={"from": "executing", "to": "executed", "refund_id": "R105"},
        ),
        # AP_BAD4: pending status with non-pending last_transition
        _mk_rec(
            60,
            tool="create_refund",
            outcome="pending_approval",
            status_code=202,
            approval_id="AP_BAD4",
        ),
        _mk_rec(
            61,
            actor="li",
            event_type="approval_execute",
            status_code=403,
            approval_id="AP_BAD4",
            on_behalf_of_sub="u1001",
            result={"from": "executing", "to": "bad"},
        ),
    ]
    b_sums, b_issues = _check_r3_approval_chains(
        broken_records,
        {
            "AP_MISSING": {"status": "pending"},
            "AP_BAD2": {"status": "executed", "refund_id": "R999"},
            "AP_BAD3": {"status": "executed", "refund_id": "R999"},
            "AP_BAD4": {"status": "pending"},
        },
    )
    assert len(b_issues) >= 15
    assert any(s.approval_id == "AP_MISSING" and not s.passed for s in b_sums)
