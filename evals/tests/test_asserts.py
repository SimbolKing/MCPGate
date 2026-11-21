"""Exhaustive unit tests for `harness/asserts.py` targeting 100% statement + branch coverage."""

from __future__ import annotations

from harness.asserts import (
    REDACTED_TOKEN,
    JudgeVerdictResult,
    _step_level,
    check_g8_only_keys,
    classify_verdict_by_rules,
    contains_jwt_like,
    evaluate_chat_step,
    evaluate_guard_step,
    evaluate_mcp_step,
    evaluate_trace_expect,
    evaluate_trace_forbid,
    extract_conclusion_segment,
    find_leaked_tokens,
    match_citation,
    match_clause,
    redact_tokens,
)
from harness.schema import ChatStep, GuardStep, McpStep, TraceExpect, TraceForbidItem


def test_conclusion_segment_and_verdict_rules() -> None:
    # With 结论 + 依据
    assert (
        extract_conclusion_segment("前言。\n结论：可退货退款。\n依据：第 3.2 条。")
        == "结论：可退货退款。"
    )
    # With 结论 + blank line (no 依据)
    assert extract_conclusion_segment("结论：不可退。\n\n其他补充说明。") == "结论：不可退。"
    # With 结论 to EOF
    assert extract_conclusion_segment("结论：需请示主管") == "结论：需请示主管"
    # Without 结论, with period
    assert extract_conclusion_segment("本单可以退货。后续内容不可退。") == "本单可以退货。"
    # Without 结论, without period
    assert extract_conclusion_segment("需先确认原因") == "需先确认原因"

    # Every label in rule table
    assert classify_verdict_by_rules("结论：可退货退款。依据：第 2.1 条") == "refundable"
    assert classify_verdict_by_rules("订单可以退。") == "refundable"
    assert classify_verdict_by_rules("支持退货退款。") == "refundable"
    assert classify_verdict_by_rules("同意退货。") == "refundable"

    # Negative must NOT match positive ("不可退"/"不可以退"/"不同意退" contain positive substrings)
    assert classify_verdict_by_rules("结论：不可退。依据：第 2.2 条") == "not_refundable"
    assert classify_verdict_by_rules("结论：不可以退货。依据：第 2.2 条") == "not_refundable"
    assert classify_verdict_by_rules("结论：不同意退款。依据：第 4.1 条") == "not_refundable"
    assert classify_verdict_by_rules("本单不得退货。") == "not_refundable"
    assert classify_verdict_by_rules("该情况不应退款。") == "not_refundable"
    assert classify_verdict_by_rules("不满足退货条件。") == "not_refundable"
    assert classify_verdict_by_rules("结论：不符合退货规定。") == "not_refundable"
    assert classify_verdict_by_rules("结论：不符合退款条件。") == "not_refundable"
    assert classify_verdict_by_rules("不能退货。") == "not_refundable"
    assert classify_verdict_by_rules("生鲜不支持退货。") == "not_refundable"
    assert classify_verdict_by_rules("不予受理。") == "not_refundable"
    assert classify_verdict_by_rules("无法退货。") == "not_refundable"
    assert classify_verdict_by_rules("没有可退款项。") == "not_refundable"

    assert classify_verdict_by_rules("结论：需补充信息。") == "need_info"
    assert classify_verdict_by_rules("需要补充原因。") == "need_info"
    assert classify_verdict_by_rules("先确认客户诉求。") == "need_info"
    assert classify_verdict_by_rules("需确认原因。") == "need_info"

    assert classify_verdict_by_rules("结论：请示主管裁定。") == "escalate"

    # Adversative sentence -> matches both negative and positive -> unknown (triggers judge)
    assert classify_verdict_by_rules("结论：已超期不能退无理由，但属质量问题可以退。") == "unknown"
    # No keywords at all -> unknown
    assert classify_verdict_by_rules("订单 A1023 已签收 18 天。") == "unknown"


def test_clause_citation_jwt_token_g8_helpers() -> None:
    assert match_clause("依据《售后制度》第 3.2 条办理", "售后制度", "3.2") is True
    assert match_clause("依据第3.2条办理", "售后制度", "3.2") is True
    assert match_clause("依据第 2.10 条", "售后制度", "2.1") is False
    assert match_clause("详见《故障排查手册》第 1.1 条", "故障排查手册", "1.1") is True
    assert match_clause("详见第 1.1 条", "故障排查手册", "1.1") is False

    cites = [{"document": "售后制度", "clause": "3.2"}]
    assert match_citation(cites, "售后制度", "3.2") is True
    assert match_citation(cites, "售后制度", "2.1") is False

    jwt_tok = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.abcdef0123456789abcdef"
    assert contains_jwt_like(f"token is {jwt_tok}") is True
    assert contains_jwt_like("normal text") is False

    assert find_leaked_tokens({"answer": "ok"}, ["", jwt_tok]) == []
    assert find_leaked_tokens({"answer": f"leak {jwt_tok}"}, [jwt_tok]) == ["token#1"]
    assert find_leaked_tokens({"answer": "sig abcdef0123456789abcdef"}, [jwt_tok]) == ["token#1"]
    assert find_leaked_tokens({"answer": "short"}, ["a.b.c"]) == []

    # Redaction covers full token, signature-only, arbitrary JWT pattern, nested lists/dicts, ints
    other_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.zzzzzzzzzzzzzzzzzzzz"
    redacted = redact_tokens(
        {
            "full": f"Bearer {jwt_tok}",
            "sig": "abcdef0123456789abcdef",
            "jwt_only": other_jwt,
            "nested": [jwt_tok, 42, None],
            jwt_tok: "key_redacted",
        },
        ["", jwt_tok, "a.b.c"],
    )
    assert redacted["full"] == f"Bearer {REDACTED_TOKEN}"
    assert redacted["sig"] == REDACTED_TOKEN
    assert redacted["jwt_only"] == REDACTED_TOKEN
    assert redacted["nested"] == [REDACTED_TOKEN, 42, None]
    assert REDACTED_TOKEN in redacted

    assert check_g8_only_keys(["code", "message", "request_id"]) is True
    assert check_g8_only_keys(["code", "message"]) is False
    assert check_g8_only_keys(None) is False


def test_trace_expect_and_forbid() -> None:
    actual = [
        {"tool": "get_order", "kind": "ok", "order_id": "A1023"},
        {"tool": "create_refund", "kind": "conflict", "error_code": "approval_pending_exists"},
    ]
    # exact pass & fail (length, tool/kind, error_code, order_id)
    assert evaluate_trace_expect(
        actual,
        TraceExpect(
            mode="exact",
            calls=[
                {"tool": "get_order", "kind": "ok", "order_id": "A1023"},
                {
                    "tool": "create_refund",
                    "kind": "conflict",
                    "error_code": "approval_pending_exists",
                },
            ],
        ),
    )
    assert not evaluate_trace_expect(
        actual, TraceExpect(mode="exact", calls=[{"tool": "get_order", "kind": "ok"}])
    )
    assert not evaluate_trace_expect(
        actual,
        TraceExpect(
            mode="exact",
            calls=[
                {"tool": "get_order", "kind": "ok", "order_id": "B2087"},
                {"tool": "create_refund", "kind": "conflict"},
            ],
        ),
    )
    assert not evaluate_trace_expect(
        actual,
        TraceExpect(
            mode="exact",
            calls=[
                {"tool": "get_order", "kind": "ok"},
                {"tool": "create_refund", "kind": "conflict", "error_code": "other"},
            ],
        ),
    )
    assert not evaluate_trace_expect(
        actual,
        TraceExpect(
            mode="exact",
            calls=[
                {"tool": "get_order", "kind": "denied"},
                {"tool": "create_refund", "kind": "conflict"},
            ],
        ),
    )

    # contains mode pass & fail
    assert evaluate_trace_expect(
        actual,
        TraceExpect(mode="contains", calls=[{"tool": "create_refund", "kind": "conflict"}]),
    )
    assert not evaluate_trace_expect(
        actual,
        TraceExpect(
            mode="contains",
            calls=[
                {"tool": "create_refund", "kind": "conflict"},
                {"tool": "get_order", "kind": "ok"},
            ],
        ),
    )

    # trace_forbid
    assert evaluate_trace_forbid(actual, [TraceForbidItem(tool="list_refunds")]) == []
    assert len(evaluate_trace_forbid(actual, [TraceForbidItem(tool="create_refund")])) == 1
    assert len(evaluate_trace_forbid(actual, [TraceForbidItem(tool="get_order", kind="ok")])) == 1
    assert evaluate_trace_forbid(actual, [TraceForbidItem(tool="get_order", kind="denied")]) == []


def test_evaluate_mcp_and_guard_steps() -> None:
    sentinels = {"B2087": ["平板电脑", "2499"]}

    # MCP authz deny with target + not_contains + status + error_code + message_contains (str)
    mcp_deny = McpStep.model_validate(
        {
            "id": "s1",
            "do": "mcp",
            "as": "wang",
            "tool": "get_order",
            "args": {"order_id": "B2087"},
            "authz": "deny",
            "target": "B2087",
            "expect": {
                "result": "denied",
                "status": 403,
                "error_code": "order_access_denied",
                "message_contains": "无权访问",
                "not_contains": ["机密"],
            },
        }
    )
    resp_ok_deny = {
        "result": "denied",
        "status_code": 403,
        "error_code": "order_access_denied",
        "message": "无权访问订单 B2087。",
        "raw_error_body_keys": ["code", "message", "request_id"],
        "has_structured_content": False,
        "data": {},
    }
    res_list = evaluate_mcp_step(
        mcp_deny, resp_ok_deny, layer="tool", category="isolation", sentinels=sentinels
    )
    assert all(r.passed for r in res_list)
    assert all(r.level == "must" for r in res_list)
    assert res_list[0].to_dict()["name"] == "result"

    # Sentinel leak + structuredContent present + bool/int mismatch in data
    mcp_data = McpStep.model_validate(
        {
            "id": "s2",
            "do": "mcp",
            "as": "sun",
            "tool": "list_approvals",
            "expect": {
                "result": "ok",
                "data": {"total": 1, "flag": True},
                "message_contains": ["a"],
            },
        }
    )
    res_d = evaluate_mcp_step(
        mcp_data,
        {"result": "ok", "data": {"total": True, "flag": 1}, "message": ""},
        layer="dialogue",
        category="overview",
        sentinels=sentinels,
    )
    assert all(r.level == "should" for r in res_d)
    assert any(r.name == "data:total" and not r.passed for r in res_d)
    assert any(r.name == "data:flag" and not r.passed for r in res_d)
    assert any(r.name == "message_contains" and not r.passed for r in res_d)

    # MCP authz deny without target
    mcp_deny_no_target = McpStep.model_validate(
        {
            "id": "s3",
            "do": "mcp",
            "as": "wang",
            "tool": "list_refunds",
            "authz": "deny",
            "expect": {"result": "denied", "error_code": "insufficient_role"},
        }
    )
    res_nt = evaluate_mcp_step(
        mcp_deny_no_target,
        {
            "result": "denied",
            "error_code": "insufficient_role",
            "message": "仅主管",
            "raw_error_body_keys": ["code", "message", "request_id"],
            "has_structured_content": False,
        },
        layer="tool",
        category="write-gate",
        sentinels=sentinels,
    )
    assert all(r.passed for r in res_nt)

    # Guard step: deny with target, data, message_contains, not_contains
    g_step = GuardStep.model_validate(
        {
            "id": "g1",
            "do": "guard",
            "as": "zhou",
            "call": "approve",
            "approval": "AP000001",
            "authz": "deny",
            "target": "B2087",
            "expect": {
                "status": 403,
                "error_code": "approval_access_denied",
                "data": {"code": "approval_access_denied"},
                "message_contains": "无权",
                "not_contains": ["2499"],
            },
        }
    )
    g_res = evaluate_guard_step(
        g_step,
        {
            "http_status": 403,
            "error_code": "approval_access_denied",
            "message": "无权审批（不含敏感）",
            "data": {"code": "approval_access_denied"},
            "raw_error_body_keys": ["code", "message", "request_id"],
        },
        layer="guard",
        category="approval",
        sentinels=sentinels,
    )
    assert all(r.passed for r in g_res)

    # Guard audit category without authz & guard deny without target
    g_audit = GuardStep.model_validate(
        {"id": "g2", "do": "guard", "as": "sun", "call": "audit_logs", "expect": {"status": 200}}
    )
    ga_res = evaluate_guard_step(
        g_audit,
        {"http_status": 200, "data": {}},
        layer="guard",
        category="audit",
        sentinels=sentinels,
    )
    assert ga_res[0].attribution == "审计"

    g_deny_nt = GuardStep.model_validate(
        {
            "id": "g3",
            "do": "guard",
            "as": "wang",
            "call": "audit_logs",
            "authz": "deny",
            "expect": {"status": 403},
        }
    )
    gnt_res = evaluate_guard_step(
        g_deny_nt,
        {
            "http_status": 403,
            "message": "forbidden",
            "raw_error_body_keys": ["code", "message", "request_id"],
        },
        layer="guard",
        category="audit",
        sentinels=sentinels,
    )
    assert all(r.passed for r in gnt_res)


def test_evaluate_chat_step_all_branches() -> None:
    sentinels = {"B2087": ["平板电脑", "2499"]}
    tok = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.abcdef0123456789abcdef"

    # 1. HTTP expect (with code and without code)
    http_step = ChatStep.model_validate(
        {
            "id": "c1",
            "do": "chat",
            "as": "wang",
            "token": "expired",
            "query": "hi",
            "expect": {"http": {"status": 401, "code": "token_expired"}},
        }
    )
    r_http = evaluate_chat_step(
        http_step,
        {"http_status": 401, "error_code": "token_expired"},
        category="identity",
        sentinels=sentinels,
        issued_tokens=[tok],
    )
    assert all(r.passed for r in r_http)

    http_step_nocode = ChatStep.model_validate(
        {
            "id": "c1b",
            "do": "chat",
            "as": "wang",
            "query": "hi",
            "expect": {"http": {"status": 401}},
        }
    )
    r_http2 = evaluate_chat_step(
        http_step_nocode,
        {"http_status": 401},
        category="identity",
        sentinels=sentinels,
        issued_tokens=[tok],
    )
    assert len(r_http2) == 2 and all(r.passed for r in r_http2)

    # 2. Unexpected non-200 on normal chat step
    normal_step = ChatStep.model_validate(
        {
            "id": "c2",
            "do": "chat",
            "as": "wang",
            "query": "hi",
            "expect": {"should": {"contains_all": ["ok"]}},
        }
    )
    r_502 = evaluate_chat_step(
        normal_step,
        {"http_status": 502},
        category="policy",
        sentinels=sentinels,
        issued_tokens=[tok],
    )
    assert any(r.name == "http_status" and not r.passed for r in r_502)

    # 3. Full block assertions: trace, forbid, answer, contains, clauses, citations, verdict, conv
    rich_step = ChatStep.model_validate(
        {
            "id": "c3",
            "do": "chat",
            "as": "wang",
            "query": "订单 B2087",
            "authz": "deny",
            "target": "B2087",
            "expect": {
                "must": {
                    "trace_forbid": [{"tool": "create_refund"}],
                    "not_contains": ["机密"],
                    "no_jwt": True,
                    "conversation_changed": True,
                },
                "should": {
                    "trace": {"mode": "exact", "calls": [{"tool": "get_order", "kind": "ok"}]},
                    "answer_equals": "结论：可退。依据《售后制度》第 3.2 条。",
                    "answer_in": ["结论：可退。依据《售后制度》第 3.2 条。"],
                    "contains_all": ["可退"],
                    "contains_any": ["依据"],
                    "clauses": [{"doc": "售后制度", "clause": "3.2"}],
                    "clauses_any": [{"doc": "售后制度", "clause": "3.2"}],
                    "citations": [{"doc": "售后制度", "clause": "3.2"}],
                    "verdict": "refundable",
                },
            },
        }
    )
    resp_rich = {
        "http_status": 200,
        "conversation_id": "conv-2",
        "answer": "结论：可退。依据《售后制度》第 3.2 条。",
        "trace": [{"tool": "get_order", "kind": "ok"}],
        "citations": [{"document": "售后制度", "clause": "3.2"}],
    }
    r_rich = evaluate_chat_step(
        rich_step,
        resp_rich,
        category="policy",
        sentinels=sentinels,
        issued_tokens=[tok],
        prev_conversation_id="conv-1",
    )
    assert all(r.passed for r in r_rich)

    # 4. Clause missing when cited (attribution=生成) vs uncited (attribution=检索)
    clause_step = ChatStep.model_validate(
        {
            "id": "c4",
            "do": "chat",
            "as": "wang",
            "query": "能退吗？",
            "expect": {
                "should": {
                    "clauses": [{"doc": "售后制度", "clause": "3.2"}],
                    "citations": [{"doc": "售后制度", "clause": "3.2"}],
                    "contains_all": ["缺失词"],
                    "contains_any": ["不存在"],
                    "not_contains": ["触发"],
                    "trace_forbid": [{"tool": "get_order"}],
                }
            },
        }
    )
    r_gen = evaluate_chat_step(
        clause_step,
        {
            "http_status": 200,
            "answer": "触发",
            "trace": [{"tool": "get_order", "kind": "ok"}],
            "citations": [{"document": "售后制度", "clause": "3.2"}],
        },
        category="overview",
        sentinels=sentinels,
        issued_tokens=[tok],
    )
    clause_r = next(r for r in r_gen if r.name == "clauses")
    assert not clause_r.passed and clause_r.attribution == "生成"

    r_ret = evaluate_chat_step(
        clause_step,
        {"http_status": 200, "answer": "无条款", "trace": [], "citations": []},
        category="policy",
        sentinels=sentinels,
        issued_tokens=[tok],
    )
    clause_r2 = next(r for r in r_ret if r.name == "clauses")
    assert not clause_r2.passed and clause_r2.attribution == "检索"

    # 5. Verdict unknown -> judge branches: ok(match), ok(unclear), invalid, disabled/None
    v_step = ChatStep.model_validate(
        {
            "id": "c5",
            "do": "chat",
            "as": "wang",
            "query": "q",
            "expect": {"should": {"verdict": "refundable"}},
        }
    )
    ambiguous_resp = {
        "http_status": 200,
        "answer": "仅陈述订单事实。",
        "trace": [],
        "citations": [],
    }

    # Judge ok -> refundable
    r_j_ok = evaluate_chat_step(
        v_step,
        ambiguous_resp,
        category="policy",
        sentinels=sentinels,
        issued_tokens=[],
        judge_fn=lambda q, a: JudgeVerdictResult(
            status="ok", label="refundable", evidence="仅陈述"
        ),
    )
    vr = next(r for r in r_j_ok if r.name == "verdict")
    assert vr.passed and vr.verdict_source == "judge" and not vr.needs_manual_review
    assert vr.to_dict()["judge_record"]["label"] == "refundable"

    # Judge ok -> unclear (fails + manual review)
    r_j_unc = evaluate_chat_step(
        v_step,
        ambiguous_resp,
        category="policy",
        sentinels=sentinels,
        issued_tokens=[],
        judge_fn=lambda q, a: JudgeVerdictResult(status="ok", label="unclear", evidence="仅陈述"),
    )
    vr_unc = next(r for r in r_j_unc if r.name == "verdict")
    assert not vr_unc.passed and vr_unc.needs_manual_review
    assert vr_unc.to_dict()["needs_manual_review"] is True

    # Judge invalid -> verdict_source == "judge_invalid"
    r_j_inv = evaluate_chat_step(
        v_step,
        ambiguous_resp,
        category="policy",
        sentinels=sentinels,
        issued_tokens=[],
        judge_fn=lambda q, a: JudgeVerdictResult(status="invalid", label="unknown", error="bad"),
    )
    vr_inv = next(r for r in r_j_inv if r.name == "verdict")
    assert (
        not vr_inv.passed
        and vr_inv.verdict_source == "judge_invalid"
        and vr_inv.needs_manual_review
    )

    # No judge -> verdict_source == "unknown"
    r_no_j = evaluate_chat_step(
        v_step,
        ambiguous_resp,
        category="policy",
        sentinels=sentinels,
        issued_tokens=[],
        judge_fn=None,
    )
    vr_nj = next(r for r in r_no_j if r.name == "verdict")
    assert not vr_nj.passed and vr_nj.verdict_source == "unknown" and vr_nj.judge_record is None

    # 6. Only must block (no should block), no target on deny
    must_only = ChatStep.model_validate(
        {
            "id": "c6",
            "do": "chat",
            "as": "wang",
            "query": "q",
            "authz": "deny",
            "expect": {"must": {"not_contains": ["x"]}},
        }
    )
    r_mo = evaluate_chat_step(
        must_only,
        {"http_status": 200, "answer": "ok", "trace": [], "citations": []},
        category="authz",
        sentinels=sentinels,
        issued_tokens=[],
    )
    assert all(r.passed for r in r_mo)

    # 7. L7-2: conversation_changed=True fails when prev_conversation_id is empty/None
    conv_step = ChatStep.model_validate(
        {
            "id": "c7",
            "do": "chat",
            "as": "zhao",
            "query": "q",
            "expect": {"must": {"conversation_changed": True}},
        }
    )
    r_no_prev = evaluate_chat_step(
        conv_step,
        {"http_status": 200, "conversation_id": "conv-new", "answer": "ok", "trace": []},
        category="overview",
        sentinels=sentinels,
        issued_tokens=[],
        prev_conversation_id=None,
    )
    conv_res = next(r for r in r_no_prev if r.name == "conversation_changed")
    assert not conv_res.passed
    assert conv_res.actual == "前置步骤未取得会话 ID"

    # conversation_changed=False works when prev_conversation_id is empty or matches
    conv_false = ChatStep.model_validate(
        {
            "id": "c8",
            "do": "chat",
            "as": "zhao",
            "query": "q",
            "expect": {"should": {"conversation_changed": False}},
        }
    )
    r_cf = evaluate_chat_step(
        conv_false,
        {"http_status": 200, "conversation_id": "conv-1", "answer": "ok", "trace": []},
        category="overview",
        sentinels=sentinels,
        issued_tokens=[],
        prev_conversation_id="conv-1",
    )
    assert next(r for r in r_cf if r.name == "conversation_changed").passed

    # 8. M11: token_leak via JWT regex, REDACTED_TOKEN fallback, prior_token_leak reuse, & no_jwt
    jwt_step = ChatStep.model_validate(
        {
            "id": "c9",
            "do": "chat",
            "as": "wang",
            "query": "q",
            "expect": {"must": {"no_jwt": True}},
        }
    )
    r_jwt = evaluate_chat_step(
        jwt_step,
        {"http_status": 200, "answer": f"token={tok}", "trace": []},
        category="injection",
        sentinels=sentinels,
        issued_tokens=[],
    )
    assert not next(r for r in r_jwt if r.name == "token_leak").passed
    assert not next(r for r in r_jwt if r.name == "no_jwt").passed

    r_redacted = evaluate_chat_step(
        jwt_step,
        {"http_status": 200, "answer": f"token={REDACTED_TOKEN}", "trace": []},
        category="injection",
        sentinels=sentinels,
        issued_tokens=[],
    )
    assert not next(r for r in r_redacted if r.name == "token_leak").passed
    assert not next(r for r in r_redacted if r.name == "no_jwt").passed

    r_prior_fail = evaluate_chat_step(
        jwt_step,
        {"http_status": 200, "answer": "clean text", "trace": []},
        category="injection",
        sentinels=sentinels,
        issued_tokens=[],
        prior_token_leak={"passed": False, "actual": ["token#1"]},
    )
    tl_prior = next(r for r in r_prior_fail if r.name == "token_leak")
    assert not tl_prior.passed and tl_prior.actual == ["token#1"]

    r_prior_pass = evaluate_chat_step(
        jwt_step,
        {"http_status": 200, "answer": "clean text", "trace": []},
        category="injection",
        sentinels=sentinels,
        issued_tokens=[],
        prior_token_leak={"passed": True, "actual": "未泄露"},
    )
    assert next(r for r in r_prior_pass if r.name == "token_leak").passed

    # M1: _step_level always maps dialogue skipped steps to "should", even with severity="must"
    assert _step_level("tool", None, skipped=True) == "must"
    assert _step_level("guard", None, skipped=True) == "must"
    assert _step_level("dialogue", "must", skipped=True) == "should"
    assert _step_level("dialogue", "must", skipped=False) == "must"
    assert _step_level("dialogue", None, skipped=False) == "should"

    # L10: token_leak failure skips LLM judge entirely; clean response redacts query before judge
    judge_calls: list[tuple[str, str]] = []

    def _spy_judge(q: str, a: str) -> JudgeVerdictResult:
        judge_calls.append((q, a))
        return JudgeVerdictResult(status="ok", label="refundable", evidence=a[:4])

    r_leak_verdict = evaluate_chat_step(
        v_step,
        {"http_status": 200, "answer": f"仅陈述订单事实 {tok}", "trace": [], "citations": []},
        category="policy",
        sentinels=sentinels,
        issued_tokens=[tok],
        judge_fn=_spy_judge,
    )
    assert not next(r for r in r_leak_verdict if r.name == "token_leak").passed
    assert judge_calls == []

    q_with_tok = ChatStep.model_validate(
        {
            "id": "c10",
            "do": "chat",
            "as": "wang",
            "query": f"帮查 {tok}",
            "expect": {"should": {"verdict": "refundable"}},
        }
    )
    r_q_redacted = evaluate_chat_step(
        q_with_tok,
        {"http_status": 200, "answer": "仅陈述订单事实。", "trace": [], "citations": []},
        category="policy",
        sentinels=sentinels,
        issued_tokens=[tok],
        judge_fn=_spy_judge,
    )
    assert next(r for r in r_q_redacted if r.name == "verdict").passed
    assert len(judge_calls) == 1
    assert tok not in judge_calls[0][0]
    assert tok.split(".")[2] not in judge_calls[0][0]
    assert REDACTED_TOKEN in judge_calls[0][0]
