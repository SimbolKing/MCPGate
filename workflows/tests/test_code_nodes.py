from __future__ import annotations

import json
from datetime import date
from typing import Any

import _mcp_common
import build_refund_args
import parse_order
import parse_query
import parse_refund
import pytest
import resolve_context


def _mcp_ok(structured: dict[str, Any]) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
                "structuredContent": structured,
            },
        },
        ensure_ascii=False,
    )


def _mcp_is_error(status: int, resp_body: Any) -> str:
    resp_text = (
        json.dumps(resp_body, ensure_ascii=False) if isinstance(resp_body, dict) else str(resp_body)
    )
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": f"call failed, status: {status}, response: {resp_text}",
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# _mcp_common.py
# ---------------------------------------------------------------------------


class TestMcpCommon:
    def test_http_401_and_non_200(self) -> None:
        r401 = _mcp_common.parse_mcp_response("get_order", 401, "Jwt is missing")
        assert r401["kind"] == "auth_expired"
        assert r401["call_summary"] == {
            "tool": "get_order",
            "kind": "auth_expired",
            "http_status": 401,
        }
        assert _mcp_common.format_error_reply(r401) == "登录已过期或无效，请重新登录后再试。"

        r502 = _mcp_common.parse_mcp_response("get_order", 502, "Bad Gateway")
        assert r502["kind"] == "unavailable"
        assert r502["call_summary"] == {
            "tool": "get_order",
            "kind": "unavailable",
            "http_status": 502,
        }
        assert _mcp_common.format_error_reply(r502) == "售后系统暂时不可用，请稍后再试。"

    def test_invalid_json_non_dict_and_missing_result(self) -> None:
        r_bad_json = _mcp_common.parse_mcp_response("get_order", 200, "not-json")
        assert r_bad_json["kind"] == "unavailable"

        r_non_str = _mcp_common.parse_mcp_response("get_order", 200, None)  # type: ignore[arg-type]
        assert r_non_str["kind"] == "unavailable"

        r_list = _mcp_common.parse_mcp_response("get_order", 200, "[]")
        assert r_list["kind"] == "unavailable"

        r_no_result = _mcp_common.parse_mcp_response(
            "get_order", 200, json.dumps({"jsonrpc": "2.0"})
        )
        assert r_no_result["kind"] == "unavailable"

        r_no_structured = _mcp_common.parse_mcp_response(
            "get_order", 200, json.dumps({"jsonrpc": "2.0", "result": {"isError": False}})
        )
        assert r_no_structured["kind"] == "unavailable"

    def test_jsonrpc_error_internal(self) -> None:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "unknown tool"}}
        )
        r = _mcp_common.parse_mcp_response("unknown_tool", 200, body)
        assert r["kind"] == "internal"
        assert _mcp_common.format_error_reply(r) == "助手与网关的工具清单不一致，请联系管理员。"

    def test_ok_response(self) -> None:
        r = _mcp_common.parse_mcp_response("get_order", 200, _mcp_ok({"order_id": "A1023"}))
        assert r["kind"] == "ok"
        assert r["data"] == {"order_id": "A1023"}
        assert r["call_summary"] == {"tool": "get_order", "kind": "ok", "http_status": 200}

    def test_is_error_variants(self) -> None:
        # 403 order_access_denied
        r403 = _mcp_common.parse_mcp_response(
            "get_order",
            200,
            _mcp_is_error(
                403, {"code": "order_access_denied", "message": "无权访问", "request_id": "req-1"}
            ),
        )
        assert r403["kind"] == "denied"
        assert r403["call_summary"] == {
            "tool": "get_order",
            "kind": "denied",
            "http_status": 403,
            "error_code": "order_access_denied",
            "request_id": "req-1",
        }
        assert _mcp_common.format_error_reply(r403) == "你没有权限查看该订单。"

        # 403 other codes + fallback
        for code, expected in [
            ("refund_access_denied", "你没有权限查看该退款单。"),
            ("approval_access_denied", "你没有权限查看该退款申请。"),
            ("insufficient_role", "该功能仅主管可用。"),
            ("invalid_claims", "当前账号的角色或区域信息无效，请联系管理员。"),
            ("other_403", "你没有权限执行该操作。"),
        ]:
            parsed = _mcp_common.parse_mcp_response("t", 200, _mcp_is_error(403, {"code": code}))
            assert _mcp_common.format_error_reply(parsed) == expected

        # 401 inside isError
        r401_inner = _mcp_common.parse_mcp_response(
            "get_order", 200, _mcp_is_error(401, {"code": "token_expired", "message": "已过期"})
        )
        assert r401_inner["kind"] == "auth_expired"

        # 404 variants with and without target_id
        for code, tid, expected in [
            ("order_not_found", "X9999", "没有找到订单 X9999，请核对单号。"),
            ("order_not_found", "", "没有找到该订单，请核对单号。"),
            ("refund_not_found", "R999", "没有找到退款单 R999，请核对单号。"),
            ("refund_not_found", "", "没有找到该退款单，请核对单号。"),
            ("approval_not_found", "AP999", "没有找到审批单 AP999，请核对单号。"),
            ("approval_not_found", "", "没有找到该审批单，请核对单号。"),
            ("unknown_404", "", "没有找到对应记录，请核对单号。"),
        ]:
            parsed = _mcp_common.parse_mcp_response("t", 200, _mcp_is_error(404, {"code": code}))
            assert _mcp_common.format_error_reply(parsed, target_id=tid) == expected

        # 409 approval_pending_exists and other 409
        r409_dup = _mcp_common.parse_mcp_response(
            "create_refund",
            200,
            _mcp_is_error(
                409,
                {
                    "code": "approval_pending_exists",
                    "message": "订单 A1023 已有待审批申请 AP000001。",
                    "request_id": "req-2",
                },
            ),
        )
        assert r409_dup["kind"] == "conflict"
        assert "AP000001" in _mcp_common.format_error_reply(r409_dup)
        assert "无需重复提交" in _mcp_common.format_error_reply(r409_dup)

        r409_other = _mcp_common.parse_mcp_response(
            "create_refund",
            200,
            _mcp_is_error(409, {"code": "idempotency_conflict", "message": "幂等键冲突。"}),
        )
        assert _mcp_common.format_error_reply(r409_other) == "幂等键冲突。"

        # 422 with details and without details
        r422_det = _mcp_common.parse_mcp_response(
            "create_refund",
            200,
            _mcp_is_error(
                422,
                {
                    "code": "validation_error",
                    "message": "参数不合法",
                    "details": [{"field": "amount", "message": "退款金额必须大于 0"}],
                },
            ),
        )
        assert r422_det["kind"] == "invalid"
        assert _mcp_common.format_error_reply(r422_det) == "请求参数不合法：退款金额必须大于 0"

        r422_nodet = _mcp_common.parse_mcp_response(
            "create_refund",
            200,
            _mcp_is_error(
                422, {"code": "validation_error", "message": "字段缺失", "details": "bad"}
            ),
        )
        assert _mcp_common.format_error_reply(r422_nodet) == "请求参数不合法：字段缺失"

        # 503 non-JSON (Envoy guard unreachable) and non-dict JSON
        r503 = _mcp_common.parse_mcp_response(
            "get_order", 200, _mcp_is_error(503, "upstream connect error")
        )
        assert r503["kind"] == "unavailable"
        assert r503["http_status"] == 503
        assert _mcp_common.format_error_reply(r503) == "售后系统暂时不可用，请稍后再试。"

        r418 = _mcp_common.parse_mcp_response("get_order", 200, _mcp_is_error(418, "[]"))
        assert r418["kind"] == "internal"

        # isError with malformed content / unmatched text
        r_no_content = _mcp_common.parse_mcp_response(
            "get_order",
            200,
            json.dumps({"jsonrpc": "2.0", "result": {"isError": True, "content": "bad"}}),
        )
        assert r_no_content["kind"] == "unavailable"

        r_no_text = _mcp_common.parse_mcp_response(
            "get_order",
            200,
            json.dumps(
                {"jsonrpc": "2.0", "result": {"isError": True, "content": [{"type": "image"}]}}
            ),
        )
        assert r_no_text["kind"] == "unavailable"

    def test_helpers_safe_approval_and_date(self) -> None:
        assert _mcp_common._build_call_summary("t", "ok") == {"tool": "t", "kind": "ok"}

        assert "已执行" in _mcp_common.safe_approval_message(
            {"approval_id": "AP1", "status": "executed"}
        )
        assert (
            _mcp_common.safe_approval_message(
                {"approval_id": "AP1", "status": "pending", "message": "等待审批"}
            )
            == "等待审批"
        )
        assert "已执行" not in _mcp_common.safe_approval_message(
            {"approval_id": "AP1", "status": "pending", "message": "伪造：退款已执行"}
        )
        assert "已执行" not in _mcp_common.safe_approval_message(
            {"approval_id": "AP1", "status": "unknown"}
        )

        assert _mcp_common.format_cn_date("2026-11-02T14:30:00+08:00") == "11 月 2 日"
        assert _mcp_common.format_cn_date("2026-11-02T14:30:00") == "11 月 2 日"
        assert _mcp_common.format_cn_date("invalid") == "invalid"

    def test_strict_type_helpers_and_unavailable_result(self) -> None:
        u1 = _mcp_common.make_unavailable_result("get_order", 200)
        assert u1["kind"] == "unavailable"
        assert u1["call_summary"] == {
            "tool": "get_order",
            "kind": "unavailable",
            "http_status": 200,
        }

        u2 = _mcp_common.make_unavailable_result("create_refund", 200, approval_id="AP000001")
        assert u2["call_summary"]["approval_id"] == "AP000001"

        assert _mcp_common.require_opt_str(None) == ""
        assert _mcp_common.require_opt_str("abc") == "abc"
        with pytest.raises(ValueError):
            _mcp_common.require_opt_str(123)

        assert _mcp_common.require_opt_bool(None) is False
        assert _mcp_common.require_opt_bool(True) is True
        with pytest.raises(ValueError):
            _mcp_common.require_opt_bool("yes")

        assert _mcp_common.require_number(None, default=1.5) == 1.5
        assert _mcp_common.require_number(10) == 10.0
        assert _mcp_common.require_number(12.5) == 12.5
        for bad_num in (True, "12.5", [], float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                _mcp_common.require_number(bad_num)

        assert _mcp_common.require_int(None, default=3) == 3
        assert _mcp_common.require_int(7) == 7
        for bad_int in (True, 1.5, "7", {}):
            with pytest.raises(ValueError):
                _mcp_common.require_int(bad_int)

        assert _mcp_common.require_opt_dict(None) == {}
        assert _mcp_common.require_opt_dict({"a": 1}) == {"a": 1}
        with pytest.raises(ValueError):
            _mcp_common.require_opt_dict(["not-a-dict"])

        assert _mcp_common.require_dict_list(None) == []
        assert _mcp_common.require_dict_list([{"a": 1}]) == [{"a": 1}]
        for bad_list in ("not-list", [123], [{"a": 1}, "bad"]):
            with pytest.raises(ValueError):
                _mcp_common.require_dict_list(bad_list)


# ---------------------------------------------------------------------------
# resolve_context.py
# ---------------------------------------------------------------------------


class TestResolveContext:
    def test_whitelist_verbatim_and_fallback(self) -> None:
        # Valid lowercase in query -> uppercased; fabricated ID rejected; illegal chars rejected
        res = resolve_context.main(
            query="查一下订单 a1023 和客户 C0001",
            order_id="a1023",
            approval_id="AP999999",  # not in query -> dropped
            refund_id="R001;DROP",  # invalid chars -> dropped
            customer_id="C0001",
            amount=100.456,
            reason_note="  屏幕坏了 ",
            standalone_question="数码家电质量问题怎么退",
        )
        assert res["order_id"] == "A1023"
        assert res["amount"] == 100.46
        assert res["reason_note"] == "屏幕坏了"
        assert res["kb_query"] == "数码家电质量问题怎么退"
        assert res["query_tool"] == "list_orders"
        assert json.loads(res["query_args_json"]) == {"page_size": 20, "customer_id": "C0001"}

        # Order fallback from current_order_id + non-string/invalid inputs
        res2 = resolve_context.main(
            query=None,  # type: ignore[arg-type]
            order_id=123,  # type: ignore[arg-type]
            amount=True,
            reason_note=None,  # type: ignore[arg-type]
            list_target=None,  # type: ignore[arg-type]
            order_status=None,  # type: ignore[arg-type]
            refund_status=None,  # type: ignore[arg-type]
            open_ticket_only=None,  # type: ignore[arg-type]
            standalone_question=None,  # type: ignore[arg-type]
            current_order_id="a1024",
        )
        assert res2["order_id"] == "A1024"
        assert res2["amount"] == 0.0
        assert res2["kb_query"] == ""

    def test_amount_edge_cases(self) -> None:
        huge_400_digit_int = int("9" * 400)
        for raw in (
            None,
            False,
            "abc",
            -5,
            0,
            float("nan"),
            float("inf"),
            float("-inf"),
            0.001,
            1e20,
            10_000_000.01,
            huge_400_digit_int,
            "9" * 400,
        ):
            assert resolve_context._clean_amount(raw) == 0.0
        assert resolve_context._clean_amount("299.5") == 299.5
        assert resolve_context._clean_amount(10_000_000.0) == 10_000_000.0

    def test_query_plan_all_8_rules(self) -> None:
        # Rule 1: turn refund_id
        r1 = resolve_context.main(query="查 R000105", refund_id="R000105", approval_id="AP000001")
        assert r1["query_tool"] == "get_refund"
        assert json.loads(r1["query_args_json"]) == {"refund_id": "R000105"}

        # Rule 2: turn approval_id
        r2 = resolve_context.main(query="AP000001 批了吗", approval_id="AP000001")
        assert r2["query_tool"] == "get_approval"
        assert json.loads(r2["query_args_json"]) == {"approval_id": "AP000001"}

        # Rule 3: list_target == refunds (with & without optional filters)
        r3a = resolve_context.main(query="查退款记录", list_target="refunds")
        assert r3a["query_tool"] == "list_refunds"
        assert json.loads(r3a["query_args_json"]) == {"page_size": 20}

        r3b = resolve_context.main(
            query="查 A1031 成功的退款记录",
            order_id="A1031",
            list_target="refunds",
            refund_status="succeeded",
        )
        assert r3b["query_tool"] == "list_refunds"
        assert json.loads(r3b["query_args_json"]) == {
            "page_size": 20,
            "order_id": "A1031",
            "status": "succeeded",
        }

        # Rule 4: list_orders (all filter combinations)
        r4a = resolve_context.main(query="查订单列表", list_target="orders")
        assert r4a["query_tool"] == "list_orders"
        assert json.loads(r4a["query_args_json"]) == {"page_size": 20}

        r4b = resolve_context.main(
            query="查 C0003 已签收有工单的订单",
            customer_id="C0003",
            order_status="delivered",
            open_ticket_only="yes",
        )
        assert r4b["query_tool"] == "list_orders"
        assert json.loads(r4b["query_args_json"]) == {
            "page_size": 20,
            "customer_id": "C0003",
            "status": "delivered",
            "has_open_ticket": True,
        }

        r4c = resolve_context.main(query="查无工单订单", open_ticket_only="no")
        assert r4c["query_tool"] == "list_orders"
        assert json.loads(r4c["query_args_json"]) == {"page_size": 20, "has_open_ticket": False}

        # Rule 5: turn order_id
        r5 = resolve_context.main(query="A1023 的退款申请", order_id="A1023")
        assert r5["query_tool"] == "list_approvals"
        assert json.loads(r5["query_args_json"]) == {"order_id": "A1023", "page_size": 5}

        # Rule 6: conv last_approval_id
        r6 = resolve_context.main(
            query="那笔批了吗", last_approval_id="AP000001", current_order_id="A1023"
        )
        assert r6["query_tool"] == "get_approval"
        assert json.loads(r6["query_args_json"]) == {"approval_id": "AP000001"}

        # Rule 7: conv current_order_id
        r7 = resolve_context.main(query="申请进度如何", current_order_id="A1023")
        assert r7["query_tool"] == "list_approvals"
        assert json.loads(r7["query_args_json"]) == {"order_id": "A1023", "page_size": 5}

        # Rule 8: none -> empty tool
        r8 = resolve_context.main(query="批了吗")
        assert r8["query_tool"] == ""
        assert r8["query_args_json"] == "{}"
        assert r8["kb_query"] == "批了吗"

        # Every non-empty tool is in read-only whitelist
        for res in (r1, r2, r3a, r3b, r4a, r4b, r4c, r5, r6, r7):
            assert res["query_tool"] in resolve_context.QUERY_TOOL_WHITELIST

    def test_whitelist_guard_downgrades_disallowed_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resolve_context, "QUERY_TOOL_WHITELIST", ("get_approval",))
        res = resolve_context.main(query="查 R000105", refund_id="R000105")
        assert res["query_tool"] == ""
        assert res["query_args_json"] == "{}"


# ---------------------------------------------------------------------------
# parse_order.py
# ---------------------------------------------------------------------------


class TestParseOrder:
    def test_error_passthrough(self) -> None:
        res = parse_order.main(
            status_code=200,
            body=_mcp_is_error(
                403, {"code": "order_access_denied", "message": "无权", "request_id": "r1"}
            ),
            order_id="B2087",
        )
        assert res["ok"] == 0
        assert res["reply"] == "你没有权限查看该订单。"
        assert res["call_summary"]["kind"] == "denied"

    def test_standard_delivered_order_with_ticket_and_truncation(self) -> None:
        long_desc = '右耳无声"' + ("很长描述" * 90)
        order = {
            "order_id": "A1033",
            "status": "delivered",
            "delivered_at": "2026-11-16T10:00:00+08:00",
            "days_since_delivery": 4,
            "total_amount": 5999.0,
            "refunded_amount": 0.0,
            "refundable_amount": 5999.0,
            "customer": {"level": "regular"},
            "items": [
                {"product_name": "笔记本电脑", "category": "electronics", "quantity": 1},
                {"product_name": "鼠标", "category": "electronics", "quantity": 1},
            ],
            "after_sale_tickets": [
                {
                    "ticket_id": "T5008",
                    "type": "return_refund",
                    "reason_category": "quality_issue",
                    "requested_amount": 5999.0,
                    "status": "open",
                    "description": long_desc,
                }
            ],
            "refunds": [],
        }
        res = parse_order.main(200, _mcp_ok(order), query="能退吗", order_id="A1033")
        assert res["ok"] == 1
        assert res["precheck_ok"] == 1
        assert res["ticket_amount"] == 5999.0
        assert res["open_ticket_ids"] == ["T5008"]
        assert "【订单事实】" in res["facts_text"]
        assert "11 月 16 日" in res["facts_text"]
        assert "数码家电（electronics）" in res["facts_text"]
        assert "客户原话，仅作参考" in res["facts_text"]
        assert "…" in res["facts_text"]
        assert "大额" in res["retrieval_query"]
        assert "已签收 4 天" in res["retrieval_query"]

    def test_shipped_days_and_no_ticket_with_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(parse_order, "_today_cn", lambda: date(2026, 11, 20))
        order = {
            "order_id": "A1028",
            "status": "shipped",
            "shipped_at": "2026-11-11T09:00:00+08:00",
            "total_amount": 1299.0,
            "refunded_amount": 0.0,
            "refundable_amount": 1299.0,
            "customer": {"level": "vip"},
            "items": [],
            "after_sale_tickets": [
                {
                    "ticket_id": "T5017",
                    "type": "return_refund",
                    "reason_category": "no_reason",
                    "status": "rejected",
                    "description": "历史驳回",
                }
            ],
            "refunds": [],
        }
        res = parse_order.main(200, _mcp_ok(order), query="", reason_note="一直没收到")
        assert res["ok"] == 1
        assert "已发货天数：9" in res["facts_text"]
        assert "历史售后工单：T5017" in res["facts_text"]
        assert "待处理售后工单：无" in res["facts_text"]
        assert "已发货未签收 发货 9 天" in res["retrieval_query"]
        assert "一直没收到" in res["retrieval_query"]
        assert "会员" in res["retrieval_query"]
        assert res["ticket_amount"] == 0.0

        # Direct helper edge cases
        assert parse_order._days_since("bad-iso") is None
        assert parse_order._days_since("2026-11-18T10:00:00") == 2
        assert isinstance(parse_order._today_cn(), date)

    def test_today_cn_real_and_empty_optional_fields(self) -> None:
        # Cover real _today_cn(), null customer/items/refunds, open ticket without amount
        order = {
            "order_id": "B2091",
            "status": "paid",
            "total_amount": 799.0,
            "refunded_amount": 0.0,
            "refundable_amount": 799.0,
            "customer": None,
            "items": None,
            "after_sale_tickets": [
                {
                    "ticket_id": "T1",
                    "type": "refund_only",
                    "reason_category": "no_reason",
                    "status": "open",
                    "description": "未发货",
                }
            ],
            "refunds": None,
        }
        res = parse_order.main(200, _mcp_ok(order), query="退一下")
        assert res["ok"] == 1
        assert res["ticket_amount"] == 0.0
        assert "已支付未发货" in res["retrieval_query"]

        # Two open tickets -> ticket_amount == 0
        order["after_sale_tickets"] = [
            {
                "ticket_id": "T1",
                "type": "refund_only",
                "reason_category": "other",
                "requested_amount": 10,
                "status": "open",
                "description": "a",
            },
            {
                "ticket_id": "T2",
                "type": "refund_only",
                "reason_category": "other",
                "requested_amount": 20,
                "status": "open",
                "description": "b",
            },
        ]
        res2 = parse_order.main(200, _mcp_ok(order))
        assert res2["ticket_amount"] == 0.0
        assert res2["open_ticket_ids"] == ["T1", "T2"]

        # Cover real _today_cn() with shipped_at
        order["status"] = "shipped"
        order["shipped_at"] = "2026-01-01T00:00:00+08:00"
        order["after_sale_tickets"] = []
        res3 = parse_order.main(200, _mcp_ok(order))
        assert "已发货天数" in res3["facts_text"]

    def test_precheck_three_rules_in_order(self) -> None:
        # 1. pending_payment -> 5.1
        o1 = {
            "order_id": "A1029",
            "status": "pending_payment",
            "total_amount": 89.0,
            "refunded_amount": 0.0,
            "refundable_amount": 0.0,
            "customer": {"level": "regular"},
            "items": [{"product_name": "箱子", "category": "home", "quantity": 1}],
            "after_sale_tickets": [],
            "refunds": [],
        }
        r1 = parse_order.main(200, _mcp_ok(o1))
        assert r1["precheck_ok"] == 0
        assert "待支付" in r1["precheck_reply"] and "5.1" in r1["precheck_reply"]

        # 2. processing refund (even when refundable_amount == 0, rule 2 beats rule 3!)
        o2 = {
            "order_id": "A1035",
            "status": "delivered",
            "days_since_delivery": 8,
            "total_amount": 699.0,
            "refunded_amount": 0.0,
            "refundable_amount": 0.0,
            "customer": {"level": "regular"},
            "items": [{"product_name": "手表", "category": "electronics", "quantity": 1}],
            "after_sale_tickets": [],
            "refunds": [
                {
                    "refund_id": "R000103",
                    "amount": 699.0,
                    "status": "processing",
                    "reason_category": "quality_issue",
                }
            ],
        }
        r2 = parse_order.main(200, _mcp_ok(o2))
        assert r2["precheck_ok"] == 0
        assert "R000103" in r2["precheck_reply"] and "5.4" in r2["precheck_reply"]
        assert "已有退款" in r2["retrieval_query"]

        # 3. refundable_amount == 0 (already fully refunded)
        o3 = {
            "order_id": "A1034",
            "status": "completed",
            "days_since_delivery": 40,
            "total_amount": 199.0,
            "refunded_amount": 199.0,
            "refundable_amount": 0.0,
            "customer": {"level": "regular"},
            "items": [{"product_name": "充电宝", "category": "electronics", "quantity": 1}],
            "after_sale_tickets": [],
            "refunds": [
                {
                    "refund_id": "R000102",
                    "amount": 199.0,
                    "status": "succeeded",
                    "reason_category": "quality_issue",
                }
            ],
        }
        r3 = parse_order.main(200, _mcp_ok(o3))
        assert r3["precheck_ok"] == 0
        assert "已全额退款" in r3["precheck_reply"] and "5.4" in r3["precheck_reply"]

    def test_malformed_structured_content_returns_unavailable(self) -> None:
        base_order: dict[str, Any] = {
            "order_id": "A1023",
            "status": "delivered",
            "total_amount": 299.0,
            "refunded_amount": 0.0,
            "refundable_amount": 299.0,
            "customer": {"level": "regular"},
            "items": [{"product_name": "耳机", "category": "electronics", "quantity": 1}],
            "after_sale_tickets": [],
            "refunds": [],
        }
        malformed_cases: list[dict[str, Any]] = [
            {**base_order, "total_amount": "not-a-number"},
            {**base_order, "refundable_amount": True},
            {**base_order, "days_since_delivery": "4"},
            {**base_order, "customer": "vip"},
            {**base_order, "items": ["not-an-object"]},
            {
                **base_order,
                "items": [{"product_name": "耳机", "category": "electronics", "quantity": "1"}],
            },
            {**base_order, "after_sale_tickets": ["bad-ticket"]},
            {
                **base_order,
                "after_sale_tickets": [
                    {
                        "ticket_id": "T1",
                        "type": "return_refund",
                        "reason_category": "quality_issue",
                        "requested_amount": "bad-amt",
                        "status": "open",
                        "description": "坏了",
                    }
                ],
            },
            {**base_order, "refunds": ["bad-refund"]},
            {
                **base_order,
                "refunds": [
                    {
                        "refund_id": "R1",
                        "amount": "bad-amt",
                        "status": "processing",
                        "reason_category": "quality_issue",
                    }
                ],
            },
        ]
        for payload in malformed_cases:
            res = parse_order.main(200, _mcp_ok(payload), order_id="A1023")
            assert res["ok"] == 0
            assert res["call_summary"] == {
                "tool": "get_order",
                "kind": "unavailable",
                "http_status": 200,
            }
            assert res["reply"] == "售后系统暂时不可用，请稍后再试。"

        # Open ticket with zero requested_amount exercises parsed_req <= 0 branch
        zero_req_order = {
            **base_order,
            "after_sale_tickets": [
                {
                    "ticket_id": "T1",
                    "type": "return_refund",
                    "reason_category": "quality_issue",
                    "requested_amount": 0,
                    "status": "open",
                    "description": "测试",
                }
            ],
        }
        res_zero = parse_order.main(200, _mcp_ok(zero_req_order), order_id="A1023")
        assert res_zero["ok"] == 1
        assert res_zero["ticket_amount"] == 0.0


# ---------------------------------------------------------------------------
# build_refund_args.py
# ---------------------------------------------------------------------------


class TestBuildRefundArgs:
    CHUNKS = [
        {"content": "售后制度 第 3.2 条 质量问题退货期限\n规则：30 天内可退。"},
        {"content": "售后制度 第 5.5 条 运费承担\n规则：商家承担。"},
        {"content": "无效无编号分段"},
        {"content": ""},
        "non-dict",
    ]

    def test_unparseable_decision(self) -> None:
        r = build_refund_args.main(
            structured_output="not json", llm_text="[1, 2, 3]", order_id="A1023"
        )
        assert r["go"] == 0
        assert "7.5" in r["reply"]

        r2 = build_refund_args.main(structured_output=None, llm_text="", order_id="A1023")
        assert r2["go"] == 0

    def test_fenced_json_fallback_and_non_compliant(self) -> None:
        fenced = (
            '```json\n{"compliant": false, "clause_refs": ["3.2"], "explanation": "超过期限"}\n```'
        )
        r = build_refund_args.main(
            structured_output=None,
            llm_text=fenced,
            order_id="A1025",
            retrieved_chunks=self.CHUNKS,  # type: ignore[arg-type]
        )
        assert r["go"] == 0
        assert "超过期限" in r["reply"] and "3.2" in r["reply"] and "7.5" in r["reply"]

        # Non-compliant without valid refs
        r2 = build_refund_args.main(
            structured_output={"compliant": False, "clause_refs": "bad", "explanation": "不支持"},
            order_id="A1025",
            retrieved_chunks=None,
        )
        assert r2["go"] == 0 and "依据：" not in r2["reply"]

    def test_fabricated_clauses_rejected(self) -> None:
        r = build_refund_args.main(
            structured_output={
                "compliant": True,
                "clause_refs": ["9.9", "abc"],
                "reason_category": "quality_issue",
            },
            order_id="A1023",
            refundable_amount=299.0,
            retrieved_chunks=self.CHUNKS,  # type: ignore[arg-type]
        )
        assert r["go"] == 0
        assert "未能在制度中找到" in r["reply"]

    def test_amount_priority_and_limits_and_security(self) -> None:
        decision = {
            "compliant": True,
            "clause_refs": ["3.2", "3.2", "9.9", "5.5"],
            "reason_category": "quality_issue",
            "ticket_id": "T5001",
            "order_id": "B2087",  # must be ignored!
            "amount": 999999,  # must be ignored!
            "reason": "右耳无声属质量问题",
        }
        # 1) user_amount > 0 wins
        r1 = build_refund_args.main(
            structured_output=decision,
            order_id="a1023",
            user_amount=100.0,
            refundable_amount=299.0,
            open_ticket_ids=["T5001"],
            ticket_amount=299.0,
            retrieved_chunks=self.CHUNKS,  # type: ignore[arg-type]
        )
        assert r1["go"] == 1
        args1 = json.loads(r1["arguments_json"])
        assert args1["order_id"] == "A1023"
        assert args1["amount"] == 100.0
        assert args1["ticket_id"] == "T5001"
        assert "idempotency_key" not in args1
        assert (
            "3.2" in args1["reason"] and "5.5" in args1["reason"] and "9.9" not in args1["reason"]
        )

        # 2) ticket_amount > 0 when user_amount == 0
        r2 = build_refund_args.main(
            structured_output={**decision, "ticket_id": "T_FAKE", "reason_category": "bad_cat"},
            order_id="A1023",
            user_amount=0.0,
            refundable_amount=299.0,
            open_ticket_ids=["T5001"],
            ticket_amount=299.0,
            retrieved_chunks=self.CHUNKS,  # type: ignore[arg-type]
        )
        assert r2["go"] == 1
        args2 = json.loads(r2["arguments_json"])
        assert args2["amount"] == 299.0
        assert args2["reason_category"] == "other"
        assert "ticket_id" not in args2

        # 3) fallback to refundable_amount + large refund (>=2000) + reason truncation <= 200
        r3 = build_refund_args.main(
            structured_output={**decision, "reason": "详" * 250},
            order_id="A1033",
            user_amount=0.0,
            refundable_amount=5999.0,
            open_ticket_ids=None,
            ticket_amount=0.0,
            retrieved_chunks=self.CHUNKS,  # type: ignore[arg-type]
        )
        assert r3["go"] == 1
        args3 = json.loads(r3["arguments_json"])
        assert args3["amount"] == 5999.0
        assert len(args3["reason"]) <= 200
        assert "7.3" in args3["reason"]

        # 4) amount exceeds refundable_amount -> rejected
        r4 = build_refund_args.main(
            structured_output=decision,
            order_id="A1031",
            user_amount=350.0,
            refundable_amount=300.0,
            retrieved_chunks=self.CHUNKS,  # type: ignore[arg-type]
        )
        assert r4["go"] == 0
        assert "350.00" in r4["reply"] and "300.00" in r4["reply"]

        # 5) _compose_reason edge cases: empty raw_reason and ultra-long basis
        assert (
            build_refund_args._compose_reason("", "《售后制度》第 3.2 条", 100.0)
            == "依据《售后制度》第 3.2 条"
        )
        huge_basis = "《售后制度》第 3.2 条" * 30
        assert len(build_refund_args._compose_reason("说明", huge_basis, 100.0)) == 200


# ---------------------------------------------------------------------------
# parse_refund.py & parse_query.py
# ---------------------------------------------------------------------------


class TestParseRefundAndQuery:
    def test_parse_refund_error_pending_and_executed(self) -> None:
        # Conflict 409
        r_err = parse_refund.main(
            200,
            _mcp_is_error(
                409,
                {
                    "code": "approval_pending_exists",
                    "message": "订单 A1023 已有待审批申请 AP000001。",
                },
            ),
            order_id="A1023",
        )
        assert r_err["ok"] == 0
        assert r_err["call_summary"]["kind"] == "conflict"
        assert "AP000001" in r_err["reply"]

        # Pending approval (must NEVER contain "已执行")
        r_pend = parse_refund.main(
            200,
            _mcp_ok(
                {
                    "approval_id": "AP000001",
                    "status": "pending",
                    "message": "已提交退款申请 AP000001，等待主管审批。退款尚未执行。",
                    "order_id": "A1023",
                    "amount": 299.0,
                    "reason_category": "quality_issue",
                }
            ),
            basis="《售后制度》第 3.2 条",
        )
        assert r_pend["ok"] == 1
        assert r_pend["call_summary"]["kind"] == "pending"
        assert r_pend["call_summary"]["approval_id"] == "AP000001"
        assert r_pend["approval_id"] == "AP000001"
        assert "等待主管审批" in r_pend["reply"]
        assert "AP000001 批了吗" in r_pend["reply"]
        assert "已执行" not in r_pend["reply"]

        # Executed status (only case allowed to contain "已执行"), no basis, no approval_id
        r_exec = parse_refund.main(
            200,
            _mcp_ok(
                {
                    "approval_id": "",
                    "status": "executed",
                    "message": "主管已同意退款申请，退款已执行。",
                    "order_id": "A1023",
                    "amount": 299.0,
                    "reason_category": "quality_issue",
                }
            ),
            basis="",
        )
        assert r_exec["ok"] == 1
        assert r_exec["call_summary"]["kind"] == "ok"
        assert "已执行" in r_exec["reply"]

    def test_parse_refund_preserves_approval_id_on_malformed_fields(self) -> None:
        # Valid non-empty approval_id must be preserved even if remaining fields are malformed
        r_bad_amt = parse_refund.main(
            200,
            _mcp_ok(
                {
                    "approval_id": "AP000009",
                    "status": "pending",
                    "order_id": "A1023",
                    "amount": "not-a-number",
                    "reason_category": "quality_issue",
                }
            ),
            order_id="A1023",
        )
        assert r_bad_amt["ok"] == 0
        assert r_bad_amt["approval_id"] == "AP000009"
        assert r_bad_amt["call_summary"] == {
            "tool": "create_refund",
            "kind": "unavailable",
            "http_status": 200,
            "approval_id": "AP000009",
        }
        assert r_bad_amt["reply"] == "售后系统暂时不可用，请稍后再试。"

        # Non-string approval_id returns unavailable with empty approval_id
        r_bad_aid = parse_refund.main(
            200,
            _mcp_ok(
                {
                    "approval_id": 12345,
                    "status": "pending",
                    "order_id": "A1023",
                    "amount": 299.0,
                }
            ),
            order_id="A1023",
        )
        assert r_bad_aid["ok"] == 0
        assert r_bad_aid["approval_id"] == ""
        assert r_bad_aid["call_summary"] == {
            "tool": "create_refund",
            "kind": "unavailable",
            "http_status": 200,
        }
        assert r_bad_aid["reply"] == "售后系统暂时不可用，请稍后再试。"

    def test_parse_query_all_tools_and_errors(self) -> None:
        # Error with bad query_args_json and non-dict json
        r_err1 = parse_query.main(
            200, _mcp_is_error(404, {"code": "order_not_found"}), "list_approvals", "bad-json"
        )
        assert r_err1["ok"] == 0 and "没有找到该订单" in r_err1["reply"]

        r_err2 = parse_query.main(
            200, _mcp_is_error(404, {"code": "refund_not_found"}), "get_refund", "[]"
        )
        assert r_err2["ok"] == 0 and "没有找到该退款单" in r_err2["reply"]

        r_err3 = parse_query.main(
            200,
            _mcp_is_error(404, {"code": "approval_not_found"}),
            "get_approval",
            json.dumps({"approval_id": "AP999"}),
        )
        assert r_err3["ok"] == 0 and "AP999" in r_err3["reply"]

        # get_approval: pending (even with adversarial "已执行" in message/comment!)
        r_ga_p = parse_query.main(
            200,
            _mcp_ok(
                {
                    "approval_id": "AP000001",
                    "status": "pending",
                    "message": "错误文案：退款已执行",
                    "order_id": "A1023",
                    "amount": 299.0,
                    "applicant_name": "王磊",
                    "decided_by_name": "李娜",
                    "decision_comment": "已执行测试",
                }
            ),
            "get_approval",
        )
        assert r_ga_p["ok"] == 1
        assert r_ga_p["call_summary"]["approval_id"] == "AP000001"
        assert "已执行" not in r_ga_p["reply"]

        # get_approval: executed (allowed to say "已执行"), no approval_id/decider/comment
        r_ga_e = parse_query.main(
            200,
            _mcp_ok(
                {
                    "status": "executed",
                    "message": "主管已同意退款申请 AP000001，退款已执行，退款单号 R000105。",
                    "order_id": "A1023",
                    "amount": 299.0,
                    "applicant_sub": "u1001",
                }
            ),
            "get_approval",
        )
        assert "退款已执行" in r_ga_e["reply"] and "R000105" in r_ga_e["reply"]

        # list_approvals: empty with & without order_id, non-empty paginated & single-page
        r_la0 = parse_query.main(
            200, _mcp_ok({"items": [], "total": 0}), "list_approvals", '{"order_id":"A1023"}'
        )
        assert r_la0["reply"] == "订单 A1023 没有退款申请记录。"
        r_la0b = parse_query.main(200, _mcp_ok({"items": [], "total": 0}), "list_approvals", "{}")
        assert r_la0b["reply"] == "没有符合条件的退款申请记录。"
        r_la1 = parse_query.main(
            200,
            _mcp_ok(
                {
                    "items": [
                        {
                            "approval_id": "AP1",
                            "amount": 100,
                            "status": "pending",
                            "message": "等待审批",
                        }
                    ],
                    "total": 8,
                }
            ),
            "list_approvals",
        )
        assert "仅显示前 1 条" in r_la1["reply"] and "AP1" in r_la1["reply"]
        r_la2 = parse_query.main(
            200,
            _mcp_ok(
                {
                    "items": [
                        {
                            "approval_id": "AP1",
                            "amount": 100,
                            "status": "rejected",
                            "message": "已拒绝",
                        }
                    ],
                    "total": 1,
                }
            ),
            "list_approvals",
        )
        assert "仅显示前" not in r_la2["reply"]

        # get_refund
        r_gr = parse_query.main(
            200,
            _mcp_ok(
                {
                    "refund_id": "R000105",
                    "order_id": "A1023",
                    "amount": 299.0,
                    "status": "succeeded",
                    "created_at": "2026-11-20T10:10:00+08:00",
                }
            ),
            "get_refund",
        )
        assert "R000105" in r_gr["reply"] and "审批主管" in r_gr["reply"]

        # list_refunds: empty, paginated, full
        assert (
            "负责区域"
            in parse_query.main(200, _mcp_ok({"items": [], "total": 0}), "list_refunds")["reply"]
        )
        r_lr1 = parse_query.main(
            200,
            _mcp_ok(
                {
                    "items": [
                        {"refund_id": "R1", "order_id": "A1", "amount": 50, "status": "succeeded"}
                    ],
                    "total": 25,
                }
            ),
            "list_refunds",
        )
        assert "仅显示前 1 条" in r_lr1["reply"] and "R1" in r_lr1["reply"]
        r_lr2 = parse_query.main(
            200,
            _mcp_ok(
                {
                    "items": [
                        {"refund_id": "R1", "order_id": "A1", "amount": 50, "status": "processing"}
                    ],
                    "total": 1,
                }
            ),
            "list_refunds",
        )
        assert "仅显示前" not in r_lr2["reply"]

        # list_orders: empty, paginated, full
        assert (
            "负责区域"
            in parse_query.main(200, _mcp_ok({"items": [], "total": 0}), "list_orders")["reply"]
        )
        r_lo1 = parse_query.main(
            200,
            _mcp_ok(
                {
                    "items": [
                        {
                            "order_id": "A1023",
                            "status": "delivered",
                            "days_since_delivery": 18,
                            "total_amount": 299.0,
                            "has_open_ticket": True,
                        },
                        {
                            "order_id": "A1029",
                            "status": "pending_payment",
                            "total_amount": 89.0,
                            "has_open_ticket": False,
                        },
                    ],
                    "total": 30,
                }
            ),
            "list_orders",
        )
        assert (
            "仅显示前 2 条" in r_lo1["reply"]
            and "已签收 18 天" in r_lo1["reply"]
            and "未签收" in r_lo1["reply"]
        )
        r_lo2 = parse_query.main(
            200,
            _mcp_ok(
                {
                    "items": [
                        {
                            "order_id": "A1023",
                            "status": "delivered",
                            "days_since_delivery": 18,
                            "total_amount": 299.0,
                            "has_open_ticket": True,
                        }
                    ],
                    "total": 1,
                }
            ),
            "list_orders",
        )
        assert "仅显示前" not in r_lo2["reply"]

    def test_parse_query_malformed_structured_content_returns_unavailable(self) -> None:
        malformed_cases: list[tuple[str, dict[str, Any]]] = [
            ("get_approval", {"approval_id": "AP1", "amount": "bad", "status": "pending"}),
            ("list_approvals", {"items": ["not-an-object"], "total": 1}),
            ("list_approvals", {"items": [{"approval_id": "AP1", "amount": True}], "total": 1}),
            ("list_approvals", {"items": [], "total": "not-int"}),
            ("get_refund", {"refund_id": "R1", "order_id": "A1", "amount": "bad"}),
            ("list_refunds", {"items": "not-a-list", "total": 1}),
            ("list_refunds", {"items": [{"refund_id": "R1", "amount": []}], "total": 1}),
            ("list_orders", {"items": ["not-an-object"], "total": 1}),
            (
                "list_orders",
                {
                    "items": [{"order_id": "A1", "total_amount": 100, "has_open_ticket": "yes"}],
                    "total": 1,
                },
            ),
            (
                "list_orders",
                {
                    "items": [{"order_id": "A1", "total_amount": 100, "days_since_delivery": "3"}],
                    "total": 1,
                },
            ),
        ]
        for tool, payload in malformed_cases:
            res = parse_query.main(200, _mcp_ok(payload), tool)
            assert res["ok"] == 0
            assert res["call_summary"] == {
                "tool": tool,
                "kind": "unavailable",
                "http_status": 200,
            }
            assert res["reply"] == "售后系统暂时不可用，请稍后再试。"
