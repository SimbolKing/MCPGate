from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError
from pydantic import ValidationError

from app.auth import JwksKeyResolver
from app.config import Settings
from app.errors import AuthError, AuthUnavailableError
from app.main import create_app
from tests.conftest import StaticKeyResolver


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_valid_token_and_missing_preferred_username_fallback(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    token = make_token("wang")
    resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "A1023"

    token_no_username = make_token("wang", drop_claims={"preferred_username"})
    resp2 = client.get(
        "/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token_no_username}"}
    )
    assert resp2.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer    "},
    ],
)
def test_missing_or_non_bearer_token(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/orders/A1023", headers=headers)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == 'Bearer error="invalid_token"'
    body = resp.json()
    assert body["code"] == "missing_token"
    assert body["request_id"] == resp.headers["X-Request-Id"]


def test_expired_token_and_leeway(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    # 超出 60 秒时钟容忍 → 401 token_expired
    expired = make_token("wang", ttl=-120)
    resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "token_expired"
    assert resp.headers.get("WWW-Authenticate") == 'Bearer error="invalid_token"'

    # 在 60 秒时钟容忍内 → 200
    within_leeway = make_token("wang", ttl=-30)
    resp_ok = client.get(
        "/api/v1/orders/A1023", headers={"Authorization": f"Bearer {within_leeway}"}
    )
    assert resp_ok.status_code == 200


def test_wrong_issuer_and_audience(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    bad_iss = make_token("wang", overrides={"iss": "http://evil-idp:9999"})
    resp_iss = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {bad_iss}"})
    assert resp_iss.status_code == 401
    assert resp_iss.json()["code"] == "invalid_token"

    bad_aud = make_token("wang", overrides={"aud": "other-service"})
    resp_aud = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {bad_aud}"})
    assert resp_aud.status_code == 401
    assert resp_aud.json()["code"] == "invalid_token"


def test_tampered_signature_other_key_and_unknown_kid(
    client: TestClient,
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    token = make_token("wang")
    head, payload, sig = token.split(".")
    tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    tampered = f"{head}.{payload}.{tampered_sig}"
    resp_tampered = client.get(
        "/api/v1/orders/A1023", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert resp_tampered.status_code == 401
    assert resp_tampered.json()["code"] == "invalid_token"

    other_key_token = make_token("wang", private_key=rsa_keys["other_private"])
    resp_other = client.get(
        "/api/v1/orders/A1023", headers={"Authorization": f"Bearer {other_key_token}"}
    )
    assert resp_other.status_code == 401
    assert resp_other.json()["code"] == "invalid_token"

    unknown_kid_token = make_token("wang", kid="unknown-kid")
    resp_kid = client.get(
        "/api/v1/orders/A1023", headers={"Authorization": f"Bearer {unknown_kid_token}"}
    )
    assert resp_kid.status_code == 401
    assert resp_kid.json()["code"] == "invalid_token"

    malformed = "not-a-jwt"
    resp_bad = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {malformed}"})
    assert resp_bad.status_code == 401
    assert resp_bad.json()["code"] == "invalid_token"


def test_alg_none_and_hs256_confusion_rejected(
    client: TestClient,
    rsa_keys: dict[str, Any],
) -> None:
    now_ts = int(time.time())
    payload = {
        "iss": "http://dev-idp:8010",
        "aud": "mcpgate",
        "sub": "u1001",
        "iat": now_ts,
        "exp": now_ts + 3600,
        "role": "agent",
        "regions": "east",
    }

    # alg: none（带已知 kid）
    none_header = {"alg": "none", "typ": "JWT", "kid": rsa_keys["kid"]}
    alg_none_token = (
        f"{_b64url(json.dumps(none_header).encode())}.{_b64url(json.dumps(payload).encode())}."
    )
    resp_none = client.get(
        "/api/v1/orders/A1023", headers={"Authorization": f"Bearer {alg_none_token}"}
    )
    assert resp_none.status_code == 401
    assert resp_none.json()["code"] == "invalid_token"

    # HS256 混淆攻击：以 RSA 公钥 PEM 字节作为 HMAC 密钥手工构造 HS256 签名
    pub_pem = rsa_keys["public"].public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    hs_header = {"alg": "HS256", "typ": "JWT", "kid": rsa_keys["kid"]}
    signing_input = (
        f"{_b64url(json.dumps(hs_header).encode())}.{_b64url(json.dumps(payload).encode())}"
    )
    sig = hmac.new(pub_pem, signing_input.encode("ascii"), hashlib.sha256).digest()
    hs256_token = f"{signing_input}.{_b64url(sig)}"
    resp_hs = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {hs256_token}"})
    assert resp_hs.status_code == 401
    assert resp_hs.json()["code"] == "invalid_token"


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
def test_missing_required_standard_claims(
    client: TestClient,
    make_token: Callable[..., str],
    claim: str,
) -> None:
    token = make_token("wang", drop_claims={claim})
    resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"


def test_blank_sub_claim_rejected(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    token = make_token("wang", overrides={"sub": "   "})
    resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"


@pytest.mark.parametrize("bad_role", ["admin", "", None, 123])
def test_invalid_role_claim(
    client: TestClient,
    make_token: Callable[..., str],
    bad_role: Any,
) -> None:
    token = make_token("wang", overrides={"role": bad_role})
    resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "invalid_claims"


@pytest.mark.parametrize("bad_regions", ["", " , , ", [], ["  "], [123], None, 42])
def test_empty_or_invalid_regions_claim(
    client: TestClient,
    make_token: Callable[..., str],
    bad_regions: Any,
) -> None:
    token = make_token("wang", overrides={"regions": bad_regions})
    resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "invalid_claims"


def test_regions_accepts_string_and_array(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    token_str = make_token("zhou", overrides={"regions": "north, south"})
    resp_str = client.get("/api/v1/orders/B2087", headers={"Authorization": f"Bearer {token_str}"})
    assert resp_str.status_code == 200

    token_arr = make_token("zhou", overrides={"regions": ["north", "south"]})
    resp_arr = client.get("/api/v1/orders/C3051", headers={"Authorization": f"Bearer {token_arr}"})
    assert resp_arr.status_code == 200


def test_jwks_unavailable_returns_503(
    client: TestClient,
    make_token: Callable[..., str],
    key_resolver: StaticKeyResolver,
) -> None:
    key_resolver.unavailable = True
    try:
        token = make_token("wang")
        resp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 503
        assert resp.json()["code"] == "auth_unavailable"
    finally:
        key_resolver.unavailable = False


def test_auth_runs_before_parameter_validation(client: TestClient) -> None:
    # 未带 token 且查询参数非法（page=-1）→ 401 而非 422
    resp_query = client.get("/api/v1/orders?page=-1&page_size=999")
    assert resp_query.status_code == 401
    assert resp_query.json()["code"] == "missing_token"

    # 未带 token 且请求体字段非法（amount=-5）→ 401 而非 422
    resp_body = client.post(
        "/api/v1/refunds",
        json={"order_id": "", "amount": -5, "reason_category": "bad", "reason": ""},
    )
    assert resp_body.status_code == 401
    assert resp_body.json()["code"] == "missing_token"


def test_token_never_logged(
    client: TestClient,
    make_token: Callable[..., str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = make_token("wang", ttl=-120)
    mock_api_logger = logging.getLogger("mock_api")
    orig_propagate = mock_api_logger.propagate
    mock_api_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="mock_api"):
            client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {token}"})
        for record in caplog.records:
            assert token not in record.getMessage()
            fields = getattr(record, "fields", {})
            assert token not in json.dumps(fields, ensure_ascii=False)
    finally:
        mock_api_logger.propagate = orig_propagate


def test_jwks_key_resolver_distinguishes_token_and_jwks_errors(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    valid_token = make_token("wang")
    other_kid_token = make_token("wang", kid="rotated-kid")

    # 1. Token 自身问题 → 401 invalid_token（不触发 JWKS 请求）
    resolver = JwksKeyResolver("http://127.0.0.1:9999/.well-known/jwks.json", cache_seconds=60)
    resolver._client = MagicMock()
    resolver._client.get_signing_keys.side_effect = AssertionError("不应拉取 JWKS")

    for bad_token in [
        "not-a-jwt",
        f"{_b64url(b'{bad-json')}.{_b64url(b'{}')}.sig",
        f"{_b64url(json.dumps({'alg': 'RS256'}).encode())}.{_b64url(b'{}')}.sig",
        f"{_b64url(json.dumps({'alg': 'RS256', 'kid': '   '}).encode())}.{_b64url(b'{}')}.sig",
        f"{_b64url(json.dumps({'alg': 'RS256', 'kid': 123}).encode())}.{_b64url(b'{}')}.sig",
    ]:
        with pytest.raises(AuthError) as auth_exc:
            resolver.get_signing_key(bad_token)
        assert auth_exc.value.status_code == 401
        assert auth_exc.value.code == "invalid_token"

    # 2. 缓存命中 & 密钥轮换刷新命中 & 刷新后仍无对应 kid → 401
    primary_jwk = MagicMock(key="PUBKEY_1", key_id=rsa_keys["kid"])
    rotated_jwk = MagicMock(key="PUBKEY_2", key_id="rotated-kid")
    resolver._client = MagicMock()
    resolver._client.match_kid.side_effect = lambda keys, kid: next(
        (k for k in keys if k.key_id == kid), None
    )
    resolver._client.get_signing_keys.return_value = [primary_jwk]
    assert resolver.get_signing_key(valid_token) == "PUBKEY_1"

    # 未知 kid 触发 refresh=True，轮换后命中
    resolver._client.get_signing_keys.side_effect = [[primary_jwk], [primary_jwk, rotated_jwk]]
    assert resolver.get_signing_key(other_kid_token) == "PUBKEY_2"
    assert resolver._client.get_signing_keys.call_args_list[-1].kwargs == {"refresh": True}

    # 刷新后依然找不到 kid → 401 invalid_token（不受上一轮冷却影响的新实例）
    resolver_miss = JwksKeyResolver("http://127.0.0.1:9999/.well-known/jwks.json", cache_seconds=60)
    resolver_miss._client = MagicMock()
    resolver_miss._client.match_kid.side_effect = lambda keys, kid: next(
        (k for k in keys if k.key_id == kid), None
    )
    resolver_miss._client.get_signing_keys.side_effect = [[primary_jwk], [primary_jwk]]
    with pytest.raises(AuthError) as missing_kid_exc:
        resolver_miss.get_signing_key(other_kid_token)
    assert missing_kid_exc.value.status_code == 401
    assert missing_kid_exc.value.code == "invalid_token"
    assert resolver_miss._client.get_signing_keys.call_count == 2

    # 3. JWKS 获取 / 解析 / 空密钥集等错误 → 503 auth_unavailable
    for err in [
        PyJWKClientConnectionError("net err"),
        json.JSONDecodeError("Expecting value", "<html>502</html>", 0),
        PyJWKClientError("The JWKS endpoint did not return a JSON object"),
        PyJWKSetError("The JWK Set did not contain any keys"),
        PyJWKClientError("The JWKS endpoint did not contain any signing keys"),
        TypeError("unexpected jwks payload type"),
        OSError("connection reset"),
        ConnectionResetError("connection reset by peer"),
    ]:
        resolver._client.get_signing_keys.side_effect = err
        with pytest.raises(AuthUnavailableError) as unavail_exc:
            resolver.get_signing_key(valid_token)
        assert unavail_exc.value.status_code == 503
        assert unavail_exc.value.code == "auth_unavailable"

    # get_signing_keys 返回空列表同样视为 503 auth_unavailable
    resolver._client.get_signing_keys.side_effect = None
    resolver._client.get_signing_keys.return_value = []
    with pytest.raises(AuthUnavailableError):
        resolver.get_signing_key(valid_token)

    # 4. 真实 PyJWKClient 解析各类坏 JWKS 载荷 → 503 auth_unavailable
    for raw_body in [
        b"<html>not json</html>",
        b"[]",
        b"{}",
        b'{"keys": []}',
        b'{"keys": [{"kty": "RSA"}]}',
    ]:
        real_resolver = JwksKeyResolver(
            "http://127.0.0.1:9999/.well-known/jwks.json", cache_seconds=60
        )
        fake_resp = MagicMock()
        fake_resp.read.return_value = raw_body
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp
        with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fake_opener):
            with pytest.raises(AuthUnavailableError) as real_exc:
                real_resolver.get_signing_key(valid_token)
            assert real_exc.value.status_code == 503
            assert real_exc.value.code == "auth_unavailable"

    # 5. 真实 PyJWKClient 加载合法 JWKS，但 token 的 kid 不在其中 → 401 invalid_token
    pub_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["public"]))
    pub_jwk.update({"kid": rsa_keys["kid"], "use": "sig", "alg": "RS256"})
    good_jwks_bytes = json.dumps({"keys": [pub_jwk]}).encode("utf-8")

    real_resolver_ok = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json", cache_seconds=60
    )
    fake_opener_ok = MagicMock()

    def _open_good(*_args: Any, **_kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = good_jwks_bytes
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    fake_opener_ok.open.side_effect = _open_good
    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fake_opener_ok):
        assert real_resolver_ok.get_signing_key(valid_token) is not None
        with pytest.raises(AuthError) as not_found_exc:
            real_resolver_ok.get_signing_key(other_kid_token)
        assert not_found_exc.value.status_code == 401
        assert not_found_exc.value.code == "invalid_token"


def test_jwks_unknown_kid_cooldown_rotation_timeout_and_connection_reset(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    now = [1000.0]

    def _fake_monotonic() -> float:
        return now[0]

    pub_jwk_1 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["public"]))
    pub_jwk_1.update({"kid": rsa_keys["kid"], "use": "sig", "alg": "RS256"})
    pub_jwk_2 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["other_public"]))
    pub_jwk_2.update({"kid": rsa_keys["other_kid"], "use": "sig", "alg": "RS256"})

    current_jwks = [{"keys": [pub_jwk_1]}]
    fetch_count = 0

    def _fake_open(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal fetch_count
        fetch_count += 1
        resp = MagicMock()
        resp.read.return_value = json.dumps(current_jwks[0]).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=300,
        refresh_cooldown_seconds=30.0,
        timeout_seconds=5.0,
        monotonic=_fake_monotonic,
    )
    assert resolver._client.timeout == 5.0

    fake_opener = MagicMock()
    fake_opener.open.side_effect = _fake_open
    valid_token = make_token("wang")
    rotated_token = make_token(
        "wang",
        kid=rsa_keys["other_kid"],
        private_key=rsa_keys["other_private"],
    )

    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fake_opener):
        # 1. 首次合法 token 预热缓存（1 次拉取）
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 1

        # 2. 冷却期内连续 100 个随机未知 kid，只触发 1 次强制刷新
        for i in range(100):
            now[0] = 1000.0 + (i * 0.2)  # 始终在 30 秒冷却窗口内
            unknown_tok = make_token("wang", kid=f"random-unknown-{i}")
            with pytest.raises(AuthError) as exc_info:
                resolver.get_signing_key(unknown_tok)
            assert exc_info.value.status_code == 401
            assert exc_info.value.code == "invalid_token"
        assert fetch_count == 2

        # 3. 冷却期内缓存自然过期时，正常拉取（refresh=False）不受冷却限制
        assert resolver._client.jwk_set_cache is not None
        resolver._client.jwk_set_cache.put(None)  # type: ignore[arg-type]
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 3

        # 4. 冷却结束后再次遇到未知 kid 会重新强制拉取；若密钥已轮换则验签通过
        now[0] = 1030.0
        current_jwks[0] = {"keys": [pub_jwk_1, pub_jwk_2]}
        assert resolver.get_signing_key(rotated_token) is not None
        assert fetch_count == 4

        # 5. 再次超过冷却后，真正不存在的 kid 仍触发一次强制刷新并报 401
        now[0] = 1061.0
        with pytest.raises(AuthError) as still_unknown:
            resolver.get_signing_key(make_token("wang", kid="still-missing"))
        assert still_unknown.value.status_code == 401
        assert still_unknown.value.code == "invalid_token"
        assert fetch_count == 5

    # 6. JWKS 响应体读取中途发生 ConnectionResetError → 503 auth_unavailable
    reset_resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=60,
        timeout_seconds=5.0,
    )
    broken_resp = MagicMock()
    broken_resp.read.side_effect = ConnectionResetError("connection reset mid-body")
    broken_resp.__enter__.return_value = broken_resp
    broken_resp.__exit__.return_value = False
    broken_opener = MagicMock()
    broken_opener.open.return_value = broken_resp

    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=broken_opener):
        with pytest.raises(AuthUnavailableError) as reset_exc:
            reset_resolver.get_signing_key(valid_token)
        assert reset_exc.value.status_code == 503
        assert reset_exc.value.code == "auth_unavailable"


def test_jwks_failed_forced_refresh_returns_503_during_cooldown_and_recovers_after(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    now = [1000.0]

    def _fake_monotonic() -> float:
        return now[0]

    pub_jwk_1 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["public"]))
    pub_jwk_1.update({"kid": rsa_keys["kid"], "use": "sig", "alg": "RS256"})
    pub_jwk_2 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["other_public"]))
    pub_jwk_2.update({"kid": rsa_keys["other_kid"], "use": "sig", "alg": "RS256"})

    state: dict[str, Any] = {"fail": False, "jwks": {"keys": [pub_jwk_1]}}
    fetch_count = 0

    def _fake_open(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal fetch_count
        fetch_count += 1
        if state["fail"]:
            raise OSError("idp temporarily unavailable")
        resp = MagicMock()
        resp.read.return_value = json.dumps(state["jwks"]).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=300,
        refresh_cooldown_seconds=30.0,
        timeout_seconds=5.0,
        monotonic=_fake_monotonic,
    )
    fake_opener = MagicMock()
    fake_opener.open.side_effect = _fake_open

    valid_token = make_token("wang")
    rotated_token = make_token(
        "wang",
        kid=rsa_keys["other_kid"],
        private_key=rsa_keys["other_private"],
    )

    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fake_opener):
        # 1. 预热缓存（1 次拉取）
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 1

        # 2. 密钥刚轮换但 IdP 恰好故障：首次未知 kid 强制刷新失败 → 503
        state["fail"] = True
        with pytest.raises(AuthUnavailableError) as exc1:
            resolver.get_signing_key(rotated_token)
        assert exc1.value.status_code == 503
        assert exc1.value.code == "auth_unavailable"
        assert fetch_count == 2

        # 3. 冷却期内再次遇到未知 kid：不外呼，返回 503 auth_unavailable（非 401）
        now[0] = 1005.0
        with pytest.raises(AuthUnavailableError) as exc2:
            resolver.get_signing_key(rotated_token)
        assert exc2.value.status_code == 503
        assert exc2.value.code == "auth_unavailable"
        assert fetch_count == 2

        now[0] = 1029.9
        with pytest.raises(AuthUnavailableError):
            resolver.get_signing_key(make_token("wang", kid="another-unknown"))
        assert fetch_count == 2

        # 已缓存的旧 kid 在冷却期内仍可正常验签
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 2

        # 4. 冷却过后 IdP 恢复，重新强制刷新成功，同一 token 通过
        now[0] = 1030.0
        state["fail"] = False
        state["jwks"] = {"keys": [pub_jwk_1, pub_jwk_2]}
        assert resolver.get_signing_key(rotated_token) is not None
        assert fetch_count == 3

        # 5. 本次刷新成功后，冷却期内未知 kid 应返回 401 invalid_token 且不外呼
        now[0] = 1035.0
        with pytest.raises(AuthError) as exc_after_ok:
            resolver.get_signing_key(make_token("wang", kid="nonexistent-after-ok"))
        assert exc_after_ok.value.status_code == 401
        assert exc_after_ok.value.code == "invalid_token"
        assert fetch_count == 3


def test_create_app_passes_jwks_settings_and_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_cfg = Settings(jwks_refresh_cooldown_seconds=12.5, jwks_timeout_seconds=2.5)
    app = create_app(settings=custom_cfg, init_database=False)
    resolver = app.state.signing_key_resolver
    assert isinstance(resolver, JwksKeyResolver)
    assert resolver._refresh_cooldown_seconds == 12.5
    assert resolver._client.timeout == 2.5

    # 冷却时间允许为 0（合法边界）
    zero_cooldown = Settings(jwks_refresh_cooldown_seconds=0.0, jwks_timeout_seconds=0.5)
    assert zero_cooldown.jwks_refresh_cooldown_seconds == 0.0
    assert zero_cooldown.jwks_timeout_seconds == 0.5

    # 非法取值在启动配置校验阶段直接报错
    for bad_kwargs in [
        {"jwks_timeout_seconds": 0},
        {"jwks_timeout_seconds": -1.0},
        {"jwks_timeout_seconds": float("nan")},
        {"jwks_timeout_seconds": float("inf")},
        {"jwks_timeout_seconds": float("-inf")},
        {"jwks_refresh_cooldown_seconds": -0.1},
        {"jwks_refresh_cooldown_seconds": float("nan")},
        {"jwks_refresh_cooldown_seconds": float("inf")},
        {"jwks_refresh_cooldown_seconds": float("-inf")},
    ]:
        with pytest.raises(ValidationError):
            Settings(**bad_kwargs)  # type: ignore[arg-type]

    monkeypatch.setenv("MOCKAPI_JWKS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        create_app(init_database=False)
