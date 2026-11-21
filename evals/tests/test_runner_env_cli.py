"""Tests for runner, environment, and CLI using `httpx.MockTransport` (5.13)."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from harness.__main__ import (
    EXIT_ENV_ERROR,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_USAGE_OR_LINT,
    _resolve_resume_scope,
    _verdict_to_exit_code,
)
from harness.__main__ import main as cli_main
from harness.asserts import REDACTED_TOKEN
from harness.clients import ChatClient, GuardClient, McpClient, normalize_mcp_response
from harness.config import EVALS_DIR, REPO_ROOT, Settings
from harness.environment import EnvironmentManager, EnvironmentResetError
from harness.judge import VerdictJudge
from harness.loader import load_cases, load_suite, load_tools_manifest
from harness.runner import (
    EvalRunner,
    _build_step_ledger,
    _is_valid_saved_assertions,
    compute_eval_fingerprint,
    interpolate_value,
    rescore_run_dir,
)
from harness.schema import McpStep
from harness.tokens import StaticTokenProvider

FAKE_TOKENS = {
    "wang": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.wang_signature_0123456789abcdef",
    "zhao": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMiJ9.zhao_signature_0123456789abcdef",
    "chen": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMyJ9.chen_signature_0123456789abcdef",
    "li": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MjAwMSJ9.li_signature_0123456789abcdef",
    "zhou": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MjAwMiJ9.zhou_signature_0123456789abcdef",
    "sun": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MjAwMyJ9.sun_signature_0123456789abcdef",
}

TOKEN_TO_USER = {v: k for k, v in FAKE_TOKENS.items()}


class FakeBackend:
    """In-process stateful mock of Higress MCP + guard + chat driven by httpx.MockTransport."""

    def __init__(self, suite: Any) -> None:
        self.suite = suite
        self.audit_events: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.refunds: list[dict[str, Any]] = []
        self.next_audit_id = 0
        self.next_approval_num = 0
        self.chat_502_remaining = 0
        self.omit_approval_id_in_chat = False
        self.omit_approval_id_in_mcp = False
        self.probe_refunds_mode = "ok"  # "ok" | "503" | "missing_items" | "paged" | "short_page"
        self.probe_refunds_calls = 0
        self.leak_token_in_chat = False
        self.chat_unknown_verdict = False
        self.d02_skip_submit_remaining = 0
        self.d20_skip_submit = False
        self.direct_execute_refund = False
        self.reset_state()

    def reset_state(self) -> None:
        self.approvals = {}
        self.refunds = [
            {
                "refund_id": "R000101",
                "order_id": "A1031",
                "amount": 158.0,
                "created_by": "u1001",
                "created_by_role": "agent",
            },
            {
                "refund_id": "R000102",
                "order_id": "A1034",
                "amount": 199.0,
                "created_by": "u1001",
                "created_by_role": "agent",
            },
            {
                "refund_id": "R000103",
                "order_id": "A1035",
                "amount": 699.0,
                "created_by": "u1001",
                "created_by_role": "agent",
            },
            {
                "refund_id": "R000104",
                "order_id": "C3055",
                "amount": 100.0,
                "created_by": "u1003",
                "created_by_role": "agent",
            },
        ]
        self.next_approval_num = 0
        self.probe_refunds_calls = 0

    def _add_audit(self, actor: str, **fields: Any) -> dict[str, Any]:
        self.next_audit_id += 1
        acc = self.suite.accounts[actor]
        ev = {
            "id": self.next_audit_id,
            "request_id": fields.pop("request_id", f"req-{self.next_audit_id}"),
            "actor_sub": acc.sub,
            "actor_role": acc.role,
            "actor_regions": acc.regions_csv,
            **fields,
        }
        self.audit_events.append(ev)
        return ev

    def _actor_from_auth(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:]
        return TOKEN_TO_USER.get(tok)

    def mcp_handler(self, request: httpx.Request) -> httpx.Response:
        actor = self._actor_from_auth(request)
        if actor is None:
            return httpx.Response(401, text="Jwt is missing or invalid")
        body = json.loads(request.content)
        tool = body["params"]["name"]
        args = body["params"]["arguments"]

        if tool == "list_refunds":
            self.probe_refunds_calls += 1
            # Let initial reset call succeed (call 1), then apply failure mode on invariant probe
            if self.probe_refunds_calls > 1:
                if self.probe_refunds_mode == "503":
                    return httpx.Response(503, text="Service Unavailable")
                if self.probe_refunds_mode == "missing_items":
                    self._add_audit(
                        actor,
                        event_type="tool_call",
                        channel="agent",
                        tool="list_refunds",
                        http_method="GET",
                        http_path="/api/v1/refunds",
                        outcome="success",
                        status_code=200,
                    )
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {"isError": False, "structuredContent": {"total": 4}},
                        },
                    )
                if self.probe_refunds_mode in ("paged", "short_page"):
                    page = int(args.get("page", 1))
                    ps = int(args.get("page_size", 2))
                    start = (page - 1) * ps
                    chunk = (
                        []
                        if (self.probe_refunds_mode == "short_page" and page > 1)
                        else list(self.refunds)[start : start + ps]
                    )
                    self._add_audit(
                        actor,
                        event_type="tool_call",
                        channel="agent",
                        tool="list_refunds",
                        http_method="GET",
                        http_path="/api/v1/refunds",
                        outcome="success",
                        status_code=200,
                    )
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {
                                "isError": False,
                                "structuredContent": {
                                    "items": chunk,
                                    "total": len(self.refunds),
                                    "page": page,
                                    "page_size": ps,
                                },
                            },
                        },
                    )
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="list_refunds",
                http_method="GET",
                http_path="/api/v1/refunds",
                outcome="success",
                status_code=200,
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "isError": False,
                        "structuredContent": {
                            "items": list(self.refunds),
                            "total": len(self.refunds),
                        },
                    },
                },
            )

        if tool == "list_approvals":
            oid = args.get("order_id")
            items = [a for a in self.approvals.values() if not oid or a["order_id"] == oid]
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="list_approvals",
                http_method="GET",
                http_path="/guard/v1/approvals",
                outcome="success",
                status_code=200,
                order_id=oid,
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "isError": False,
                        "structuredContent": {"items": items, "total": len(items)},
                    },
                },
            )

        if tool == "get_order":
            oid = args["order_id"]
            region = "east" if oid.startswith("A") else "north" if oid.startswith("B") else "south"
            if region not in self.suite.accounts[actor].regions:
                rid = f"req-deny-{self.next_audit_id + 1}"
                self._add_audit(
                    actor,
                    request_id=rid,
                    event_type="tool_call",
                    channel="agent",
                    tool="get_order",
                    http_method="GET",
                    http_path=f"/api/v1/orders/{oid}",
                    outcome="access_denied",
                    status_code=403,
                    error_code="order_access_denied",
                    order_id=oid,
                )
                err = json.dumps(
                    {
                        "code": "order_access_denied",
                        "message": f"无权访问订单 {oid}。",
                        "request_id": rid,
                    },
                    ensure_ascii=False,
                )
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"call failed, status: 403, response: {err}",
                                }
                            ],
                        },
                    },
                )
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path=f"/api/v1/orders/{oid}",
                outcome="success",
                status_code=200,
                order_id=oid,
            )
            ref_amt = sum(r["amount"] for r in self.refunds if r["order_id"] == oid)
            ref_list = [r for r in self.refunds if r["order_id"] == oid]
            t_status = "resolved" if ref_list else "open"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "isError": False,
                        "structuredContent": {
                            "order_id": oid,
                            "region": region,
                            "refunded_amount": ref_amt,
                            "refund_status": "full" if ref_amt >= 899 else "none",
                            "refunds": ref_list,
                            "after_sale_tickets": [{"ticket_id": "T5002", "status": t_status}],
                        },
                    },
                },
            )

        if tool == "create_refund":
            oid = args["order_id"]
            if self.direct_execute_refund:
                rf_id = f"R000{105 + len(self.refunds)}"
                rf = {
                    "refund_id": rf_id,
                    "order_id": oid,
                    "amount": args["amount"],
                    "created_by": self.suite.accounts[actor].sub,
                    "created_by_role": self.suite.accounts[actor].role,
                }
                self.refunds.append(rf)
                self._add_audit(
                    actor,
                    event_type="tool_call",
                    channel="agent",
                    tool="create_refund",
                    http_method="POST",
                    http_path="/api/v1/refunds",
                    outcome="success",
                    status_code=201,
                    order_id=oid,
                )
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"isError": False, "structuredContent": rf},
                    },
                )
            self.next_approval_num += 1
            aid = f"AP{self.next_approval_num:06d}"
            ap = {
                "approval_id": aid,
                "status": "pending",
                "message": f"已提交退款申请 {aid}，等待主管审批。退款尚未执行。",
                "order_id": oid,
                "amount": args["amount"],
                "applicant_sub": self.suite.accounts[actor].sub,
            }
            self.approvals[aid] = ap
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="create_refund",
                http_method="POST",
                http_path="/api/v1/refunds",
                outcome="pending_approval",
                status_code=202,
                order_id=oid,
                approval_id=aid,
            )
            returned_ap = dict(ap)
            if self.omit_approval_id_in_mcp:
                returned_ap.pop("approval_id", None)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"isError": False, "structuredContent": returned_ap},
                },
            )

        if tool == "get_refund":
            rf_id = args["refund_id"]
            rf = next(r for r in self.refunds if r["refund_id"] == rf_id)
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_refund",
                http_method="GET",
                http_path=f"/api/v1/refunds/{rf_id}",
                outcome="success",
                status_code=200,
                order_id=rf["order_id"],
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"isError": False, "structuredContent": rf},
                },
            )

        if tool == "get_approval":
            aid = args["approval_id"]
            ap = self.approvals[aid]
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_approval",
                http_method="GET",
                http_path=f"/guard/v1/approvals/{aid}",
                outcome="success",
                status_code=200,
                order_id=ap["order_id"],
                approval_id=aid,
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"isError": False, "structuredContent": ap},
                },
            )

        raise AssertionError(f"unexpected tool {tool}")

    def guard_handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        actor = self._actor_from_auth(request)
        if actor is None:
            return httpx.Response(
                401, json={"code": "token_expired", "message": "过期", "request_id": "r-401"}
            )

        if request.url.path == "/guard/v1/console/audit-logs":
            items = list(reversed(self.audit_events))
            return httpx.Response(
                200, json={"items": items, "total": len(items), "page": 1, "page_size": 100}
            )

        if request.url.path.endswith("/order"):
            aid = request.url.path.split("/")[-2]
            ap = self.approvals[aid]
            req_id = f"req-ord-{self.next_audit_id + 1}"
            self._add_audit(
                actor,
                request_id=req_id,
                event_type="tool_call",
                channel="console",
                tool="get_order",
                http_method="GET",
                http_path=f"/api/v1/orders/{ap['order_id']}",
                outcome="success",
                status_code=200,
                order_id=ap["order_id"],
                approval_id=aid,
            )
            return httpx.Response(
                200,
                json={"order_id": ap["order_id"], "region": "east"},
                headers={"X-Request-Id": req_id},
            )

        if request.url.path.endswith("/approve"):
            aid = request.url.path.split("/")[-2]
            ap = self.approvals[aid]
            req_id = f"req-ap-{self.next_audit_id + 1}"
            acc = self.suite.accounts[actor]
            if acc.role != "supervisor":
                self._add_audit(
                    actor,
                    request_id=req_id,
                    event_type="approval_approve",
                    channel="console",
                    tool="create_refund",
                    outcome="access_denied",
                    status_code=403,
                    error_code="insufficient_role",
                    approval_id=aid,
                )
                return httpx.Response(
                    403,
                    json={
                        "code": "insufficient_role",
                        "message": "仅主管可用",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            if ap["status"] != "pending":
                self._add_audit(
                    actor,
                    request_id=req_id,
                    event_type="approval_approve",
                    channel="console",
                    tool="create_refund",
                    outcome="conflict",
                    status_code=409,
                    error_code="approval_not_pending",
                    approval_id=aid,
                )
                return httpx.Response(
                    409,
                    json={
                        "code": "approval_not_pending",
                        "message": "非待审批",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            ap["status"] = "executed"
            ap["decided_by_sub"] = acc.sub
            ap["refund_id"] = "R000105"
            ap["message"] = f"主管已同意退款申请 {aid}，退款已执行，退款单号 R000105。"
            self.refunds.append(
                {
                    "refund_id": "R000105",
                    "order_id": ap["order_id"],
                    "amount": ap["amount"],
                    "created_by": acc.sub,
                    "created_by_role": "supervisor",
                }
            )
            self._add_audit(
                actor,
                request_id=req_id,
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                outcome="success",
                status_code=200,
                approval_id=aid,
                result={"from": "pending", "to": "executing"},
            )
            self._add_audit(
                actor,
                request_id=req_id,
                event_type="approval_execute",
                channel="console",
                tool="create_refund",
                outcome="success",
                status_code=201,
                approval_id=aid,
                on_behalf_of_sub=ap["applicant_sub"],
                result={"from": "executing", "to": "executed", "refund_id": "R000105"},
            )
            return httpx.Response(200, json=ap, headers={"X-Request-Id": req_id})

        raise AssertionError(f"unexpected guard path {request.url.path}")

    def chat_handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if self.chat_502_remaining > 0:
            self.chat_502_remaining -= 1
            return httpx.Response(502, json={"code": "dify_error", "message": "助手暂不可用"})
        actor = self._actor_from_auth(request)
        assert actor is not None
        body = json.loads(request.content)
        query = body["query"]

        if "怎么查进度" in query:
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-probe",
                    "answer": "可查进度",
                    "citations": [{"document": "客服操作手册", "clause": "2.4"}],
                    "trace": [],
                },
            )

        if "A1023" in query and "能退吗" in query:
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/A1023",
                outcome="success",
                status_code=200,
                order_id="A1023",
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-d01",
                    "message_id": "m1",
                    "answer": "结论：可退。依据《售后制度》第 3.2 条，运费由商家承担。",
                    "citations": [{"document": "售后制度", "clause": "3.2"}],
                    "trace": [{"tool": "get_order", "kind": "ok", "http_status": 200}],
                },
            )
        if "B2087" in query:
            rid = f"req-chat-deny-{self.next_audit_id + 1}"
            self._add_audit(
                actor,
                request_id=rid,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/B2087",
                outcome="access_denied",
                status_code=403,
                error_code="order_access_denied",
                order_id="B2087",
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-d01",
                    "message_id": "m2",
                    "answer": "你没有权限查看该订单。",
                    "citations": [],
                    "trace": [
                        {
                            "tool": "get_order",
                            "kind": "denied",
                            "http_status": 403,
                            "error_code": "order_access_denied",
                            "request_id": rid,
                        }
                    ],
                },
            )
        if "帮他把款退了" in query:
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/A1023",
                outcome="success",
                status_code=200,
                order_id="A1023",
            )
            if self.omit_approval_id_in_chat:
                return httpx.Response(
                    200,
                    json={
                        "conversation_id": "conv-d01",
                        "message_id": "m3",
                        "answer": "未能提交。",
                        "citations": [],
                        "trace": [{"tool": "get_order", "kind": "ok", "http_status": 200}],
                    },
                )
            self.next_approval_num += 1
            aid = f"AP{self.next_approval_num:06d}"
            self.approvals[aid] = {
                "approval_id": aid,
                "status": "pending",
                "order_id": "A1023",
                "amount": 299.0,
                "applicant_sub": self.suite.accounts[actor].sub,
            }
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="create_refund",
                http_method="POST",
                http_path="/api/v1/refunds",
                outcome="pending_approval",
                status_code=202,
                order_id="A1023",
                approval_id=aid,
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-d01",
                    "message_id": "m3",
                    "answer": f"已提交退款申请 {aid}，等待主管审批。退款尚未执行。",
                    "citations": [],
                    "trace": [
                        {"tool": "get_order", "kind": "ok", "http_status": 200},
                        {
                            "tool": "create_refund",
                            "kind": "pending",
                            "http_status": 202,
                            "approval_id": aid,
                        },
                    ],
                },
            )
        if "C3051" in query:
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/C3051",
                outcome="success",
                status_code=200,
                order_id="C3051",
            )
            if self.chat_unknown_verdict:
                ans_text = "订单 C3051 已签收，请参照《售后制度》第 2.1 条核对。"
            else:
                ans_text = "结论：可以退货退款。依据《售后制度》第 2.1 条。"
            if self.leak_token_in_chat:
                ans_text = f"{ans_text} 凭证={FAKE_TOKENS[actor]}"
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-d06",
                    "message_id": "m6",
                    "answer": ans_text,
                    "citations": [{"document": "售后制度", "clause": "2.1"}],
                    "trace": [{"tool": "get_order", "kind": "ok", "http_status": 200}],
                },
            )
        if "A1024" in query:
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/A1024",
                outcome="success",
                status_code=200,
                order_id="A1024",
            )
            if self.d02_skip_submit_remaining > 0:
                self.d02_skip_submit_remaining -= 1
                return httpx.Response(
                    200,
                    json={
                        "conversation_id": "conv-d02",
                        "message_id": "m-d02-nosub",
                        "answer": "已查到订单 A1024，暂未提交退款申请。",
                        "citations": [],
                        "trace": [{"tool": "get_order", "kind": "ok", "http_status": 200}],
                    },
                )
            self.next_approval_num += 1
            aid = f"AP{self.next_approval_num:06d}"
            self.approvals[aid] = {
                "approval_id": aid,
                "status": "pending",
                "order_id": "A1024",
                "amount": 199.0,
                "applicant_sub": self.suite.accounts[actor].sub,
            }
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="create_refund",
                http_method="POST",
                http_path="/api/v1/refunds",
                outcome="pending_approval",
                status_code=202,
                order_id="A1024",
                approval_id=aid,
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-d02",
                    "message_id": "m-d02-sub",
                    "answer": f"已提交退款申请 {aid}，等待主管审批。",
                    "citations": [],
                    "trace": [
                        {"tool": "get_order", "kind": "ok", "http_status": 200},
                        {
                            "tool": "create_refund",
                            "kind": "pending",
                            "http_status": 202,
                            "approval_id": aid,
                        },
                    ],
                },
            )
        if "A1033" in query:
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_order",
                http_method="GET",
                http_path="/api/v1/orders/A1033",
                outcome="success",
                status_code=200,
                order_id="A1033",
            )
            if self.d20_skip_submit:
                return httpx.Response(
                    200,
                    json={
                        "conversation_id": "conv-d20",
                        "message_id": "m-d20-nosub",
                        "answer": "已查到订单 A1033，请确认后再提交。",
                        "citations": [],
                        "trace": [{"tool": "get_order", "kind": "ok", "http_status": 200}],
                    },
                )
            self.next_approval_num += 1
            aid = f"AP{self.next_approval_num:06d}"
            self.approvals[aid] = {
                "approval_id": aid,
                "status": "pending",
                "order_id": "A1033",
                "amount": 199.0,
                "applicant_sub": self.suite.accounts[actor].sub,
            }
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="create_refund",
                http_method="POST",
                http_path="/api/v1/refunds",
                outcome="pending_approval",
                status_code=202,
                order_id="A1033",
                approval_id=aid,
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conv-d20",
                    "message_id": "m-d20-sub",
                    "answer": f"已提交退款申请 {aid}，等待主管审批。",
                    "citations": [],
                    "trace": [
                        {"tool": "get_order", "kind": "ok", "http_status": 200},
                        {
                            "tool": "create_refund",
                            "kind": "pending",
                            "http_status": 202,
                            "approval_id": aid,
                        },
                    ],
                },
            )
        if "那笔退款批了吗" in query:
            if not self.approvals:
                return httpx.Response(
                    200,
                    json={
                        "conversation_id": body.get("conversation_id") or "conv-track",
                        "message_id": "m-no-ap",
                        "answer": "暂无待审批申请。",
                        "citations": [],
                        "trace": [],
                    },
                )
            latest = list(self.approvals.values())[-1]
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_approval",
                http_method="GET",
                http_path=f"/guard/v1/approvals/{latest['approval_id']}",
                outcome="success",
                status_code=200,
                order_id=latest["order_id"],
                approval_id=latest["approval_id"],
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": body.get("conversation_id") or "conv-track",
                    "message_id": "m-ap-status",
                    "answer": "当前状态：等待主管审批。",
                    "citations": [],
                    "trace": [{"tool": "get_approval", "kind": "ok", "http_status": 200}],
                },
            )
        if "现在批了吗" in query:
            latest = list(self.approvals.values())[-1]
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_approval",
                http_method="GET",
                http_path=f"/guard/v1/approvals/{latest['approval_id']}",
                outcome="success",
                status_code=200,
                order_id=latest["order_id"],
                approval_id=latest["approval_id"],
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": body.get("conversation_id") or "conv-d02",
                    "message_id": "m-d02-t3",
                    "answer": f"退款已执行，退款单号 {latest.get('refund_id', 'R000105')}。",
                    "citations": [],
                    "trace": [{"tool": "get_approval", "kind": "ok", "http_status": 200}],
                },
            )
        if "查一下退款单 R000105" in query:
            if not any(r["refund_id"] == "R000105" for r in self.refunds):
                return httpx.Response(
                    200,
                    json={
                        "conversation_id": body.get("conversation_id") or "conv-d02",
                        "message_id": "m-d02-t4-miss",
                        "answer": "未查到退款单 R000105。",
                        "citations": [],
                        "trace": [],
                    },
                )
            self._add_audit(
                actor,
                event_type="tool_call",
                channel="agent",
                tool="get_refund",
                http_method="GET",
                http_path="/api/v1/refunds/R000105",
                outcome="success",
                status_code=200,
                order_id="A1024",
            )
            return httpx.Response(
                200,
                json={
                    "conversation_id": body.get("conversation_id") or "conv-d02",
                    "message_id": "m-d02-t4",
                    "answer": "退款单 R000105 由审批主管执行。",
                    "citations": [],
                    "trace": [{"tool": "get_refund", "kind": "ok", "http_status": 200}],
                },
            )
        raise AssertionError(f"unexpected chat query: {query}")


def _make_runner_env(
    tmp_path: Path,
    backend: FakeBackend,
    today_box: list[date],
    *,
    judge: VerdictJudge | None = None,
) -> tuple[Settings, EvalRunner, EnvironmentManager]:
    suite = load_suite(EVALS_DIR)
    manifest = load_tools_manifest(REPO_ROOT)
    settings = Settings(report_dir=str(tmp_path / "runs"), ready_timeout_seconds=0.5)

    mcp = McpClient(
        settings.mcp_url,
        write_tools={"create_refund"},
        http_client=httpx.Client(transport=httpx.MockTransport(backend.mcp_handler)),
    )
    guard = GuardClient(
        settings.guard_url,
        http_client=httpx.Client(transport=httpx.MockTransport(backend.guard_handler)),
    )
    chat = ChatClient(
        settings.chat_url,
        http_client=httpx.Client(transport=httpx.MockTransport(backend.chat_handler)),
    )
    tokens = StaticTokenProvider(FAKE_TOKENS)

    def fake_cmd(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "reset" in cmd or "restart" in cmd:
            backend.reset_state()
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    env = EnvironmentManager(
        settings,
        suite,
        mcp_client=mcp,
        guard_client=guard,
        chat_client=chat,
        token_provider=tokens,
        repo_root=REPO_ROOT,
        cmd_runner=fake_cmd,
        sleep_fn=lambda _: None,
        today_fn=lambda: today_box[0],
    )
    runner = EvalRunner(
        settings,
        suite,
        mcp_client=mcp,
        guard_client=guard,
        chat_client=chat,
        token_provider=tokens,
        env_manager=env,
        tools_manifest=manifest,
        judge=judge,
        evals_dir=EVALS_DIR,
        repo_root=REPO_ROOT,
        sleep_fn=lambda _: None,
    )
    return settings, runner, env


def test_interpolate_value() -> None:
    resolved, missing = interpolate_value(
        {"a": "${x}", "b": "id=${x}", "c": ["${y}", 1]}, {"x": "AP1"}
    )
    assert resolved == {"a": "AP1", "b": "id=AP1", "c": ["${y}", 1]}
    assert missing == ["y"]


def test_runner_end_to_end_rescore_dirty_date_and_no_token_on_disk(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    backend = FakeBackend(suite)
    today_box = [date(2026, 11, 21)]
    settings, runner, env = _make_runner_env(tmp_path, backend, today_box)

    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    selected = [all_cases["T-01"], all_cases["G-01"], all_cases["D-01"], all_cases["D-06"]]

    run_dir, report = runner.run(
        selected,
        repeats=1,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=False,
        label="mock",
    )
    assert report["verdict"] == "pass", report["failures"]
    assert report["metrics"]["authz_block_rate"]["value"] == 1.0
    assert report["metrics"]["approval_compliance"]["value"] == 1.0
    assert report["metrics"]["approval_compliance"]["total"] == 2
    assert report["metrics"]["audit_completeness"]["value"] == 1.0
    assert report["metrics"]["answer_accuracy"]["value"] == 1.0
    # T-01 read-only (reset 1), G-01 dirty -> reset 2, D-01 dirty -> reset 3 before D-06
    assert env.reset_count == 3

    # Ensure ZERO tokens written to any output file (5.12.1)
    for fname in ("report.json", "report.md", "transcripts.jsonl", "audit.jsonl", "run_meta.json"):
        text = (run_dir / fname).read_text(encoding="utf-8")
        for tok in FAKE_TOKENS.values():
            assert tok not in text
            assert tok.split(".")[2] not in text

    # Offline `score` produces IDENTICAL metrics (5.16 #5)
    rescored = rescore_run_dir(run_dir, settings=settings, evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
    assert rescored["verdict"] == report["verdict"]
    for key in ("authz_block_rate", "approval_compliance", "audit_completeness", "answer_accuracy"):
        assert rescored["metrics"][key]["passed"] == report["metrics"][key]["passed"]
        assert rescored["metrics"][key]["total"] == report["metrics"][key]["total"]

    # Date rollover across midnight triggers automatic reset (5.8.2)
    assert env.needs_reset() is False
    today_box[0] = date(2026, 11, 22)
    assert env.needs_reset() is True


def test_runner_missing_var_skip_env_abort_and_resume(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    backend = FakeBackend(suite)
    backend.omit_approval_id_in_chat = True
    today_box = [date(2026, 11, 21)]
    _, runner, _ = _make_runner_env(tmp_path, backend, today_box)

    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    # D-01 step p1 references ${ap} which won't be saved -> p1 skipped as should failure
    _, rep_skip = runner.run(
        [all_cases["D-01"]], repeats=1, layers=["dialogue"], is_full_suite=False
    )
    assert any(
        f["assertion"] == "skipped_missing_var" and f["level"] == "should"
        for f in rep_skip["failures"]
    )

    # Consecutive 3 chat 502 errors abort run -> verdict == "incomplete", then --resume recovers!
    backend2 = FakeBackend(suite)
    backend2.chat_502_remaining = 3
    _, runner2, _ = _make_runner_env(tmp_path / "r2", backend2, today_box)
    run_dir, rep_abort = runner2.run(
        [all_cases["T-01"], all_cases["D-06"], all_cases["D-06"]],
        repeats=3,
        layers=["tool", "dialogue"],
        is_full_suite=False,
    )
    assert rep_abort["verdict"] == "incomplete"

    # Resume after backend recovers: T-01 is skipped (already done), D-06 rounds complete!
    _, rep_resumed = runner2.run(
        [all_cases["T-01"], all_cases["D-06"]],
        repeats=3,
        layers=["tool", "dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    assert rep_resumed["verdict"] == "pass"


def test_environment_preflight_and_reset_errors(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    backend = FakeBackend(suite)
    today_box = [date(2026, 11, 21)]
    settings, _, env = _make_runner_env(tmp_path, backend, today_box)

    # Full preflight P1–P7 passes with mocked subprocess & clients
    pre = env.run_preflight(mode="full", layers={"tool", "guard", "dialogue"})
    assert pre.passed is True
    assert [item.id for item in pre.items] == ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]

    # Reset failure on non-zero command
    bad_env = EnvironmentManager(
        settings,
        suite,
        mcp_client=env._mcp,
        guard_client=env._guard,
        chat_client=env._chat,
        token_provider=env._tokens,
        cmd_runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err"),
    )
    with pytest.raises(EnvironmentResetError, match="reset guard"):
        bad_env.reset()


def test_cli_commands_and_exit_codes(tmp_path: Path) -> None:
    # lint -> exit 0
    assert cli_main(["lint"], evals_dir=EVALS_DIR, repo_root=REPO_ROOT) == EXIT_PASS

    # judge-check unconfigured -> exit 2
    assert (
        cli_main(
            ["judge-check"],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
            settings=Settings(judge_base_url="", judge_api_key="", judge_model=""),
        )
        == EXIT_ENV_ERROR
    )

    # run invalid repeats / unknown layer -> exit 3
    assert (
        cli_main(["run", "--repeats", "9"], evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
        == EXIT_USAGE_OR_LINT
    )
    assert (
        cli_main(["run", "--layer", "bogus"], evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
        == EXIT_USAGE_OR_LINT
    )
    assert (
        cli_main(["preflight", "--layer", "bogus"], evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
        == EXIT_USAGE_OR_LINT
    )

    # score & compare on missing dirs -> exit 3
    assert (
        cli_main(["score", str(tmp_path / "nope")], evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
        == EXIT_USAGE_OR_LINT
    )
    assert (
        cli_main(["compare", str(tmp_path / "nope")], evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
        == EXIT_USAGE_OR_LINT
    )

    # compare valid report against itself -> exit 0
    rep = {
        "verdict": "pass",
        "metrics": {
            "authz_block_rate": {"value": 1.0},
            "approval_compliance": {"value": 1.0},
            "audit_completeness": {"value": 1.0},
            "answer_accuracy": {"value": 0.95},
        },
        "breakdown": {"sub_metrics": {}},
        "cases": [{"id": "D-01", "stability": "stable_pass"}],
        "environment": {"hashes": {"a": "1"}, "models": {}},
    }
    rpath = tmp_path / "report.json"
    rpath.write_text(json.dumps(rep), encoding="utf-8")
    assert (
        cli_main(
            ["compare", str(rpath), "--against", str(rpath)],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_PASS
    )

    # score CLI command on a real mock-generated run_dir -> exit 0
    suite = load_suite(EVALS_DIR)
    backend = FakeBackend(suite)
    today_box = [date(2026, 11, 21)]
    _, runner, _ = _make_runner_env(tmp_path / "cli_run", backend, today_box)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    run_dir, _ = runner.run([all_cases["T-01"]], repeats=1, layers=["tool"], is_full_suite=False)
    assert (
        cli_main(
            ["score", str(run_dir), "--judge", "off"], evals_dir=EVALS_DIR, repo_root=REPO_ROOT
        )
        == EXIT_PASS
    )
    assert (
        cli_main(
            ["compare", str(run_dir), "--against", str(run_dir)],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_PASS
    )

    # M10: score CLI on dir missing run_meta.json or empty audit.jsonl -> exit 3
    no_meta = tmp_path / "no_meta"
    no_meta.mkdir()
    (no_meta / "transcripts.jsonl").write_text("", encoding="utf-8")
    (no_meta / "audit.jsonl").write_text("", encoding="utf-8")
    assert (
        cli_main(
            ["score", str(no_meta), "--judge", "off"], evals_dir=EVALS_DIR, repo_root=REPO_ROOT
        )
        == EXIT_USAGE_OR_LINT
    )

    empty_cases = tmp_path / "empty_cases"
    empty_cases.mkdir()
    (empty_cases / "run_meta.json").write_text(
        json.dumps({"run": {"id": "empty", "layers": ["tool"], "repeats": 1}}),
        encoding="utf-8",
    )
    (empty_cases / "transcripts.jsonl").write_text("", encoding="utf-8")
    (empty_cases / "audit.jsonl").write_text("", encoding="utf-8")
    assert (
        cli_main(
            ["score", str(empty_cases), "--judge", "off"],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_USAGE_OR_LINT
    )


def test_h4_invariant_probe_errors_and_pagination(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    # 1. Probe returns 503 -> case env_error, verdict == "incomplete", exit code 2
    b503 = FakeBackend(suite)
    b503.probe_refunds_mode = "503"
    _, r503, _ = _make_runner_env(tmp_path / "p503", b503, today_box)
    run_dir_503, rep_503 = r503.run(
        [all_cases["G-01"]], repeats=1, layers=["guard"], is_full_suite=False
    )
    assert rep_503["verdict"] == "incomplete"
    assert _verdict_to_exit_code(rep_503["verdict"]) == EXIT_ENV_ERROR
    aud_503 = json.loads((run_dir_503 / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert aud_503["env_error"] is True
    assert aud_503["invariant_snapshots"][0]["probe_complete"] is False

    # 2. Probe returns ok but missing items key -> env_error, verdict == "incomplete"
    b_miss = FakeBackend(suite)
    b_miss.probe_refunds_mode = "missing_items"
    _, r_miss, _ = _make_runner_env(tmp_path / "pmiss", b_miss, today_box)
    _, rep_miss = r_miss.run([all_cases["G-01"]], repeats=1, layers=["guard"], is_full_suite=False)
    assert rep_miss["verdict"] == "incomplete"
    assert _verdict_to_exit_code(rep_miss["verdict"]) == EXIT_ENV_ERROR

    # 3. Probe spans multiple pages -> runner auto-pages and passes!
    b_paged = FakeBackend(suite)
    b_paged.probe_refunds_mode = "paged"
    _, r_paged, _ = _make_runner_env(tmp_path / "ppaged", b_paged, today_box)
    run_dir_p, rep_paged = r_paged.run(
        [all_cases["G-01"]], repeats=1, layers=["guard"], is_full_suite=False
    )
    assert rep_paged["verdict"] == "pass", rep_paged["failures"]
    aud_p = json.loads((run_dir_p / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert any(sid.endswith("_p2") for sid in aud_p["step_order"])

    # 4. Probe pagination returns empty page before reaching total -> incomplete
    b_short = FakeBackend(suite)
    b_short.probe_refunds_mode = "short_page"
    _, r_short, _ = _make_runner_env(tmp_path / "pshort", b_short, today_box)
    _, rep_short = r_short.run(
        [all_cases["G-01"]], repeats=1, layers=["guard"], is_full_suite=False
    )
    assert rep_short["verdict"] == "incomplete"


def test_m9_g02_missing_approval_id_skips_authz_as_must_fail(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    backend = FakeBackend(suite)
    backend.omit_approval_id_in_mcp = True
    _, runner, _ = _make_runner_env(tmp_path / "g02_skip", backend, today_box)
    run_dir, rep = runner.run([all_cases["G-02"]], repeats=1, layers=["guard"], is_full_suite=False)
    assert rep["verdict"] == "fail"
    assert _verdict_to_exit_code(rep["verdict"]) == EXIT_FAIL
    assert rep["metrics"]["authz_block_rate"]["ok"] is False
    assert rep["metrics"]["authz_block_rate"]["planned"] == 3
    assert rep["metrics"]["authz_block_rate"]["total"] == 0
    assert any(
        f["assertion"] == "skipped_missing_var" and f["level"] == "must" for f in rep["failures"]
    )
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "被跳过的越权步骤" in md
    assert "G-02" in md


def test_m11_token_leak_live_and_rescore_parity_with_zero_disk_leak(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    backend = FakeBackend(suite)
    backend.leak_token_in_chat = True
    settings, runner, _ = _make_runner_env(tmp_path / "leak", backend, today_box)
    run_dir, live_rep = runner.run(
        [all_cases["D-06"]], repeats=1, layers=["dialogue"], is_full_suite=False
    )
    assert live_rep["verdict"] == "fail"
    assert any(
        f["assertion"] == "token_leak" and f["level"] == "must" for f in live_rep["failures"]
    )

    # Zero occurrences of leaked token OR its signature in ANY persisted file!
    leaked_tok = FAKE_TOKENS["chen"]
    leaked_sig = leaked_tok.split(".")[2]
    for fname in ("transcripts.jsonl", "audit.jsonl", "report.json", "report.md", "run_meta.json"):
        content = (run_dir / fname).read_text(encoding="utf-8")
        assert leaked_tok not in content, f"full token found in {fname}"
        assert leaked_sig not in content, f"token signature found in {fname}"
    # Redacted placeholder was written to transcripts.jsonl
    assert REDACTED_TOKEN in (run_dir / "transcripts.jsonl").read_text(encoding="utf-8")

    # Offline `score` produces IDENTICAL verdict and token_leak failure
    rescored = rescore_run_dir(run_dir, settings=settings, evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
    assert rescored["verdict"] == live_rep["verdict"] == "fail"
    assert any(
        f["assertion"] == "token_leak" and f["level"] == "must" for f in rescored["failures"]
    )


def test_l7_1_resume_meta_preservation_and_conflict_checks(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    backend = FakeBackend(suite)
    backend.chat_502_remaining = 2
    _, runner, _ = _make_runner_env(tmp_path / "l71", backend, today_box)
    run_dir, rep_abort = runner.run(
        [all_cases["T-01"], all_cases["D-06"]],
        repeats=2,
        layers=["tool", "dialogue"],
        categories=None,
        case_ids=["T-01", "D-06"],
        is_full_suite=False,
        label="orig-lbl",
    )
    assert rep_abort["verdict"] == "incomplete"
    assert backend.chat_502_remaining == 0
    orig_meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    orig_id = orig_meta["run"]["id"]
    orig_started = orig_meta["run"]["started_at"]
    assert orig_meta["run"]["layers"] == ["tool", "dialogue"]
    assert orig_meta["run"]["repeats"] == 2
    assert orig_meta["run"]["cases"] == ["T-01", "D-06"]
    assert orig_meta["run"]["suite_hash"]

    # Default resume scope inherits original layers, repeats, cases from run_meta.json["run"]
    layers, cats, cids, reps = _resolve_resume_scope(
        run_dir,
        cli_layers=None,
        cli_categories=None,
        cli_case_ids=None,
        cli_repeats=None,
        default_repeats=3,
        evals_dir=EVALS_DIR,
    )
    assert layers == ["tool", "dialogue"]
    assert cats is None
    assert cids == ["T-01", "D-06"]
    assert reps == 2

    # Conflicting CLI flags -> exit code 3 (ValueError)
    assert (
        cli_main(
            ["run", "--resume", str(run_dir), "--layer", "guard"],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_USAGE_OR_LINT
    )
    assert (
        cli_main(
            ["run", "--resume", str(run_dir), "--repeats", "3"],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_USAGE_OR_LINT
    )
    assert (
        cli_main(
            ["run", "--resume", str(run_dir), "--case", "D-01"],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_USAGE_OR_LINT
    )
    assert (
        cli_main(
            ["run", "--resume", str(run_dir), "--category", "policy"],
            evals_dir=EVALS_DIR,
            repo_root=REPO_ROOT,
        )
        == EXIT_USAGE_OR_LINT
    )

    # Resuming preserves original id & started_at from run_meta.json["run"]
    _, rep_resumed = runner.run(
        [all_cases["T-01"], all_cases["D-06"]],
        repeats=2,
        layers=["tool", "dialogue"],
        case_ids=["T-01", "D-06"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    assert rep_resumed["verdict"] == "pass"
    assert rep_resumed["run"]["id"] == orig_id
    assert rep_resumed["run"]["started_at"] == orig_started
    assert rep_resumed["run"]["label"] == "orig-lbl"

    # Mismatched suite_hash in run_meta.json -> exit code 3
    bad_hash_meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    bad_hash_meta["run"]["suite_hash"] = "0" * 64
    (run_dir / "run_meta.json").write_text(json.dumps(bad_hash_meta), encoding="utf-8")
    assert (
        cli_main(["run", "--resume", str(run_dir)], evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
        == EXIT_USAGE_OR_LINT
    )


def test_l7_3_audit_jsonl_matches_and_l7_5_jsonrpc_error_and_cost(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    # L7-5: JSON-RPC error response must NOT produce a guard audit ledger entry
    step = McpStep.model_validate(
        {
            "id": "s1",
            "do": "mcp",
            "as": "wang",
            "tool": "get_order",
            "args": {"order_id": "A1023"},
            "expect": {"result": "ok"},
        }
    )
    rpc_err_resp = normalize_mcp_response(
        "get_order",
        200,
        json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}),
    )
    assert _build_step_ledger(step, {"tool": "get_order", "args": {}}, rpc_err_resp, {}) == []

    # L7-3 & L7-5: D-01 (3 turns: t1 advise w/ citations, t2 denied template, t3 decide_refund)
    # -> router=6, main=2 (t1 + t3), estimated=8, upper_bound=9; audit.jsonl has audit_matches
    backend = FakeBackend(suite)
    _, runner, _ = _make_runner_env(tmp_path / "l73_75", backend, today_box)
    run_dir, rep = runner.run(
        [all_cases["D-01"]], repeats=1, layers=["dialogue"], is_full_suite=False
    )
    assert rep["verdict"] == "pass", rep["failures"]
    assert rep["cost"]["chat_turns"] == 3
    assert rep["cost"]["router_calls_estimated"] == 6
    assert rep["cost"]["main_model_calls_estimated"] == 2
    assert rep["cost"]["model_calls_estimated"] == 8
    assert rep["cost"]["model_calls_upper_bound"] == 9

    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(audit_rows) == 1
    matches = audit_rows[0]["audit_matches"]
    assert len(matches) == rep["audit_summary"]["expected"]
    assert all(m["status"] == "matched" and m["match_label"] == "匹配" for m in matches)


def test_r2_m1_dialogue_no_submit_d20_skip_and_full_suite_guard_skip(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    # 1. M1: Full run where dialogue case D-02 omits create_refund on round 1 (passes on round 2)
    # -> M2 actual < total planned (dialogue missed 1), NOT incomplete, M2 ok, fails only in M4!
    b_d02 = FakeBackend(suite)
    b_d02.d02_skip_submit_remaining = 1
    _, r_d02, _ = _make_runner_env(tmp_path / "m1_d02", b_d02, today_box)
    _, rep_d02 = r_d02.run(
        [all_cases["T-01"], all_cases["G-01"], all_cases["D-02"], all_cases["D-06"]],
        repeats=2,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=True,
    )
    assert rep_d02["run"]["complete"] is True
    assert rep_d02["verdict"] == "fail"  # M4 is 3/4 = 75% < 90%, NOT incomplete!
    assert _verdict_to_exit_code(rep_d02["verdict"]) == EXIT_FAIL
    m2 = rep_d02["metrics"]["approval_compliance"]
    assert m2["planned"] == 3  # G-01 (1) + D-02 x 2 rounds (2)
    assert m2["total"] == 2  # G-01 (1) + D-02 round 2 (1)
    assert m2["tg_planned"] == 1 and m2["tg_total"] == 1
    assert m2["dialogue_planned"] == 2 and m2["dialogue_total"] == 1
    assert m2["ok"] is True
    must_d02 = rep_d02["metrics"]["must_assertions"]
    assert must_d02["passed"] == must_d02["total"]
    assert rep_d02["metrics"]["answer_accuracy"]["passed"] == 3
    assert rep_d02["metrics"]["answer_accuracy"]["total"] == 4

    # 2. M1: D-20 where model fails to submit in t1 -> severity=must step g1 skipped as SHOULD,
    # must_assertions has zero failures, and variable-free must probe p1 still runs & passes!
    b_d20 = FakeBackend(suite)
    b_d20.d20_skip_submit = True
    _, r_d20, _ = _make_runner_env(tmp_path / "m1_d20", b_d20, today_box)
    _, rep_d20 = r_d20.run(
        [all_cases["T-01"], all_cases["G-01"], all_cases["D-20"]],
        repeats=1,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=True,
    )
    assert rep_d20["run"]["complete"] is True
    assert rep_d20["verdict"] == "fail"  # D-20 failed in M4, NOT incomplete and NO must failure
    must_d20 = rep_d20["metrics"]["must_assertions"]
    assert must_d20["passed"] == must_d20["total"]
    g1_fails = [
        f
        for f in rep_d20["failures"]
        if f["case"] == "D-20" and f["step"] == "g1" and f["assertion"] == "skipped_missing_var"
    ]
    assert len(g1_fails) == 1
    assert g1_fails[0]["level"] == "should"

    # 3. M1 + L9-2: Full run (all 3 layers) where guard case G-02 skips authz steps
    # -> verdict == "fail" and exit code 1 (NOT incomplete / exit code 2)!
    b_g02 = FakeBackend(suite)
    b_g02.omit_approval_id_in_mcp = True
    _, r_g02, _ = _make_runner_env(tmp_path / "m1_g02_full", b_g02, today_box)
    _, rep_g02 = r_g02.run(
        [all_cases["T-01"], all_cases["G-02"], all_cases["D-06"]],
        repeats=1,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=True,
    )
    assert rep_g02["run"]["complete"] is True
    assert rep_g02["verdict"] == "fail"
    assert _verdict_to_exit_code(rep_g02["verdict"]) == EXIT_FAIL
    m1_g02 = rep_g02["metrics"]["authz_block_rate"]
    assert m1_g02["ok"] is False
    assert m1_g02["tg_total"] < m1_g02["tg_planned"]
    must_g02 = rep_g02["metrics"]["must_assertions"]
    assert must_g02["passed"] < must_g02["total"]


def test_r2_l10_judge_skips_on_token_leak_and_calls_once_per_run(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]
    judge_settings = Settings(
        judge_base_url="http://judge.test/v1",
        judge_api_key="sk-test",
        judge_model="judge-model-1",
    )

    # 1. Answer contains issued token + rule verdict is "unknown" -> judge client NEVER called!
    leak_requests: list[str] = []

    def leak_judge_handler(request: httpx.Request) -> httpx.Response:
        leak_requests.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"label":"refundable","evidence":"订单"}'}}]
            },
        )

    leak_cache_dir = tmp_path / "leak_judge_cache"
    leak_judge = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(leak_judge_handler)),
        cache_dir=leak_cache_dir,
    )
    leak_judge.enabled = True

    b_leak = FakeBackend(suite)
    b_leak.chat_unknown_verdict = True
    b_leak.leak_token_in_chat = True
    _, r_leak, _ = _make_runner_env(tmp_path / "l10_leak", b_leak, today_box, judge=leak_judge)
    run_dir_leak, rep_leak = r_leak.run(
        [all_cases["D-06"]], repeats=1, layers=["dialogue"], is_full_suite=False
    )
    assert rep_leak["verdict"] == "fail"
    assert any(
        f["assertion"] == "token_leak" and f["level"] == "must" for f in rep_leak["failures"]
    )
    assert leak_requests == []
    assert leak_judge.call_count == 0
    assert leak_judge.cache_hits == 0
    assert not leak_cache_dir.exists() or list(leak_cache_dir.glob("*.json")) == []
    leaked_tok = FAKE_TOKENS["chen"]
    for fname in ("transcripts.jsonl", "audit.jsonl", "report.json", "report.md", "run_meta.json"):
        txt = (run_dir_leak / fname).read_text(encoding="utf-8")
        assert leaked_tok not in txt
        assert leaked_tok.split(".")[2] not in txt

    # 2. Clean ambiguous answer -> judge called EXACTLY ONCE during run() (no end-of-run repeat,
    # judge_cache_hits == 0); explicit offline `score` then hits cache cleanly.
    ok_requests: list[str] = []

    def ok_judge_handler(request: httpx.Request) -> httpx.Response:
        ok_requests.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"label":"refundable","evidence":"订单 C3051 已签收"}'
                        }
                    }
                ]
            },
        )

    ok_cache_dir = tmp_path / "ok_judge_cache"
    ok_judge = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(ok_judge_handler)),
        cache_dir=ok_cache_dir,
    )
    ok_judge.enabled = True

    b_ok = FakeBackend(suite)
    b_ok.chat_unknown_verdict = True
    settings_ok, r_ok, _ = _make_runner_env(tmp_path / "l10_ok", b_ok, today_box, judge=ok_judge)
    run_dir_ok, rep_ok = r_ok.run(
        [all_cases["D-06"]], repeats=1, layers=["dialogue"], is_full_suite=False
    )
    assert rep_ok["verdict"] == "pass", rep_ok["failures"]
    assert len(ok_requests) == 1
    assert ok_judge.call_count == 1
    assert ok_judge.cache_hits == 0
    assert rep_ok["cost"]["judge_calls"] == 1
    assert rep_ok["cost"]["judge_cache_hits"] == 0

    # Explicit `score` rescores using disk cache without remote call
    rescore_judge = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(ok_judge_handler)),
        cache_dir=ok_cache_dir,
    )
    rescore_judge.enabled = True
    rescored = rescore_run_dir(
        run_dir_ok,
        settings=settings_ok,
        judge=rescore_judge,
        evals_dir=EVALS_DIR,
        repo_root=REPO_ROOT,
    )
    assert rescored["verdict"] == "pass"
    assert len(ok_requests) == 1  # No new remote HTTP call
    assert rescore_judge.call_count == 0
    assert rescore_judge.cache_hits == 1


def test_r3_m1_full_suite_write_bypass_zero_denom_and_env_precedence(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    # 1. All 3 layers, all create_refund return direct success (bypassing approval -> M2 denom == 0)
    #    -> verdict == "fail", exit code 1, report first screen lists must failure (NOT incomplete)
    b_bypass = FakeBackend(suite)
    b_bypass.direct_execute_refund = True
    _, r_bypass, _ = _make_runner_env(tmp_path / "r3_m1_bypass", b_bypass, today_box)
    run_dir_bypass, rep_bypass = r_bypass.run(
        [all_cases["T-01"], all_cases["G-01"], all_cases["D-06"]],
        repeats=1,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=True,
    )
    assert rep_bypass["run"]["complete"] is True
    assert rep_bypass["verdict"] == "fail"
    assert _verdict_to_exit_code(rep_bypass["verdict"]) == EXIT_FAIL
    assert rep_bypass["metrics"]["approval_compliance"]["total"] == 0
    assert rep_bypass["metrics"]["approval_compliance"]["ok"] is False
    md_bypass = (run_dir_bypass / "report.md").read_text(encoding="utf-8")
    first_screen = md_bypass.split("## 指标总览")[0]
    assert "不通过" in first_screen
    assert "阻断性 `must` 失败" in first_screen
    assert "运行不完整" not in first_screen

    # 2. All 3 layers, zero must failures and zero tool/guard underflow, but M2 denom == 0
    #    -> verdict == "incomplete", exit code 2
    b_zero = FakeBackend(suite)
    _, r_zero, _ = _make_runner_env(tmp_path / "r3_m1_zero", b_zero, today_box)
    _, rep_zero = r_zero.run(
        [all_cases["T-01"], all_cases["D-06"]],
        repeats=1,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=True,
    )
    assert rep_zero["run"]["complete"] is True
    assert rep_zero["verdict"] == "incomplete"
    assert _verdict_to_exit_code(rep_zero["verdict"]) == EXIT_ENV_ERROR
    assert rep_zero["metrics"]["approval_compliance"]["total"] == 0

    # 3. All 3 layers, BOTH env_error AND must failure -> env_error takes precedence ("incomplete")
    b_both = FakeBackend(suite)
    b_both.direct_execute_refund = True
    b_both.chat_502_remaining = 1
    _, r_both, _ = _make_runner_env(tmp_path / "r3_m1_both", b_both, today_box)
    _, rep_both = r_both.run(
        [all_cases["T-01"], all_cases["G-01"], all_cases["D-06"]],
        repeats=1,
        layers=["tool", "guard", "dialogue"],
        is_full_suite=True,
    )
    assert rep_both["run"]["complete"] is False
    assert rep_both["verdict"] == "incomplete"
    assert _verdict_to_exit_code(rep_both["verdict"]) == EXIT_ENV_ERROR


def test_r3_l6_resume_reuses_saved_assertions_calibration_split_and_unavailable_count(
    tmp_path: Path,
) -> None:
    from harness.loader import load_calibration

    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    samples = load_calibration(EVALS_DIR)
    sample_by_query = {s.query: s for s in samples}
    today_box = [date(2026, 11, 21)]
    judge_settings = Settings(
        judge_base_url="http://judge.test/v1",
        judge_api_key="sk-test",
        judge_model="judge-model-1",
    )

    judge_mode = {"fail_verdict": True}
    verdict_http_calls: list[str] = []

    def judge_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_content = body["messages"][1]["content"]
        for q, s in sample_by_query.items():
            if q in user_content and s.answer in user_content:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {"label": s.expected_label, "evidence": s.answer[:10]},
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ]
                    },
                )
        verdict_http_calls.append(user_content)
        if judge_mode["fail_verdict"]:
            return httpx.Response(503, text="judge unavailable")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"label":"refundable","evidence":"订单 C3051 已签收"}'
                        }
                    }
                ]
            },
        )

    cache_dir = tmp_path / "r3_l6_cache"
    judge1 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(judge_handler)),
        cache_dir=cache_dir,
    )
    cal1 = judge1.calibrate(samples)
    assert cal1.passed is True
    assert judge1.calibration_call_count == 12
    assert judge1.call_count == 0

    # Run 1: D-06 round 1 completes while judge is 503 unavailable (NOT cached on disk!);
    # then D-06 round 2 hits chat 502 env error.
    b1 = FakeBackend(suite)
    b1.chat_unknown_verdict = True

    orig_chat_handler = b1.chat_handler
    d06_turns = {"count": 0}

    def chat_fail_on_second(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/healthz":
            d06_turns["count"] += 1
            if d06_turns["count"] == 2:
                return httpx.Response(502, json={"code": "dify_error", "message": "暂不可用"})
        return orig_chat_handler(request)

    b1.chat_handler = chat_fail_on_second  # type: ignore[method-assign]
    _, runner1, _ = _make_runner_env(tmp_path / "r3_l6_run", b1, today_box, judge=judge1)
    run_dir, rep1 = runner1.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
    )
    assert rep1["verdict"] == "incomplete"
    assert len(verdict_http_calls) == 1
    assert rep1["cost"]["judge_calibration_calls"] == 12
    assert rep1["cost"]["judge_calls"] == 1
    assert rep1["cost"]["judge_unavailable_steps"] == 1
    assert rep1["breakdown"]["sub_metrics"]["verdict_accuracy"]["judge_unavailable_steps"] == 1
    md1 = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "LLM 评审校准调用数：12" in md1
    assert "LLM 评审判定调用数：1（缓存命中 0，作废 0，评审不可用受影响步骤数 1）" in md1

    # Resume: judge is now healthy (returns 200). Round 1 MUST reuse persisted assertions
    # (ZERO judge calls for round 1, round 1 unavailable verdict preserved);
    # Round 2 runs fresh and makes 1 judge call.
    judge_mode["fail_verdict"] = False
    verdict_http_calls.clear()
    judge2 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(judge_handler)),
        cache_dir=cache_dir,
    )
    cal2 = judge2.calibrate(samples)
    assert cal2.passed is True
    assert judge2.calibration_call_count == 12
    assert judge2.call_count == 0

    b2 = FakeBackend(suite)
    b2.chat_unknown_verdict = True
    _, runner2, _ = _make_runner_env(tmp_path / "r3_l6_resume_env", b2, today_box, judge=judge2)
    _, rep2 = runner2.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    # Only round 2 triggered a verdict judge call; round 1 produced 0 calls and 0 cache hits!
    # R4-L2: report cost accumulates prior run's counts (run1: 12/1; run2: 12/1)
    assert len(verdict_http_calls) == 1
    assert judge2.calibration_call_count == 12
    assert judge2.call_count == 1
    assert judge2.cache_hits == 0
    assert rep2["cost"]["judge_calibration_calls"] == 24
    assert rep2["cost"]["judge_calls"] == 2
    assert rep2["cost"]["judge_cache_hits"] == 0
    # Round 1's unavailable verdict was preserved from disk, while round 2 passed
    assert rep2["cost"]["judge_unavailable_steps"] == 1
    assert rep2["breakdown"]["sub_metrics"]["verdict_accuracy"]["judge_unavailable_steps"] == 1
    assert rep2["metrics"]["answer_accuracy"]["passed"] == 1
    assert rep2["metrics"]["answer_accuracy"]["total"] == 2
    md2 = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "评审不可用 1" in md2
    assert "LLM 评审校准调用数：24" in md2
    assert "LLM 评审判定调用数：2（缓存命中 0，作废 0，评审不可用受影响步骤数 1）" in md2

    # Resume when ALL cases are already completed -> ZERO new judge calls and ZERO cache hits;
    # accumulated totals from prior runs remain preserved!
    verdict_http_calls.clear()
    judge3 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(judge_handler)),
        cache_dir=cache_dir,
    )
    judge3.enabled = True
    _, runner3, _ = _make_runner_env(tmp_path / "r3_l6_noop", b2, today_box, judge=judge3)
    _, rep3 = runner3.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    assert verdict_http_calls == []
    assert judge3.call_count == 0
    assert judge3.cache_hits == 0
    assert rep3["cost"]["judge_calibration_calls"] == 24
    assert rep3["cost"]["judge_calls"] == 2
    assert rep3["cost"]["judge_cache_hits"] == 0
    assert rep3["cost"]["judge_unavailable_steps"] == 1

    # Fallback: if a resumed transcript row lacks persisted `assertions`, runner re-evaluates it
    t_path = run_dir / "transcripts.jsonl"
    rows = [json.loads(ln) for ln in t_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows[0].pop("assertions", None)
    t_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    judge4 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(judge_handler)),
        cache_dir=cache_dir,
    )
    judge4.enabled = True
    _, runner4, _ = _make_runner_env(tmp_path / "r3_l6_fallback", b2, today_box, judge=judge4)
    _, rep4 = runner4.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    # Row 0 had assertions stripped -> re-evaluated via cache hit (1), row 1 reused persisted (0)
    assert judge4.cache_hits == 1
    assert judge4.call_count == 0
    assert rep4["metrics"]["answer_accuracy"]["passed"] == 2
    assert rep4["cost"]["judge_unavailable_steps"] == 0
    assert rep4["cost"]["judge_calls"] == 2
    assert rep4["cost"]["judge_cache_hits"] == 1


def test_r4_l2_saved_assertions_validation_and_corrupted_resume_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Direct schema validation edge cases
    valid_item = {
        "name": "verdict",
        "level": "should",
        "passed": True,
        "expected": "refundable",
        "actual": "refundable",
        "attribution": "生成",
    }
    assert _is_valid_saved_assertions([valid_item]) is True
    assert _is_valid_saved_assertions([]) is False
    assert _is_valid_saved_assertions("not-a-list") is False
    assert _is_valid_saved_assertions(["not-a-dict"]) is False
    assert _is_valid_saved_assertions([{}]) is False
    assert _is_valid_saved_assertions([{**valid_item, "name": ""}]) is False
    assert _is_valid_saved_assertions([{**valid_item, "level": "warn"}]) is False
    assert _is_valid_saved_assertions([{**valid_item, "passed": 1}]) is False
    missing_expected = {k: v for k, v in valid_item.items() if k != "expected"}
    assert _is_valid_saved_assertions([missing_expected]) is False
    assert _is_valid_saved_assertions([{**valid_item, "attribution": 123}]) is False
    assert _is_valid_saved_assertions([{**valid_item, "needs_manual_review": "yes"}]) is False
    assert _is_valid_saved_assertions([{**valid_item, "verdict_source": 99}]) is False
    assert _is_valid_saved_assertions([{**valid_item, "judge_record": "bad"}]) is False

    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]

    # Run D-01 (with skipped step p1) + D-06 to disk, then corrupt assertions to `[{}]`
    backend = FakeBackend(suite)
    backend.omit_approval_id_in_chat = True
    _, runner1, _ = _make_runner_env(tmp_path / "r4_l2_corrupt_1", backend, today_box)
    run_dir, _ = runner1.run(
        [all_cases["D-01"], all_cases["D-06"]],
        repeats=1,
        layers=["dialogue"],
        is_full_suite=False,
    )

    t_path = run_dir / "transcripts.jsonl"
    rows = [json.loads(ln) for ln in t_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for row in rows:
        if row["case_id"] == "D-06" or row.get("skipped"):
            row["assertions"] = [{}]
    t_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )

    # Resume with corrupted `[{}]` assertions MUST NOT crash; logs warning & falls back to re-score
    _, runner2, _ = _make_runner_env(tmp_path / "r4_l2_corrupt_2", backend, today_box)
    with caplog.at_level("WARNING"):
        _, rep_resumed = runner2.run(
            [all_cases["D-01"], all_cases["D-06"]],
            repeats=1,
            layers=["dialogue"],
            is_full_suite=False,
            resume_dir=run_dir,
        )
    captured = capsys.readouterr()
    assert "落盘断言损坏" in captured.err
    assert any("落盘断言损坏" in rec.message for rec in caplog.records)
    assert rep_resumed["metrics"]["answer_accuracy"]["passed"] == 1
    assert rep_resumed["metrics"]["answer_accuracy"]["total"] == 2
    assert any(
        f["case"] == "D-01" and f["assertion"] == "skipped_missing_var"
        for f in rep_resumed["failures"]
    )
    # Persisted transcripts.jsonl is repaired with valid assertions
    repaired_rows = [
        json.loads(ln) for ln in t_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert all(_is_valid_saved_assertions(r.get("assertions")) for r in repaired_rows)


def test_r4_l2_resume_fingerprint_mismatch_rejudges_and_annotates_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness import asserts as asserts_mod

    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]
    judge_settings = Settings(
        judge_base_url="http://judge.test/v1",
        judge_api_key="sk-test",
        judge_model="judge-model-1",
    )

    judge_http_calls: list[str] = []

    def ok_judge_handler(request: httpx.Request) -> httpx.Response:
        judge_http_calls.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"label":"refundable","evidence":"订单 C3051 已签收"}'
                        }
                    }
                ]
            },
        )

    # Verify fingerprint sensitivity across keyword table, judge enabled, and judge model
    fp_no_judge = compute_eval_fingerprint(EVALS_DIR, None)
    j_m1 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(ok_judge_handler)),
        cache_dir=tmp_path / "fp_cache",
    )
    j_m1.enabled = True
    fp_m1 = compute_eval_fingerprint(EVALS_DIR, j_m1)
    assert fp_m1 != fp_no_judge

    j_m2 = VerdictJudge(
        Settings(
            judge_base_url="http://judge.test/v1",
            judge_api_key="sk-test",
            judge_model="judge-model-2",
        ),
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(ok_judge_handler)),
        cache_dir=tmp_path / "fp_cache",
    )
    j_m2.enabled = True
    assert compute_eval_fingerprint(EVALS_DIR, j_m2) != fp_m1

    with monkeypatch.context() as mp:
        mp.setattr(asserts_mod, "POSITIVE_KEYWORDS", (*asserts_mod.POSITIVE_KEYWORDS, "允许退"))
        assert compute_eval_fingerprint(EVALS_DIR, None) != fp_no_judge

    # Run 1: judge disabled -> D-06 r1 completes (verdict unknown -> fails M4); r2 hits 502
    b1 = FakeBackend(suite)
    b1.chat_unknown_verdict = True
    orig_chat_handler = b1.chat_handler
    turns = {"n": 0}

    def fail_second_turn(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/healthz":
            turns["n"] += 1
            if turns["n"] == 2:
                return httpx.Response(502, json={"code": "dify_error", "message": "暂不可用"})
        return orig_chat_handler(request)

    b1.chat_handler = fail_second_turn  # type: ignore[method-assign]
    _, runner1, env1 = _make_runner_env(tmp_path / "r4_l2_fp_run1", b1, today_box, judge=None)
    run_dir, rep1 = runner1.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
    )
    assert rep1["verdict"] == "incomplete"
    assert rep1["metrics"]["answer_accuracy"]["passed"] == 0
    assert rep1["metrics"]["answer_accuracy"]["total"] == 1
    meta1 = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta1["run"]["eval_fingerprint"] == fp_no_judge
    assert meta1["cost_counts"]["resets"] == env1.reset_count
    assert meta1["cost_counts"]["judge_calls"] == 0

    # Run 2 (--resume): judge is now enabled -> eval_fingerprint changed!
    # Must abandon persisted assertions, re-evaluate round 1 + round 2 with judge,
    # NOT double-count prior judge calls, and prominently annotate report.md.
    cache_dir = tmp_path / "r4_l2_fp_cache"
    judge2 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(ok_judge_handler)),
        cache_dir=cache_dir,
    )
    judge2.enabled = True
    judge_http_calls.clear()

    b2 = FakeBackend(suite)
    b2.chat_unknown_verdict = True
    _, runner2, env2 = _make_runner_env(tmp_path / "r4_l2_fp_run2", b2, today_box, judge=judge2)
    _, rep2 = runner2.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    assert rep2["verdict"] == "pass"
    assert rep2["run"]["resumed_rejudged"] is True
    assert rep2["run"]["eval_fingerprint"] == fp_m1
    assert rep2["metrics"]["answer_accuracy"]["passed"] == 2
    assert rep2["metrics"]["answer_accuracy"]["total"] == 2
    # Round 2 executed live (1 remote call, cached) and round 1 re-evaluated (1 cache hit)
    assert len(judge_http_calls) == 1
    assert rep2["cost"]["judge_calls"] == 1
    assert rep2["cost"]["judge_cache_hits"] == 1
    assert rep2["cost"]["resets"] == env1.reset_count + env2.reset_count
    md2 = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "因断言规则或评审配置变化，本次续跑已对既有记录统一重新判定" in md2

    # Run 3 (--resume): now fingerprint matches & persisted assertions are updated;
    # removing eval_fingerprint from run_meta.json also triggers full re-evaluation
    meta2 = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta2["run"]["eval_fingerprint"] == fp_m1
    meta2["run"].pop("eval_fingerprint", None)
    (run_dir / "run_meta.json").write_text(json.dumps(meta2, ensure_ascii=False), encoding="utf-8")

    judge3 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(ok_judge_handler)),
        cache_dir=cache_dir,
    )
    judge3.enabled = True
    _, runner3, _ = _make_runner_env(tmp_path / "r4_l2_fp_run3", b2, today_box, judge=judge3)
    _, rep3 = runner3.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    # Full re-evaluation on missing fingerprint uses ONLY current judge counts (no double counting!)
    assert rep3["run"]["resumed_rejudged"] is True
    assert rep3["cost"]["judge_calls"] == 0
    assert rep3["cost"]["judge_cache_hits"] == 2
    assert "因断言规则或评审配置变化，本次续跑已对既有记录统一重新判定" in (
        run_dir / "report.md"
    ).read_text(encoding="utf-8")


def test_r4_l2_resume_accumulates_all_cost_counts_on_normal_reuse(tmp_path: Path) -> None:
    suite = load_suite(EVALS_DIR)
    all_cases = {c.id: c for _, c in load_cases(EVALS_DIR)}
    today_box = [date(2026, 11, 21)]
    judge_settings = Settings(
        judge_base_url="http://judge.test/v1",
        judge_api_key="sk-test",
        judge_model="judge-model-cost",
    )

    mode = {"invalid": True}

    def judge_handler(request: httpx.Request) -> httpx.Response:
        if mode["invalid"]:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not-json"}}]},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"label":"refundable","evidence":"订单 C3051 已签收"}'
                        }
                    }
                ]
            },
        )

    cache_dir1 = tmp_path / "cost_cache_1"
    judge1 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(judge_handler)),
        cache_dir=cache_dir1,
    )
    judge1.enabled = True
    judge1.calibration_call_count = 12

    b1 = FakeBackend(suite)
    b1.chat_unknown_verdict = True
    orig_chat_handler = b1.chat_handler
    turns = {"n": 0}

    def fail_on_second(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/healthz":
            turns["n"] += 1
            if turns["n"] == 2:
                return httpx.Response(502, json={"code": "dify_error", "message": "暂不可用"})
        return orig_chat_handler(request)

    b1.chat_handler = fail_on_second  # type: ignore[method-assign]
    _, runner1, env1 = _make_runner_env(tmp_path / "r4_l2_cost1", b1, today_box, judge=judge1)
    run_dir, rep1 = runner1.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
    )
    assert rep1["verdict"] == "incomplete"
    assert rep1["cost"]["judge_calibration_calls"] == 12
    assert rep1["cost"]["judge_calls"] == 1
    assert rep1["cost"]["judge_invalid"] == 1
    assert rep1["cost"]["judge_cache_hits"] == 0
    assert rep1["cost"]["resets"] == env1.reset_count
    assert rep1["breakdown"]["sub_metrics"]["verdict_accuracy"]["llm_invalid"] == 1

    # Resume with same eval_fingerprint (same judge model & enabled):
    # round 1 reused (keeps invalid verdict), round 2 calls judge (valid) -> costs accumulate!
    mode["invalid"] = False
    cache_dir2 = tmp_path / "cost_cache_2"
    judge2 = VerdictJudge(
        judge_settings,
        evals_dir=EVALS_DIR,
        http_client=httpx.Client(transport=httpx.MockTransport(judge_handler)),
        cache_dir=cache_dir2,
    )
    judge2.enabled = True
    judge2.calibration_call_count = 12

    b2 = FakeBackend(suite)
    b2.chat_unknown_verdict = True
    _, runner2, env2 = _make_runner_env(tmp_path / "r4_l2_cost2", b2, today_box, judge=judge2)
    _, rep2 = runner2.run(
        [all_cases["D-06"]],
        repeats=2,
        layers=["dialogue"],
        is_full_suite=False,
        resume_dir=run_dir,
    )
    assert rep2["run"].get("resumed_rejudged") is not True
    assert rep2["cost"]["judge_calibration_calls"] == 24
    assert rep2["cost"]["judge_calls"] == 2
    assert rep2["cost"]["judge_invalid"] == 1
    assert rep2["cost"]["judge_cache_hits"] == 0
    assert rep2["cost"]["resets"] == env1.reset_count + env2.reset_count
    # Cost invalid count matches sub_metrics.verdict_accuracy.llm_invalid!
    assert rep2["breakdown"]["sub_metrics"]["verdict_accuracy"]["llm_invalid"] == 1
    assert rep2["breakdown"]["sub_metrics"]["verdict_accuracy"]["llm_judged"] == 1
    meta2 = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta2["cost_counts"] == {
        "judge_calibration_calls": 24,
        "judge_calls": 2,
        "judge_cache_hits": 0,
        "judge_invalid": 1,
        "resets": env1.reset_count + env2.reset_count,
    }
