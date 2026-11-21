from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import SigningKeyResolver
from app.config import Settings
from app.errors import AuthError, AuthUnavailableError
from app.main import create_app
from app.manifest import ToolManifest

GUARD_DIR = Path(__file__).resolve().parent.parent
GATEWAY_DIR = GUARD_DIR.parent
REPO_ROOT = GATEWAY_DIR.parent
MANIFEST_PATH = GATEWAY_DIR / "generated/tools.manifest.json"

FIXED_NOW = datetime(2026, 11, 20, 4, 0, 0, tzinfo=UTC)  # 2026-11-20T12:00:00+08:00

ACCOUNTS: dict[str, dict[str, Any]] = {
    "wang": {
        "sub": "u1001",
        "username": "wang",
        "name": "王磊",
        "role": "agent",
        "regions": "east",
    },
    "zhao": {
        "sub": "u1002",
        "username": "zhao",
        "name": "赵宇",
        "role": "agent",
        "regions": "north",
    },
    "chen": {
        "sub": "u1003",
        "username": "chen",
        "name": "陈晨",
        "role": "agent",
        "regions": "south",
    },
    "li": {
        "sub": "u2001",
        "username": "li",
        "name": "李娜",
        "role": "supervisor",
        "regions": "east",
    },
    "zhou": {
        "sub": "u2002",
        "username": "zhou",
        "name": "周涛",
        "role": "supervisor",
        "regions": "north,south",
    },
    "sun": {
        "sub": "u2003",
        "username": "sun",
        "name": "孙浩",
        "role": "supervisor",
        "regions": "east,north,south",
    },
}


def sample_order(
    order_id: str,
    region: str,
    *,
    status: str = "delivered",
    total_amount: float = 299.0,
    refunded_amount: float = 0.0,
    delivered_at: str | None = "2026-11-17T14:30:00+08:00",
    days_since_delivery: int | None = 3,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "order_id": order_id,
        "region": region,
        "status": status,
        "created_at": "2026-11-15T10:00:00+08:00",
        "total_amount": total_amount,
        "refunded_amount": refunded_amount,
        "refundable_amount": round(total_amount - refunded_amount, 2),
        "refund_status": "none" if refunded_amount == 0 else "partial",
        "customer": {
            "customer_id": "C0001",
            "name": "张敏",
            "phone_masked": "138****1234",
            "level": "vip",
        },
        "items": [
            {
                "sku": "SKU-001",
                "product_name": "蓝牙降噪耳机",
                "category": "electronics",
                "quantity": 1,
                "unit_price": total_amount,
            }
        ],
        "after_sale_tickets": [
            {
                "ticket_id": "T5001",
                "type": "return_refund",
                "reason_category": "quality_issue",
                "description": "右耳无声",
                "requested_amount": total_amount,
                "status": "open",
                "created_at": "2026-11-18T09:00:00+08:00",
            }
        ],
        "refunds": [],
    }
    if delivered_at is not None:
        data["delivered_at"] = delivered_at
    if days_since_delivery is not None:
        data["days_since_delivery"] = days_since_delivery
    return data


DEFAULT_ORDERS: dict[str, dict[str, Any]] = {
    "A1023": sample_order("A1023", "east", total_amount=299.0),
    "A1024": sample_order("A1024", "east", total_amount=199.0),
    "A1029": sample_order(
        "A1029",
        "east",
        status="pending_payment",
        total_amount=99.0,
        delivered_at=None,
        days_since_delivery=None,
    ),
    "B2087": sample_order("B2087", "north", total_amount=499.0),
    "C3001": sample_order("C3001", "south", total_amount=159.0),
}


@dataclass
class MutableClock:
    current: datetime = FIXED_NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, hours: float = 0, days: float = 0, seconds: float = 0) -> datetime:
        self.current = self.current + timedelta(hours=hours, days=days, seconds=seconds)
        return self.current


class StaticKeyResolver(SigningKeyResolver):
    def __init__(self, keys: dict[str, Any], *, unavailable: bool = False) -> None:
        self._keys = keys
        self.unavailable = unavailable

    def get_signing_key(self, token: str) -> Any:
        if self.unavailable:
            raise AuthUnavailableError()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("invalid_token", "身份令牌无效，请重新登录。") from exc
        kid = header.get("kid")
        if kid not in self._keys:
            raise AuthError("invalid_token", "身份令牌无效，请重新登录。")
        return self._keys[kid]


@dataclass
class RecordedUpstreamRequest:
    method: str
    url: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    raw_path: str = ""

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class FakeUpstream:
    orders: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_ORDERS.items()}
    )
    requests: list[RecordedUpstreamRequest] = field(default_factory=list)
    handler: Callable[[httpx.Request], httpx.Response] | None = None
    refund_seq: int = 105
    idempotent_refunds: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _decode_unverified_claims(self, request: httpx.Request) -> dict[str, Any]:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                return jwt.decode(auth[7:].strip(), options={"verify_signature": False})
            except Exception:
                return {}
        return {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.requests.append(
            RecordedUpstreamRequest(
                method=request.method,
                url=str(request.url),
                path=request.url.path,
                query=request.url.query.decode("utf-8"),
                headers={k.lower(): v for k, v in request.headers.items()},
                body=body,
                raw_path=request.url.raw_path.decode("ascii"),
            )
        )
        if self.handler is not None:
            return self.handler(request)

        req_id = request.headers.get("x-request-id", "upstream-req")
        path = request.url.path
        claims = self._decode_unverified_claims(request)
        raw_regions = claims.get("regions", "")
        regions = (
            {r.strip() for r in raw_regions.split(",") if r.strip()}
            if isinstance(raw_regions, str)
            else set(raw_regions)
        )

        # GET /api/v1/orders/{order_id}
        if request.method == "GET" and path.startswith("/api/v1/orders/"):
            order_id = path.removeprefix("/api/v1/orders/")
            order = self.orders.get(order_id)
            if order is None:
                return httpx.Response(
                    404,
                    json={
                        "code": "order_not_found",
                        "message": f"订单 {order_id} 不存在。",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            if order["region"] not in regions:
                return httpx.Response(
                    403,
                    json={
                        "code": "order_access_denied",
                        "message": f"无权访问订单 {order_id}。",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            return httpx.Response(200, json=order, headers={"X-Request-Id": req_id})

        # GET /api/v1/orders
        if request.method == "GET" and path == "/api/v1/orders":
            visible = [o for o in self.orders.values() if o["region"] in regions]
            return httpx.Response(
                200,
                json={"items": visible, "total": len(visible), "page": 1, "page_size": 20},
                headers={"X-Request-Id": req_id},
            )

        # POST /api/v1/refunds
        if request.method == "POST" and path == "/api/v1/refunds":
            payload = json.loads(body.decode("utf-8"))
            idem = payload.get("idempotency_key")
            if idem and idem in self.idempotent_refunds:
                return httpx.Response(
                    200,
                    json=self.idempotent_refunds[idem],
                    headers={"X-Request-Id": req_id},
                )
            order = self.orders.get(payload["order_id"])
            if order is None:
                return httpx.Response(
                    404,
                    json={
                        "code": "order_not_found",
                        "message": "订单不存在。",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            if order["region"] not in regions:
                return httpx.Response(
                    403,
                    json={
                        "code": "order_access_denied",
                        "message": "无权操作该订单。",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            if order["status"] in ("pending_payment", "cancelled"):
                return httpx.Response(
                    409,
                    json={
                        "code": "order_not_refundable",
                        "message": f"订单 {order['order_id']} 尚未支付，不能退款。",
                        "request_id": req_id,
                    },
                    headers={"X-Request-Id": req_id},
                )
            refund_id = f"R{self.refund_seq:06d}"
            self.refund_seq += 1
            refund = {
                "refund_id": refund_id,
                "order_id": order["order_id"],
                "region": order["region"],
                "amount": payload["amount"],
                "reason_category": payload["reason_category"],
                "reason": payload["reason"],
                "status": "succeeded",
                "created_by": claims.get("sub", "u2001"),
                "created_by_role": claims.get("role", "supervisor"),
                "created_at": "2026-11-20T12:00:00+08:00",
            }
            if idem:
                self.idempotent_refunds[idem] = refund
            return httpx.Response(201, json=refund, headers={"X-Request-Id": req_id})

        return httpx.Response(
            404,
            json={"code": "not_found", "message": "not found", "request_id": req_id},
            headers={"X-Request-Id": req_id},
        )

    def as_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture(scope="session")
def rsa_keys() -> dict[str, Any]:
    primary_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "kid": "test-kid-1",
        "private": primary_priv,
        "public": primary_priv.public_key(),
        "other_kid": "other-kid-2",
        "other_private": other_priv,
        "other_public": other_priv.public_key(),
    }


@pytest.fixture()
def key_resolver(rsa_keys: dict[str, Any]) -> StaticKeyResolver:
    return StaticKeyResolver({rsa_keys["kid"]: rsa_keys["public"]})


@pytest.fixture()
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture()
def fake_upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        upstream_base_url="http://mock-api:8000",
        tools_manifest=str(MANIFEST_PATH),
        jwt_issuer="http://dev-idp:8010",
        jwt_audience="mcpgate",
        jwt_jwks_url="http://dev-idp:8010/.well-known/jwks.json",
        jwt_algorithms="RS256",
        jwt_leeway_seconds=60,
        approver_role="supervisor",
        allow_self_approval=False,
        approval_ttl_hours=72,
        audit_retention_days=180,
        audit_redact_fields="",
        dev_login_url="http://dev-idp:8010/token",
        timezone="Asia/Shanghai",
    )


@pytest.fixture()
def make_token(rsa_keys: dict[str, Any], settings: Settings) -> Callable[..., str]:
    def _make(
        account: str = "wang",
        *,
        overrides: dict[str, Any] | None = None,
        drop_claims: set[str] | None = None,
        kid: str | None = None,
        private_key: Any = None,
        ttl: int = 3600,
    ) -> str:
        info = ACCOUNTS[account]
        now_ts = int(time.time())
        payload: dict[str, Any] = {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": info["sub"],
            "iat": now_ts,
            "exp": now_ts + ttl,
            "jti": str(uuid.uuid4()),
            "preferred_username": info["username"],
            "name": info["name"],
            "role": info["role"],
            "regions": info["regions"],
        }
        if overrides:
            payload.update(overrides)
        if drop_claims:
            for claim in drop_claims:
                payload.pop(claim, None)
        return jwt.encode(
            payload,
            private_key or rsa_keys["private"],
            algorithm="RS256",
            headers={"kid": kid or rsa_keys["kid"], "typ": "JWT"},
        )

    return _make


@pytest.fixture()
def auth_headers(make_token: Callable[..., str]) -> Callable[..., dict[str, str]]:
    def _headers(account: str = "wang", **kwargs: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(account, **kwargs)}"}

    return _headers


@pytest.fixture()
def build_app(
    settings: Settings,
    key_resolver: StaticKeyResolver,
    clock: MutableClock,
    fake_upstream: FakeUpstream,
) -> Callable[..., FastAPI]:
    def _build(
        *,
        custom_settings: Settings | None = None,
        custom_manifest: ToolManifest | None = None,
        custom_resolver: SigningKeyResolver | None = None,
        custom_upstream: FakeUpstream | None = None,
        custom_clock: MutableClock | None = None,
    ) -> FastAPI:
        up = custom_upstream or fake_upstream
        return create_app(
            settings=custom_settings or settings,
            clock=custom_clock or clock,
            signing_key_resolver=custom_resolver or key_resolver,
            upstream_transport=up.as_transport(),
            manifest=custom_manifest,
        )

    return _build


@pytest.fixture()
def app(build_app: Callable[..., FastAPI]) -> FastAPI:
    return build_app()


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as tc:
        yield tc
