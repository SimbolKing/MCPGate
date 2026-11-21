from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import SigningKeyResolver
from app.config import Settings
from app.dify import DifyClient
from app.errors import AuthError, AuthUnavailableError
from app.main import create_app

TEST_DIFY_API_KEY = "app-secret-test-key-999999"

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


def encode_sse_events(events: list[dict[str, Any] | str]) -> bytes:
    parts: list[str] = []
    for item in events:
        if isinstance(item, str):
            parts.append(item)
        else:
            parts.append(f"data: {json.dumps(item, ensure_ascii=False)}\n\n")
    return "".join(parts).encode("utf-8")


class FailingByteStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first_chunk
        raise httpx.ReadError("simulated mid-stream disconnect")

    async def aclose(self) -> None:
        return None


@dataclass
class RecordedRequest:
    method: str
    url: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class FakeDify:
    requests: list[RecordedRequest] = field(default_factory=list)
    chat_handler: Callable[[httpx.Request], httpx.Response] | None = None
    messages_handler: Callable[[httpx.Request], httpx.Response] | None = None
    default_events: list[dict[str, Any] | str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.requests.append(
            RecordedRequest(
                method=request.method,
                url=str(request.url),
                path=request.url.path,
                params=dict(request.url.params),
                headers={k.lower(): v for k, v in request.headers.items()},
                body=body,
            )
        )
        if request.method == "POST" and request.url.path == "/v1/chat-messages":
            if self.chat_handler is not None:
                return self.chat_handler(request)
            events = self.default_events or [
                {
                    "event": "workflow_started",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "data": {"inputs": {"user_token": "secret-leak-check"}},
                },
                {
                    "event": "node_finished",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "data": {
                        "node_id": "parse_order",
                        "inputs": {"user_token": "secret-leak-check"},
                        "process_data": {"raw": "sensitive-process-data"},
                        "outputs": {
                            "call_summary": {
                                "tool": "get_order",
                                "kind": "ok",
                                "http_status": 200,
                                "leaked_token": "secret-leak-check",
                            },
                            "facts_text": "internal-facts",
                        },
                    },
                },
                {
                    "event": "message",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "answer": "订单 A1023 ",
                },
                {
                    "event": "message",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "answer": "可以退货退款。",
                },
                {
                    "event": "workflow_finished",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "data": {"status": "succeeded"},
                },
                {
                    "event": "message_end",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "metadata": {
                        "retriever_resources": [
                            {
                                "document_name": "售后制度.md",
                                "content": (
                                    "售后制度 第 3.2 条 质量问题退货期限\n"
                                    "规则：已签收不超过 30 天可退货退款。"
                                ),
                                "score": 0.91,
                            }
                        ]
                    },
                },
            ]
            return httpx.Response(
                200,
                content=encode_sse_events(events),
                headers={"Content-Type": "text/event-stream"},
            )

        if request.method == "GET" and request.url.path == "/v1/messages":
            if self.messages_handler is not None:
                return self.messages_handler(request)
            return httpx.Response(200, json={"limit": 100, "has_more": False, "data": []})

        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    def as_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@dataclass
class FakeIdp:
    requests: list[RecordedRequest] = field(default_factory=list)
    handler: Callable[[httpx.Request], httpx.Response] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.requests.append(
            RecordedRequest(
                method=request.method,
                url=str(request.url),
                path=request.url.path,
                params=dict(request.url.params),
                headers={k.lower(): v for k, v in request.headers.items()},
                body=body,
            )
        )
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(401, json={"code": "login_failed", "message": "bad credentials"})

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
def fake_dify() -> FakeDify:
    return FakeDify()


@pytest.fixture()
def fake_idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        dify_base_url="http://dify.internal",
        dify_api_key=TEST_DIFY_API_KEY,
        dify_timeout_seconds=30.0,
        jwt_issuer="http://dev-idp:8010",
        jwt_audience="mcpgate",
        jwt_jwks_url="http://dev-idp:8010/.well-known/jwks.json",
        jwt_algorithms="RS256",
        jwt_leeway_seconds=60,
        jwks_cache_seconds=300,
        jwt_role_claim="role",
        jwt_regions_claim="regions",
        dev_login_url="http://dev-idp:8010/token",
        approval_console_url="http://localhost:8020/console/",
        timezone="Asia/Shanghai",
        log_level="INFO",
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
    fake_dify: FakeDify,
    fake_idp: FakeIdp,
) -> Callable[..., FastAPI]:
    def _build(
        *,
        custom_settings: Settings | None = None,
        custom_resolver: SigningKeyResolver | None = None,
        custom_dify: FakeDify | None = None,
        custom_idp: FakeIdp | None = None,
    ) -> FastAPI:
        cfg = custom_settings or settings
        dify_mock = custom_dify or fake_dify
        idp_mock = custom_idp or fake_idp
        dify_client = DifyClient(
            base_url=cfg.dify_base_url,
            api_key=cfg.dify_api_key,
            timeout_seconds=cfg.dify_timeout_seconds,
            tz_name=cfg.timezone,
            transport=dify_mock.as_transport(),
        )
        login_client = httpx.AsyncClient(transport=idp_mock.as_transport())
        return create_app(
            settings=cfg,
            signing_key_resolver=custom_resolver or key_resolver,
            dify_client=dify_client,
            login_client=login_client,
        )

    return _build


@pytest.fixture()
def app(build_app: Callable[..., FastAPI]) -> FastAPI:
    return build_app()


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as tc:
        yield tc
