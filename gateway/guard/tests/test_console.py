from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import reset
from app.config import Settings
from tests.conftest import FakeUpstream


def test_console_static_pages_and_config_js(
    build_app: Callable[..., FastAPI],
    settings: Settings,
) -> None:
    # With dev login configured
    app1 = build_app(custom_settings=settings, custom_upstream=FakeUpstream())
    with TestClient(app1) as tc:
        r_index = tc.get("/console/")
        assert r_index.status_code == 200
        assert "text/html" in r_index.headers["content-type"]
        assert "MCPGate" in r_index.text

        r_audit = tc.get("/console/audit.html")
        assert r_audit.status_code == 200
        assert "text/html" in r_audit.headers["content-type"]

        r_cfg = tc.get("/console/config.js")
        assert r_cfg.status_code == 200
        assert "DEV_LOGIN_ENABLED = true" in r_cfg.text

    # Without dev login configured
    cfg_no_login = settings.model_copy(update={"dev_login_url": ""})
    app2 = build_app(custom_settings=cfg_no_login, custom_upstream=FakeUpstream())
    with TestClient(app2) as tc:
        r_cfg = tc.get("/console/config.js")
        assert r_cfg.status_code == 200
        assert "DEV_LOGIN_ENABLED = false" in r_cfg.text


def test_console_dev_login_success_failure_and_not_configured(
    build_app: Callable[..., FastAPI],
    settings: Settings,
) -> None:
    up = FakeUpstream()

    def _idp_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            body = req.read().decode("utf-8")
            if '"li"' in body and '"dev-password"' in body:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "jwt-from-idp",
                        "token_type": "Bearer",
                        "expires_in": 28800,
                    },
                )
            return httpx.Response(401, json={"code": "invalid_credentials", "message": "bad"})
        return httpx.Response(500)

    up.handler = _idp_handler
    app = build_app(custom_settings=settings, custom_upstream=up)
    with TestClient(app) as tc:
        # Success
        r_ok = tc.post(
            "/guard/v1/console/login",
            json={"username": "li", "password": "dev-password"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json() == {
            "access_token": "jwt-from-idp",
            "token_type": "Bearer",
            "expires_in": 28800,
        }

        # Wrong password -> 401 login_failed
        r_bad = tc.post(
            "/guard/v1/console/login",
            json={"username": "li", "password": "wrong"},
        )
        assert r_bad.status_code == 401
        assert r_bad.json()["code"] == "login_failed"

        # Upstream unreachable -> 503
        up.handler = lambda _req: (_ for _ in ()).throw(httpx.ConnectError("down"))
        r_down = tc.post(
            "/guard/v1/console/login",
            json={"username": "li", "password": "dev-password"},
        )
        assert r_down.status_code == 503
        assert r_down.json()["code"] == "upstream_unavailable"

    # Not configured -> 404 login_not_configured
    cfg_no_login = settings.model_copy(update={"dev_login_url": ""})
    app_no_login = build_app(custom_settings=cfg_no_login, custom_upstream=FakeUpstream())
    with TestClient(app_no_login) as tc:
        r_nc = tc.post(
            "/guard/v1/console/login",
            json={"username": "li", "password": "dev-password"},
        )
        assert r_nc.status_code == 404
        assert r_nc.json()["code"] == "login_not_configured"


def test_console_list_detail_no_audit_but_order_status_audited(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Submit one approval (1 audit event)
    ap = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 100,
            "reason_category": "quality_issue",
            "reason": "损坏",
        },
        headers=auth_headers("wang"),
    ).json()
    ap_id = ap["approval_id"]

    # Non-supervisor blocked from console list / detail / order
    assert (
        client.get("/guard/v1/console/approvals", headers=auth_headers("wang")).status_code == 403
    )
    assert (
        client.get(
            f"/guard/v1/console/approvals/{ap_id}",
            headers=auth_headers("wang"),
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/guard/v1/console/approvals/{ap_id}/order",
            headers=auth_headers("wang"),
        ).status_code
        == 403
    )

    # Console list (with multi-value & comma status filters) and detail DO NOT add audit events
    r_list = client.get(
        "/guard/v1/console/approvals?status=pending&status=executing",
        headers=auth_headers("li"),
    )
    assert r_list.status_code == 200
    assert r_list.json()["total"] == 1
    assert r_list.json()["items"][0]["applicant_username"] == "wang"
    assert r_list.json()["items"][0]["applicant_role"] == "agent"

    r_list_comma = client.get(
        "/guard/v1/console/approvals?status=pending,executing&order_id=A1023",
        headers=auth_headers("li"),
    )
    assert r_list_comma.status_code == 200
    assert r_list_comma.json()["total"] == 1

    assert (
        client.get(
            "/guard/v1/console/approvals?status=invalid_status",
            headers=auth_headers("li"),
        ).status_code
        == 422
    )

    r_detail = client.get(f"/guard/v1/console/approvals/{ap_id}", headers=auth_headers("li"))
    assert r_detail.status_code == 200
    assert r_detail.json()["applicant_regions"] == "east"
    assert len(r_detail.json()["events"]) == 1

    # Audit count is STILL 1 (list/detail/audit-logs themselves are not audited)
    assert (
        client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()["total"] == 1
    )

    # Console live order status IS audited with channel=console, tool=get_order, approval_id
    li_headers = auth_headers("li")
    r_order = client.get(
        f"/guard/v1/console/approvals/{ap_id}/order",
        headers=li_headers,
    )
    assert r_order.status_code == 200
    assert r_order.json()["order_id"] == "A1023"
    assert fake_upstream.requests[-1].headers["authorization"] == li_headers["Authorization"]
    assert fake_upstream.requests[-1].raw_path == "/api/v1/orders/A1023"

    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs["total"] == 2
    order_ev = logs["items"][0]
    assert order_ev["event_type"] == "tool_call"
    assert order_ev["channel"] == "console"
    assert order_ev["tool"] == "get_order"
    assert order_ev["approval_id"] == ap_id
    assert order_ev["resource_region"] == "east"

    # Console order status when upstream unavailable -> 503 & audited
    fake_upstream.handler = lambda _req: (_ for _ in ()).throw(httpx.ReadTimeout("timeout"))
    r_order_503 = client.get(
        f"/guard/v1/console/approvals/{ap_id}/order",
        headers=auth_headers("li"),
    )
    assert r_order_503.status_code == 503
    logs_after = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs_after["total"] == 3
    assert logs_after["items"][0]["status_code"] == 503


def test_reset_cli(
    tmp_path: Path,
    build_app: Callable[..., FastAPI],
    settings: Settings,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    db_file = tmp_path / "guard.db"
    db_url = f"sqlite:///{db_file}"

    # Without --yes -> exits with 2
    assert reset.main(["--database-url", db_url]) == 2

    cfg = settings.model_copy(update={"database_url": db_url})
    app = build_app(custom_settings=cfg, custom_upstream=FakeUpstream())
    with TestClient(app) as tc:
        r = tc.post(
            "/api/v1/refunds",
            json={
                "order_id": "A1023",
                "amount": 10,
                "reason_category": "other",
                "reason": "test",
            },
            headers=auth_headers("wang"),
        )
        assert r.status_code == 202
        assert r.json()["approval_id"] == "AP000001"

    # Reset with --yes
    assert reset.main(["--yes", "--database-url", db_url]) == 0

    # After reset, first approval is AP000001 again and audit is empty
    app2 = build_app(custom_settings=cfg, custom_upstream=FakeUpstream())
    with TestClient(app2) as tc:
        assert (
            tc.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()["total"] == 0
        )
        r2 = tc.post(
            "/api/v1/refunds",
            json={
                "order_id": "A1023",
                "amount": 10,
                "reason_category": "other",
                "reason": "after reset",
            },
            headers=auth_headers("wang"),
        )
        assert r2.status_code == 202
        assert r2.json()["approval_id"] == "AP000001"


def test_http_exception_handler_405_404_and_other_status_codes(
    build_app: Callable[..., FastAPI],
    settings: Settings,
) -> None:
    app = build_app(custom_settings=settings, custom_upstream=FakeUpstream())

    @app.get("/_test_teapot")
    def _teapot() -> None:
        raise StarletteHTTPException(status_code=418, detail="I am a teapot")

    # Place test route ahead of the catch-all proxy router
    app.router.routes.insert(0, app.router.routes.pop())

    with TestClient(app) as tc:
        # 405 on static console asset preserves Allow header and returns method_not_allowed
        r_405_static = tc.put("/console/index.html")
        assert r_405_static.status_code == 405
        assert "GET" in r_405_static.headers.get("allow", "")
        b_static = r_405_static.json()
        assert b_static["code"] == "method_not_allowed"
        assert b_static["message"] == "当前接口不支持该请求方法。"
        assert b_static["request_id"] == r_405_static.headers["X-Request-Id"]

        # 405 on /healthz with disallowed method
        r_405_health = tc.request("TRACE", "/healthz")
        assert r_405_health.status_code == 405
        assert "GET" in r_405_health.headers.get("allow", "")
        assert r_405_health.json()["code"] == "method_not_allowed"
        assert r_405_health.json()["message"] == "当前接口不支持该请求方法。"

        # 404 keeps route_not_found semantics
        r_404 = tc.get("/console/does-not-exist.js")
        assert r_404.status_code == 404
        assert r_404.json()["code"] == "route_not_found"
        assert r_404.json()["message"] == "请求的接口不存在或未对外开放。"

        # Unregistered HTTP status uses fixed Chinese message and never echoes English detail
        r_418 = tc.get("/_test_teapot")
        assert r_418.status_code == 418
        b_418 = r_418.json()
        assert b_418["code"] == "http_418"
        assert b_418["message"] == "请求处理失败。"
        assert "teapot" not in r_418.text.lower()

        # approvals.js includes late execution conflict notice & pure polling comparison helper
        r_js = tc.get("/console/approvals.js")
        assert r_js.status_code == 200
        js_src = r_js.text
        assert "late_execution_notice" in js_src
        assert "has_late_execution_conflict" in js_src
        assert "订单系统另有一次执行成功" in js_src
        assert "订单系统另有一次执行成功，退款单号未知，请到订单系统人工核对" in js_src

        helper_match = re.search(
            r"export\s+function\s+hasSelectedDetailChanged\s*\(\s*item\s*,\s*detail\s*\)\s*\{(.*?)\n\}",
            js_src,
            re.DOTALL,
        )
        assert helper_match is not None, "hasSelectedDetailChanged(item, detail) must be defined"
        helper_body = helper_match.group(1)
        assert "item.status !== detail.status" in helper_body
        expected_conflict_expr = (
            "Boolean(item.has_late_execution_conflict) "
            "!== Boolean(detail.has_late_execution_conflict)"
        )
        assert expected_conflict_expr in helper_body
        assert "document." not in helper_body
        assert "window." not in helper_body
        assert "state." not in helper_body

        sync_match = re.search(
            r"function\s+syncDetailStatusFromList\s*\(\s*items\s*\)\s*\{(.*?)\n\}",
            js_src,
            re.DOTALL,
        )
        assert sync_match is not None
        assert "hasSelectedDetailChanged(" in sync_match.group(1)
