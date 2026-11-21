from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import SigningKeyResolver
from app.config import Settings
from app.errors import AuthError, AuthUnavailableError
from app.main import create_app
from app.models import AfterSaleTicket, Customer, Order, OrderItem, Refund

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
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        reset_on_startup=True,
        jwt_issuer="http://dev-idp:8010",
        jwt_audience="mcpgate",
        jwt_jwks_url="http://dev-idp:8010/.well-known/jwks.json",
        jwt_algorithms="RS256",
        jwt_leeway_seconds=60,
        public_base_url="http://mock-api:8000",
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
def app(settings: Settings, key_resolver: StaticKeyResolver) -> FastAPI:
    return create_app(
        settings=settings,
        init_database=True,
        clock=lambda: FIXED_NOW,
        signing_key_resolver=key_resolver,
    )


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as tc:
        yield tc


def take_db_snapshot(app: FastAPI) -> dict[str, list[tuple[Any, ...]]]:
    with app.state.session_factory() as session:
        customers = [
            (c.customer_id, c.name, c.phone, c.level, c.created_at)
            for c in session.scalars(select(Customer).order_by(Customer.customer_id)).all()
        ]
        orders = [
            (
                o.order_id,
                o.customer_id,
                o.region,
                o.status,
                o.total_amount_cents,
                o.refunded_amount_cents,
                o.created_at,
                o.paid_at,
                o.shipped_at,
                o.delivered_at,
            )
            for o in session.scalars(select(Order).order_by(Order.order_id)).all()
        ]
        items = [
            (i.id, i.order_id, i.sku, i.product_name, i.category, i.quantity, i.unit_price_cents)
            for i in session.scalars(select(OrderItem).order_by(OrderItem.id)).all()
        ]
        tickets = [
            (
                t.ticket_id,
                t.order_id,
                t.region,
                t.type,
                t.reason_category,
                t.description,
                t.requested_amount_cents,
                t.status,
                t.created_at,
                t.resolved_at,
            )
            for t in session.scalars(
                select(AfterSaleTicket).order_by(AfterSaleTicket.ticket_id)
            ).all()
        ]
        refunds = [
            (
                r.refund_id,
                r.order_id,
                r.region,
                r.ticket_id,
                r.amount_cents,
                r.reason_category,
                r.reason,
                r.status,
                r.idempotency_key,
                r.created_by,
                r.created_by_role,
                r.created_at,
                r.completed_at,
            )
            for r in session.scalars(select(Refund).order_by(Refund.refund_id)).all()
        ]
    return {
        "customers": customers,
        "orders": orders,
        "order_items": items,
        "after_sale_tickets": tickets,
        "refunds": refunds,
    }
