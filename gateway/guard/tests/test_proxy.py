from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.manifest import ToolManifest
from app.routers import proxy as proxy_router
from tests.conftest import FakeUpstream


@pytest.mark.parametrize("status_code", [200, 403, 404, 409, 422])
def test_proxy_passthrough_status_and_bytes(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    status_code: int,
) -> None:
    raw_bytes = f'{{"code":"err_{status_code}","message":"原始消息","region":"east"}}'.encode()
    fake_upstream.handler = lambda req: httpx.Response(
        status_code,
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Request-Id": req.headers["x-request-id"],
            "X-Internal-Secret": "should-not-leak",
            "Location": "/api/v1/orders/A1023",
        },
    )

    wang_headers = auth_headers("wang")
    r = client.get(
        "/api/v1/orders/A1023",
        headers={
            **wang_headers,
            "X-Request-Id": "req-fixed-123",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Mse-Consumer": "idp",
            "X-User-Id": "spoofed",
            "Cookie": "session=evil",
        },
    )
    assert r.status_code == status_code
    assert r.content == raw_bytes
    assert r.headers["X-Request-Id"] == "req-fixed-123"
    assert r.headers["Location"] == "/api/v1/orders/A1023"
    assert "X-Internal-Secret" not in r.headers

    # Upstream received strictly whitelisted headers (plus host from GUARD_UPSTREAM_BASE_URL)
    sent = fake_upstream.requests[-1]
    assert sent.headers["authorization"] == wang_headers["Authorization"]
    assert sent.headers["x-request-id"] == "req-fixed-123"
    assert sent.headers["accept"] == "application/json"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["host"] == "mock-api:8000"
    assert set(sent.headers.keys()) == {
        "host",
        "authorization",
        "accept",
        "content-type",
        "x-request-id",
    }
    assert "x-mse-consumer" not in sent.headers
    assert "x-user-id" not in sent.headers
    assert "cookie" not in sent.headers


def test_proxy_generates_request_id_and_preserves_query_and_list_result(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    r = client.get("/api/v1/orders?status=delivered&page=1", headers=auth_headers("wang"))
    assert r.status_code == 200
    assert r.headers["X-Request-Id"]
    sent = fake_upstream.requests[-1]
    assert sent.path == "/api/v1/orders"
    assert sent.query == "status=delivered&page=1"
    assert sent.headers["x-request-id"] == r.headers["X-Request-Id"]


def test_proxy_list_non_json_body_and_single_non_json_body(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    fake_upstream.handler = lambda req: httpx.Response(200, content=b"plain-text-list")
    r1 = client.get("/api/v1/orders", headers=auth_headers("wang"))
    assert r1.status_code == 200
    assert r1.content == b"plain-text-list"

    fake_upstream.handler = lambda req: httpx.Response(200, content=b"plain-text-item")
    r2 = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert r2.status_code == 200
    assert r2.content == b"plain-text-item"


def test_proxy_unlisted_route_returns_404_without_calling_upstream(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    r1 = client.get("/openapi.json", headers=auth_headers("wang"))
    assert r1.status_code == 404
    assert r1.json()["code"] == "route_not_found"

    r2 = client.delete("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert r2.status_code == 404
    assert r2.json()["code"] == "route_not_found"

    assert len(fake_upstream.requests) == 0


def test_proxy_unmanaged_write_tool_returns_403_and_audits(
    build_app: Callable[..., FastAPI],
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    custom_manifest = ToolManifest.from_raw(
        [
            {
                "name": "delete_order",
                "method": "DELETE",
                "path": "/api/v1/orders/{order_id}",
                "write": True,
                "owner": "upstream",
            }
        ]
    )
    app = build_app(custom_manifest=custom_manifest)
    with TestClient(app) as tc:
        r = tc.delete("/api/v1/orders/A1023", headers=auth_headers("wang"))
        assert r.status_code == 403
        assert r.json()["code"] == "write_not_permitted"
        assert len(fake_upstream.requests) == 0

        logs = tc.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
        assert logs["total"] == 1
        assert logs["items"][0]["tool"] == "delete_order"
        assert logs["items"][0]["error_code"] == "write_not_permitted"
        assert logs["items"][0]["outcome"] == "access_denied"


def test_proxy_upstream_timeout_returns_503_and_audits(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    def _timeout(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    fake_upstream.handler = _timeout
    r = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert r.status_code == 503
    assert r.json()["code"] == "upstream_unavailable"

    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs["total"] == 1
    assert logs["items"][0]["status_code"] == 503
    assert logs["items"][0]["outcome"] == "error"
    assert logs["items"][0]["error_code"] == "upstream_unavailable"


@pytest.mark.parametrize(
    ("inbound_segment", "expected_encoded"),
    [
        ("A1023%3F", "A1023%3F"),
        ("A1023%23x", "A1023%23x"),
        ("A1023%2F.", "A1023%2F."),
        ("..%2F..%2F..%2Fhealthz", "..%2F..%2F..%2Fhealthz"),
    ],
)
def test_proxy_path_param_percent_encoded_and_audited(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    inbound_segment: str,
    expected_encoded: str,
) -> None:
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]
    r = client.get(f"/api/v1/orders/{inbound_segment}", headers=auth_headers("wang"))
    assert r.status_code == 404
    assert r.json()["code"] == "order_not_found"

    sent = fake_upstream.requests[-1]
    assert sent.raw_path == f"/api/v1/orders/{expected_encoded}"
    assert sent.query == ""

    after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["tool"] == "get_order"
    assert ev["status_code"] == 404
    assert ev["outcome"] == "not_found"


@pytest.mark.parametrize(
    "bad_segment",
    ["%2e", "%2e%2e", "A1023%0Ax", "A1023%0Dx", "A1023%00x"],
)
def test_proxy_rejects_dot_and_control_char_path_segments_with_422_and_audit(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
    bad_segment: str,
) -> None:
    before_reqs = len(fake_upstream.requests)
    before_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "total"
    ]

    r = client.get(f"/api/v1/orders/{bad_segment}", headers=auth_headers("wang"))
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"
    assert r.json()["details"]
    assert len(fake_upstream.requests) == before_reqs

    after_logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert after_logs["total"] == before_logs + 1
    ev = after_logs["items"][0]
    assert ev["tool"] == "get_order"
    assert ev["status_code"] == 422
    assert ev["error_code"] == "validation_error"
    assert ev["outcome"] == "invalid"


def test_proxy_handles_invalid_url_as_503_with_audit(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    def _invalid_url(_req: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad url")

    fake_upstream.handler = _invalid_url
    r = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert r.status_code == 503
    assert r.json()["code"] == "upstream_unavailable"

    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs["total"] == 1
    assert logs["items"][0]["status_code"] == 503
    assert logs["items"][0]["error_code"] == "upstream_unavailable"


def test_proxy_read_path_params_override_query_params_in_audit(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    # Path order_id must win over colliding query parameter order_id
    r_path = client.get(
        "/api/v1/orders/A1023?order_id=B2087&extra=keep",
        headers=auth_headers("wang"),
    )
    assert r_path.status_code == 200

    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs["total"] == 1
    ev_path = logs["items"][0]
    assert ev_path["order_id"] == "A1023"
    assert ev_path["arguments"]["order_id"] == "A1023"
    assert ev_path["arguments"]["extra"] == "keep"

    # When route has no path order_id, query order_id is used as fallback
    r_list = client.get(
        "/api/v1/orders?order_id=A1023",
        headers=auth_headers("wang"),
    )
    assert r_list.status_code == 200
    logs2 = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
    assert logs2["total"] == 2
    ev_list = logs2["items"][0]
    assert ev_list["order_id"] == "A1023"
    assert ev_list["arguments"]["order_id"] == "A1023"


def test_proxy_read_handles_non_finite_upstream_json_safely(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    fake_upstream.handler = lambda _req: httpx.Response(
        200,
        content=b'{"total": NaN, "items": []}',
        headers={"Content-Type": "application/json"},
    )
    r = client.get("/api/v1/orders", headers=auth_headers("wang"))
    assert r.status_code == 200

    audit_resp = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
    assert audit_resp.status_code == 200
    ev = audit_resp.json()["items"][0]
    assert ev["tool"] == "list_orders"
    assert "result" not in ev


def test_proxy_read_rejects_deep_nesting_non_finite_and_surrogate_bodies_with_audit(
    client: TestClient,
    fake_upstream: FakeUpstream,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    nested_600 = ('{"x":' + "[" * 600 + "1" + "]" * 600 + "}").encode()

    # 1. Nested 600 layers on read proxy with path order_id -> 422, 1 audit, no upstream call
    before = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()["total"]
    r_nested = client.request(
        "GET",
        "/api/v1/orders/A1023",
        content=nested_600,
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_nested.status_code == 422
    assert r_nested.json()["code"] == "validation_error"
    assert r_nested.json()["message"] == "请求体嵌套过深。"
    assert len(fake_upstream.requests) == 0
    logs = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun"))
    assert logs.status_code == 200
    assert logs.json()["total"] == before + 1
    ev_nested = logs.json()["items"][0]
    assert ev_nested["tool"] == "get_order"
    assert ev_nested["status_code"] == 422
    assert ev_nested["order_id"] == "A1023"
    assert "[max_depth_exceeded]" in str(ev_nested["arguments"])

    # 2. NaN body on list read proxy -> 422, extracts order_id & "NaN" placeholder in audit
    r_nan = client.request(
        "GET",
        "/api/v1/orders",
        content=b'{"order_id": "A1023", "amount": NaN}',
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_nan.status_code == 422
    ev_nan = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "items"
    ][0]
    assert ev_nan["tool"] == "list_orders"
    assert ev_nan["order_id"] == "A1023"
    assert ev_nan["arguments"]["amount"] == "NaN"

    # 3. Lone surrogate body on read proxy -> 422, sanitized audit
    r_surr = client.request(
        "GET",
        "/api/v1/orders",
        content=b'{"note": "\\ud800"}',
        headers={**auth_headers("wang"), "Content-Type": "application/json"},
    )
    assert r_surr.status_code == 422
    assert r_surr.json()["message"] == "请求体含非法字符。"
    ev_surr = client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()[
        "items"
    ][0]
    assert ev_surr["arguments"]["note"] == "\ufffd"

    # 4. Upstream returning deeply nested JSON on read proxy does not 500
    fake_upstream.handler = lambda _req: httpx.Response(
        200,
        content=('{"total": 1, "items":' + "[" * 600 + "1" + "]" * 600 + "}").encode(),
        headers={"Content-Type": "application/json"},
    )
    r_up = client.get("/api/v1/orders", headers=auth_headers("wang"))
    assert r_up.status_code == 200
    assert (
        client.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).status_code == 200
    )


def test_manifest_rejects_conflicting_upstream_paths() -> None:
    with pytest.raises(ValueError, match="冲突"):
        ToolManifest.from_raw(
            [
                {
                    "name": "bad_guard",
                    "method": "GET",
                    "path": "/guard/v1/hack",
                    "write": False,
                    "owner": "upstream",
                }
            ]
        )
    with pytest.raises(ValueError, match="冲突"):
        ToolManifest.from_raw(
            [
                {
                    "name": "bad_console",
                    "method": "GET",
                    "path": "/console/index.html",
                    "write": False,
                    "owner": "upstream",
                }
            ]
        )
    with pytest.raises(ValueError, match="未知的工具 owner"):
        ToolManifest.from_raw(
            [
                {
                    "name": "bad_owner",
                    "method": "GET",
                    "path": "/api/v1/x",
                    "write": False,
                    "owner": "unknown",
                }
            ]
        )


def test_proxy_read_rejects_oversized_body_with_413_and_empty_audit(
    build_app: Callable[..., FastAPI],
    settings: Settings,
    auth_headers: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 128
    custom_cfg = settings.model_copy(update={"max_body_bytes": limit})
    up = FakeUpstream()
    app = build_app(custom_settings=custom_cfg, custom_upstream=up)

    def _forbid_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Oversized read body must not enter JSON parsing")

    monkeypatch.setattr(proxy_router, "parse_strict_json_dict", _forbid_parse)

    with TestClient(app) as tc:
        # 1. GET /api/v1/orders/A1023 with oversized body -> 413, 1 empty-args audit, no upstream
        r_item = tc.request(
            "GET",
            "/api/v1/orders/A1023",
            content=b"{" + b"z" * limit + b"}",
            headers={**auth_headers("wang"), "Content-Type": "application/json"},
        )
        assert r_item.status_code == 413
        body_item = r_item.json()
        assert body_item["code"] == "payload_too_large"
        assert body_item["message"]
        assert body_item["request_id"] == r_item.headers["X-Request-Id"]

        logs1 = tc.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
        assert logs1["total"] == 1
        ev1 = logs1["items"][0]
        assert ev1["tool"] == "get_order"
        assert ev1["arguments"] == {}
        assert ev1["order_id"] == "A1023"
        assert ev1["status_code"] == 413
        assert ev1["error_code"] == "payload_too_large"
        assert ev1["outcome"] == "invalid"

        # 2. GET /api/v1/orders with chunked oversized stream & invalid Content-Length
        r_list = tc.request(
            "GET",
            "/api/v1/orders",
            content=(b"x" * 70 for _ in range(3)),
            headers={
                **auth_headers("wang"),
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
            },
        )
        assert r_list.status_code == 413
        assert r_list.json()["code"] == "payload_too_large"

        logs2 = tc.get("/guard/v1/console/audit-logs", headers=auth_headers("sun")).json()
        assert logs2["total"] == 2
        ev2 = logs2["items"][0]
        assert ev2["tool"] == "list_orders"
        assert ev2["arguments"] == {}
        assert "order_id" not in ev2
        assert ev2["status_code"] == 413
        assert ev2["error_code"] == "payload_too_large"
        assert ev2["outcome"] == "invalid"

        assert len(up.requests) == 0
