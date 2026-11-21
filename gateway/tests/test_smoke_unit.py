from __future__ import annotations

import ast
import base64
import http.client
import json
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import smoke

GATEWAY_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = GATEWAY_DIR.parent

USER_META: dict[str, dict[str, Any]] = {
    "wang": {"sub": "u1001", "role": "agent", "regions": {"east"}},
    "zhao": {"sub": "u1002", "role": "agent", "regions": {"north"}},
    "chen": {"sub": "u1003", "role": "agent", "regions": {"south"}},
    "li": {"sub": "u2001", "role": "supervisor", "regions": {"east"}},
    "zhou": {"sub": "u2002", "role": "supervisor", "regions": {"north", "south"}},
    "sun": {"sub": "u2003", "role": "supervisor", "regions": {"east", "north", "south"}},
}


def test_smoke_uses_only_stdlib() -> None:
    """PROPOSAL 2.4 requires smoke.py to depend only on the Python standard library."""
    tree = ast.parse((GATEWAY_DIR / "scripts/smoke.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    non_stdlib = imported - sys.stdlib_module_names
    assert non_stdlib == set(), f"smoke.py imported non-stdlib modules: {non_stdlib}"


def test_registered_checks_are_v1_through_v34() -> None:
    assert [c.id for c in smoke.CHECKS] == [f"V{i}" for i in range(1, 35)]


def test_parse_only_selector() -> None:
    all_checks = smoke.parse_only_selector(None, smoke.CHECKS)
    assert len(all_checks) == 34

    v1_v7 = smoke.parse_only_selector("V1-V7", smoke.CHECKS)
    assert [c.id for c in v1_v7] == [f"V{i}" for i in range(1, 8)]

    v18_v34 = smoke.parse_only_selector("V18-V34", smoke.CHECKS)
    assert [c.id for c in v18_v34] == [f"V{i}" for i in range(18, 35)]

    mixed = smoke.parse_only_selector("v10,V1,v5-v6,V34", smoke.CHECKS)
    assert [c.id for c in mixed] == ["V1", "V5", "V6", "V10", "V34"]

    with pytest.raises(ValueError):
        smoke.parse_only_selector("V7-V1", smoke.CHECKS)
    with pytest.raises(ValueError):
        smoke.parse_only_selector("V99", smoke.CHECKS)
    with pytest.raises(ValueError):
        smoke.parse_only_selector("invalid", smoke.CHECKS)


def test_parse_tool_error() -> None:
    raw = (
        'call failed, status: 403, response: {"code":"order_access_denied",'
        '"message":"无权访问","request_id":"req-123"}'
    )
    info = smoke.parse_tool_error(raw)
    assert info.status == 403
    assert info.error_body == {
        "code": "order_access_denied",
        "message": "无权访问",
        "request_id": "req-123",
    }

    multiline = "call failed, status: 503, response: upstream connect error\nline2"
    info2 = smoke.parse_tool_error(multiline)
    assert info2.status == 503
    assert info2.error_body is None
    assert "line2" in info2.raw_response

    with pytest.raises(smoke.CheckFailure):
        smoke.parse_tool_error("unexpected message")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def test_tamper_jwt() -> None:
    tampered = smoke.tamper_jwt("abc.def.0123456789XYZ")
    head, payload, sig = tampered.split(".")
    assert head == "abc"
    assert payload == "def"
    assert sig != "0123456789XYZ"
    assert sig[-1] == "Z"  # 不修改末字符

    # 待改位原本是 'A' 时切换为 'B'
    all_a_sig = "A" * 30
    tampered_a = smoke.tamper_jwt(f"hdr.pay.{all_a_sig}")
    assert tampered_a.split(".")[2] != all_a_sig
    assert "B" in tampered_a.split(".")[2]

    for bad in ("", "only-one-part", "a.b", "a.b."):
        with pytest.raises(smoke.CheckFailure):
            smoke.tamper_jwt(bad)


@pytest.mark.parametrize(
    ("tail_byte", "expected_tail_char"),
    [
        (0x00, "A"),
        (0x01, "Q"),
        (0x02, "g"),
        (0x03, "w"),
    ],
)
def test_tamper_jwt_changes_decoded_bytes_for_all_rs256_tail_chars(
    tail_byte: int,
    expected_tail_char: str,
) -> None:
    # 构造 256 字节 RS256 签名，末字符覆盖 A / Q / g / w 四种合法取值
    raw_sig = bytes([0x5A] * 255 + [tail_byte])
    sig_b64 = base64.urlsafe_b64encode(raw_sig).rstrip(b"=").decode("ascii")
    assert len(sig_b64) == 342
    assert sig_b64[-1] == expected_tail_char

    token = f"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MTAwMSJ9.{sig_b64}"
    tampered = smoke.tamper_jwt(token)

    orig_parts = token.split(".")
    tampered_parts = tampered.split(".")
    assert tampered_parts[:2] == orig_parts[:2]
    assert tampered_parts[2][-1] == expected_tail_char
    assert len(tampered_parts[2]) == len(sig_b64)

    decoded_tampered = _b64url_decode(tampered_parts[2])
    assert len(decoded_tampered) == 256
    assert decoded_tampered != raw_sig


class FakeSmokeContext(smoke.SmokeContext):
    """In-memory mock of Higress + guard + mock-api + dev-idp for testing V1–V34."""

    def __init__(self, manifest_path: Path, *, approvals_total: int = 0) -> None:
        super().__init__(repo_root=REPO_ROOT, manifest_path=manifest_path)
        self.initial_approvals_total = approvals_total
        self.v4_returns_200 = False
        self.clean_check_calls = 0

        self.orders: dict[str, dict[str, Any]] = {
            "A1023": {
                "order_id": "A1023",
                "region": "east",
                "status": "delivered",
                "refunded_amount": 0,
                "refunds": [],
            },
            "A1024": {
                "order_id": "A1024",
                "region": "east",
                "status": "delivered",
                "refunded_amount": 0,
                "refunds": [],
            },
            "A1029": {
                "order_id": "A1029",
                "region": "east",
                "status": "pending_payment",
                "refunded_amount": 0,
                "refunds": [],
            },
            "A1033": {
                "order_id": "A1033",
                "region": "east",
                "status": "delivered",
                "refunded_amount": 0,
                "refunds": [],
            },
            "B2087": {
                "order_id": "B2087",
                "region": "north",
                "status": "delivered",
                "refunded_amount": 0,
                "refunds": [],
            },
        }
        self.approvals: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self._ap_seq = 0
        self._rf_seq = 104

    def mint(
        self,
        user: str,
        *,
        ttl: int | None = None,
        audience: str | None = None,
    ) -> str:
        tok = f"hdr.{user}_ttl{ttl}_aud{audience}.signaturesecret{user}"
        self.used_tokens.append(tok)
        return tok

    def _user_from_token(self, token: str) -> tuple[str, dict[str, Any]]:
        for uname, meta in USER_META.items():
            if f".{uname}_" in token:
                return uname, meta
        raise AssertionError(f"Unknown mock token: {token}")

    def _add_audit(self, **kwargs: Any) -> None:
        ev = {"id": len(self.audits) + 1, **kwargs}
        self.audits.append(ev)

    def run_cmd(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        req_id = self.state.get("v9_request_id", "")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=f"log {req_id}\n", stderr=""
        )

    def http_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> smoke.HttpResponse:
        headers = headers or {}
        auth = headers.get("Authorization", "")

        # Direct mock-api call (V17)
        if url.startswith(self.mock_api_url):
            if "ttl-300" in auth:
                return smoke.HttpResponse(
                    401,
                    {},
                    json.dumps({"code": "token_expired", "message": "过期", "request_id": "r-17"}),
                )
            return smoke.HttpResponse(200, {}, json.dumps({"order_id": "A1023"}))

        # Direct guard calls (V22–V34)
        if url.startswith(self.guard_url):
            return self._handle_guard_http(method, url, auth, json_body)

        # MCP endpoint auth checks (V1-V4)
        if not auth.startswith("Bearer "):
            return smoke.HttpResponse(401, {}, "Jwt is missing")
        token = auth.removeprefix("Bearer ")
        if "ttl-300" in token:
            return smoke.HttpResponse(401, {}, "Jwt is expired")
        if token not in self.used_tokens:
            return smoke.HttpResponse(401, {}, "Jwt verification fails")
        if "audother" in token and not self.v4_returns_200:
            return smoke.HttpResponse(401, {}, "Jwt claim mismatch")

        # V6: non-POST
        if method != "POST":
            return smoke.HttpResponse(405, {}, "Method Not Allowed")

        assert isinstance(json_body, dict)
        rpc_method = json_body.get("method")
        rpc_id = json_body.get("id")

        if rpc_method == "initialize":
            return smoke.HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {"serverInfo": {"name": "aftersale", "version": "1.0"}},
                    }
                ),
            )
        if rpc_method == "notifications/initialized":
            return smoke.HttpResponse(202, {}, "")
        if rpc_method == "tools/list":
            assert self.manifest_path is not None
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            tools = []
            for item in manifest:
                desc = (
                    "提交退款申请【写操作】 - 需要主管审批"
                    if item["name"] == "create_refund"
                    else f"tool {item['name']}"
                )
                tools.append({"name": item["name"], "description": desc})
            return smoke.HttpResponse(
                200,
                {},
                json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools}}),
            )
        if rpc_method == "tools/call":
            params = json_body["params"]
            name = params["name"]
            args = params.get("arguments") or {}
            return self._handle_tool_call(rpc_id, token, name, args)

        raise AssertionError(f"Unexpected RPC method: {rpc_method}")

    def _json_resp(self, status: int, data: Any) -> smoke.HttpResponse:
        return smoke.HttpResponse(
            status,
            {"content-type": "application/json"},
            json.dumps(data, ensure_ascii=False),
        )

    def _handle_guard_http(
        self,
        method: str,
        url: str,
        auth: str,
        json_body: Any | None,
    ) -> smoke.HttpResponse:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if method == "GET" and path == "/console/":
            return smoke.HttpResponse(
                200, {"content-type": "text/html; charset=utf-8"}, "<html></html>"
            )
        if method == "POST" and path == "/guard/v1/console/login":
            assert isinstance(json_body, dict)
            if json_body.get("username") == "li" and json_body.get("password") == "dev-password":
                return self._json_resp(
                    200,
                    {
                        "access_token": self.mint("li"),
                        "token_type": "Bearer",
                        "expires_in": 28800,
                    },
                )
            return self._json_resp(
                401, {"code": "login_failed", "message": "bad", "request_id": "r"}
            )

        if not auth.startswith("Bearer "):
            return self._json_resp(
                401, {"code": "missing_token", "message": "missing", "request_id": "r"}
            )
        token = auth.removeprefix("Bearer ")
        uname, user = self._user_from_token(token)

        if method == "GET" and path == "/openapi.json":
            return self._json_resp(
                404, {"code": "route_not_found", "message": "not found", "request_id": "r"}
            )

        if method == "GET" and path == "/guard/v1/console/approvals":
            if user["role"] != "supervisor":
                return self._json_resp(
                    403, {"code": "insufficient_role", "message": "denied", "request_id": "r"}
                )
            vis = [a for a in self.approvals.values() if a["region"] in user["regions"]]
            return self._json_resp(
                200, {"items": vis, "total": len(vis), "page": 1, "page_size": 20}
            )

        if method == "POST" and path.startswith("/guard/v1/console/approvals/"):
            parts = path.strip("/").split("/")
            ap_id, action = parts[4], parts[5]
            ap = self.approvals.get(ap_id)
            if user["role"] != "supervisor":
                if ap is not None:
                    self._add_audit(
                        event_type=f"approval_{action}",
                        actor_sub=user["sub"],
                        actor_regions=",".join(sorted(user["regions"])),
                        tool="create_refund",
                        outcome="access_denied",
                        status_code=403,
                        approval_id=ap_id,
                    )
                return self._json_resp(
                    403, {"code": "insufficient_role", "message": "denied", "request_id": "r"}
                )
            if ap is None:
                return self._json_resp(
                    404, {"code": "approval_not_found", "message": "404", "request_id": "r"}
                )
            if action == "reject":
                comment = (
                    (json_body or {}).get("comment", "") if isinstance(json_body, dict) else ""
                )
                if not isinstance(comment, str) or not comment.strip():
                    return self._json_resp(
                        422, {"code": "validation_error", "message": "必填", "request_id": "r"}
                    )
            if ap["region"] not in user["regions"]:
                self._add_audit(
                    event_type=f"approval_{action}",
                    actor_sub=user["sub"],
                    actor_regions=",".join(sorted(user["regions"])),
                    tool="create_refund",
                    outcome="access_denied",
                    status_code=403,
                    approval_id=ap_id,
                )
                return self._json_resp(
                    403, {"code": "approval_access_denied", "message": "denied", "request_id": "r"}
                )
            if user["sub"] == ap["applicant_sub"]:
                return self._json_resp(
                    403, {"code": "self_approval_forbidden", "message": "self", "request_id": "r"}
                )
            if ap["status"] != "pending":
                self._add_audit(
                    event_type=f"approval_{action}",
                    actor_sub=user["sub"],
                    actor_regions=",".join(sorted(user["regions"])),
                    tool="create_refund",
                    outcome="conflict",
                    status_code=409,
                    approval_id=ap_id,
                    resource_region=ap["region"],
                )
                return self._json_resp(
                    409,
                    {"code": "approval_not_pending", "message": "not pending", "request_id": "r"},
                )
            if action == "reject":
                comment = json_body["comment"].strip()
                ap["status"] = "rejected"
                ap["message"] = f"主管已拒绝退款申请 {ap_id}：{comment}。退款未执行。"
                return self._json_resp(200, ap)
            # approve
            self._add_audit(
                event_type="approval_approve",
                actor_sub=user["sub"],
                actor_regions=",".join(sorted(user["regions"])),
                tool="create_refund",
                outcome="success",
                status_code=200,
                approval_id=ap_id,
                resource_region=ap["region"],
                result={"from": "pending", "to": "executing"},
            )
            order = self.orders[ap["order_id"]]
            if order["status"] == "pending_payment":
                ap["status"] = "execution_failed"
                ap["failure_code"] = "order_not_refundable"
                ap["message"] = f"主管已同意退款申请 {ap_id}，但订单系统拒绝执行：订单尚未支付。"
                return self._json_resp(200, ap)
            self._rf_seq += 1
            rf_id = f"R{self._rf_seq:06d}"
            rf = {
                "refund_id": rf_id,
                "order_id": ap["order_id"],
                "region": ap["region"],
                "amount": ap["amount"],
                "created_by": user["sub"],
                "created_by_role": user["role"],
            }
            self.refunds[rf_id] = rf
            order["refunded_amount"] += ap["amount"]
            order["refunds"].append(rf)
            ap["status"] = "executed"
            ap["refund_id"] = rf_id
            ap["message"] = f"主管已同意退款申请 {ap_id}，退款已执行，退款单号 {rf_id}。"
            self._add_audit(
                event_type="approval_execute",
                actor_sub=user["sub"],
                actor_regions=",".join(sorted(user["regions"])),
                tool="create_refund",
                outcome="success",
                status_code=201,
                approval_id=ap_id,
                resource_region=ap["region"],
                on_behalf_of_sub=ap["applicant_sub"],
                result={"from": "executing", "to": "executed", "refund_id": rf_id},
            )
            return self._json_resp(200, ap)

        if method == "GET" and path == "/guard/v1/console/audit-logs":
            if user["role"] != "supervisor":
                return self._json_resp(
                    403, {"code": "insufficient_role", "message": "denied", "request_id": "r"}
                )
            matched = []
            for ev in reversed(self.audits):
                ev_regions = set(ev.get("actor_regions", "").split(",")) - {""}
                if ev.get("resource_region") not in user["regions"] and not (
                    ev_regions & user["regions"]
                ):
                    continue
                if "request_id" in qs and ev.get("request_id") != qs["request_id"][0]:
                    continue
                if "approval_id" in qs and ev.get("approval_id") != qs["approval_id"][0]:
                    continue
                if "actor" in qs and ev.get("actor_sub") != qs["actor"][0]:
                    continue
                matched.append(ev)
            return self._json_resp(
                200,
                {"items": matched, "total": len(matched), "page": 1, "page_size": 100},
            )

        raise AssertionError(f"Unhandled guard request: {method} {url}")

    def _ok(self, rpc_id: Any, structured: dict[str, Any]) -> smoke.HttpResponse:
        return smoke.HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": json.dumps(structured)}],
                        "structuredContent": structured,
                    },
                }
            ),
        )

    def _err(
        self, rpc_id: Any, status: int, code: str, message: str = "err", request_id: str = "req-001"
    ) -> smoke.HttpResponse:
        err_json = json.dumps({"code": code, "message": message, "request_id": request_id})
        return smoke.HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": f"call failed, status: {status}, response: {err_json}",
                            }
                        ],
                    },
                }
            ),
        )

    def _handle_tool_call(
        self, rpc_id: Any, token: str, name: str, args: dict[str, Any]
    ) -> smoke.HttpResponse:
        _, user = self._user_from_token(token)
        if name == "list_approvals":
            if not args:
                self.clean_check_calls += 1
                total = self.initial_approvals_total + len(self.approvals)
                return self._ok(rpc_id, {"items": [], "total": total})
            oid = args.get("order_id")
            items = [
                a
                for a in self.approvals.values()
                if a["region"] in user["regions"] and (oid is None or a["order_id"] == oid)
            ]
            return self._ok(rpc_id, {"items": items, "total": len(items)})
        if name == "get_approval":
            ap_id = args["approval_id"]
            ap = self.approvals.get(ap_id)
            if ap is None:
                return self._err(rpc_id, 404, "approval_not_found")
            if ap["region"] not in user["regions"]:
                return self._err(rpc_id, 403, "approval_access_denied")
            return self._ok(rpc_id, ap)
        if name == "get_order":
            oid = args.get("order_id")
            order = self.orders.get(str(oid))
            if order is None:
                return self._err(rpc_id, 404, "order_not_found")
            if order["region"] not in user["regions"]:
                self._add_audit(
                    event_type="tool_call",
                    request_id="req-v9-trace",
                    tool="get_order",
                    actor_sub=user["sub"],
                    actor_regions=",".join(sorted(user["regions"])),
                    outcome="access_denied",
                    status_code=403,
                    arguments={"order_id": oid},
                )
                return self._err(rpc_id, 403, "order_access_denied", request_id="req-v9-trace")
            return self._ok(rpc_id, order)
        if name == "get_refund":
            rf = self.refunds.get(args["refund_id"])
            if rf is None:
                return self._err(rpc_id, 404, "refund_not_found")
            return self._ok(rpc_id, rf)
        if name == "list_refunds":
            if user["role"] != "supervisor":
                return self._err(rpc_id, 403, "insufficient_role")
            return self._ok(rpc_id, {"items": [], "total": 0})
        if name == "list_orders":
            if args.get("page_size", 20) > 50:
                return self._err(rpc_id, 422, "validation_error")
            return self._ok(rpc_id, {"items": [], "total": 0})
        if name == "create_refund":
            if float(args.get("amount", 0)) <= 0:
                return self._err(rpc_id, 422, "validation_error")
            oid = args["order_id"]
            order = self.orders.get(oid)
            if order is None:
                return self._err(rpc_id, 404, "order_not_found")
            if order["region"] not in user["regions"]:
                return self._err(rpc_id, 403, "order_access_denied")
            active = next(
                (
                    a
                    for a in self.approvals.values()
                    if a["order_id"] == oid and a["status"] == "pending"
                ),
                None,
            )
            if active is not None:
                return self._err(
                    rpc_id,
                    409,
                    "approval_pending_exists",
                    message=f"已有审批单 {active['approval_id']}",
                )
            self._ap_seq += 1
            ap_id = f"AP{self._ap_seq:06d}"
            ap = {
                "approval_id": ap_id,
                "status": "pending",
                "order_id": oid,
                "region": order["region"],
                "amount": args["amount"],
                "applicant_sub": user["sub"],
                "message": f"已提交退款申请 {ap_id}，等待主管审批。退款尚未执行。",
            }
            self.approvals[ap_id] = ap
            self._add_audit(
                event_type="tool_call",
                tool="create_refund",
                actor_sub=user["sub"],
                actor_regions=",".join(sorted(user["regions"])),
                outcome="pending_approval",
                status_code=202,
                approval_id=ap_id,
                resource_region=order["region"],
            )
            return self._ok(rpc_id, ap)
        raise AssertionError(f"Unhandled tool_call: {name} {args}")


def test_smoke_v1_through_v34_happy_path() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    ctx = FakeSmokeContext(manifest_path)
    passed, failed = smoke.run_checks(ctx, smoke.CHECKS)
    assert passed == 34
    assert failed == 0
    assert ctx.clean_check_calls == 1
    assert ctx.state["v9_request_id"] == "req-v9-trace"
    assert ctx.state["v14_approval_id"] == "AP000001"


def test_smoke_v1_to_v7_skips_clean_guard_check() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    ctx = FakeSmokeContext(manifest_path, approvals_total=5)
    selected = smoke.parse_only_selector("V1-V7", smoke.CHECKS)
    passed, failed = smoke.run_checks(ctx, selected)
    assert passed == 7
    assert failed == 0
    assert ctx.clean_check_calls == 0


def test_smoke_precheck_fails_when_guard_not_clean() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    ctx = FakeSmokeContext(manifest_path, approvals_total=2)
    selected = smoke.parse_only_selector("V8-V9", smoke.CHECKS)
    passed, failed = smoke.run_checks(ctx, selected)
    assert passed == 0
    assert failed == 1


def test_smoke_v4_detects_broken_aud_check() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    ctx = FakeSmokeContext(manifest_path)
    ctx.v4_returns_200 = True
    selected = smoke.parse_only_selector("V4", smoke.CHECKS)
    passed, failed = smoke.run_checks(ctx, selected)
    assert passed == 0
    assert failed == 1


def test_smoke_v16_standalone_probes_v9_first() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    ctx = FakeSmokeContext(manifest_path)
    selected = smoke.parse_only_selector("V16", smoke.CHECKS)
    passed, failed = smoke.run_checks(ctx, selected)
    assert passed == 1
    assert failed == 0
    assert ctx.state["v9_request_id"] == "req-v9-trace"


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: TimeoutError("timed out"),
        lambda: ConnectionResetError("connection reset by peer"),
        lambda: OSError("network unreachable"),
        lambda: http.client.IncompleteRead(b"partial"),
    ],
)
def test_http_request_converts_transport_errors_to_check_failure(
    exc_factory: Any,
) -> None:
    ctx = smoke.SmokeContext(repo_root=REPO_ROOT)
    with patch("smoke.urllib.request.urlopen", side_effect=exc_factory()):
        with pytest.raises(smoke.CheckFailure):
            ctx.http_request("GET", "http://127.0.0.1:8080/aftersale/mcp")


def test_http_request_converts_http_error_read_timeout_to_check_failure() -> None:
    ctx = smoke.SmokeContext(repo_root=REPO_ROOT)
    fake_err = urllib.error.HTTPError(
        url="http://127.0.0.1:8080/aftersale/mcp",
        code=502,
        msg="Bad Gateway",
        hdrs=http.client.HTTPMessage(),
        fp=None,
    )
    fake_err.read = MagicMock(side_effect=TimeoutError("read error body timed out"))  # type: ignore[method-assign]
    with patch("smoke.urllib.request.urlopen", side_effect=fake_err):
        with pytest.raises(smoke.CheckFailure, match="HTTP 502"):
            ctx.http_request("POST", "http://127.0.0.1:8080/aftersale/mcp")


def test_precheck_transport_failure_exits_cleanly_with_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"

    class TimeoutPrecheckContext(FakeSmokeContext):
        def http_request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json_body: Any | None = None,
        ) -> smoke.HttpResponse:
            raise smoke.CheckFailure(f"请求 {method} {url} 传输失败: timed out")

    ctx = TimeoutPrecheckContext(manifest_path)
    with patch("smoke.SmokeContext", return_value=ctx):
        rc = smoke.main(["--only", "V8"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "[PRECHECK FAIL]" in captured.err
    assert "Traceback" not in captured.err
    assert "完成：通过 0 项，失败 1 项。" in captured.out


def test_smoke_v16_retries_until_logs_appear_without_real_sleep() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    sleeps: list[float] = []
    higress_calls = 0
    mock_api_calls = 0

    class DelayedLogContext(FakeSmokeContext):
        def run_cmd(
            self,
            args: list[str],
            *,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal higress_calls, mock_api_calls
            req_id = self.state.get("v9_request_id", "")
            if any("gateway.log" in a for a in args):
                higress_calls += 1
                if higress_calls < 3:
                    return subprocess.CompletedProcess(
                        args=args, returncode=1, stdout="", stderr=""
                    )
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=f"hit {req_id}\n", stderr=""
                )
            mock_api_calls += 1
            if mock_api_calls < 2:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=f"hit {req_id}\n", stderr=""
            )

    ctx = DelayedLogContext(manifest_path)
    ctx.sleep_fn = sleeps.append
    selected = smoke.parse_only_selector("V16", smoke.CHECKS)
    passed, failed = smoke.run_checks(ctx, selected)
    assert passed == 1
    assert failed == 0
    assert higress_calls == 3
    assert mock_api_calls == 2
    assert sleeps == [1.0, 1.0, 1.0]


def test_smoke_v16_fails_after_five_attempts() -> None:
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    sleeps: list[float] = []
    higress_calls = 0

    class MissingLogContext(FakeSmokeContext):
        def run_cmd(
            self,
            args: list[str],
            *,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal higress_calls
            if any("gateway.log" in a for a in args):
                higress_calls += 1
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            return super().run_cmd(args, check=check)

    ctx = MissingLogContext(manifest_path)
    ctx.sleep_fn = sleeps.append
    selected = smoke.parse_only_selector("V16", smoke.CHECKS)
    passed, failed = smoke.run_checks(ctx, selected)
    assert passed == 0
    assert failed == 1
    assert higress_calls == 5
    assert sleeps == [1.0, 1.0, 1.0, 1.0]
