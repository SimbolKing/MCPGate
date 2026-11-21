from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError
from pydantic import ValidationError

from app.auth import JwksKeyResolver
from app.config import Settings
from app.errors import AuthError, AuthUnavailableError
from app.main import create_app
from tests.conftest import TEST_DIFY_API_KEY, StaticKeyResolver


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_missing_and_non_bearer_token(client: TestClient) -> None:
    r1 = client.post("/chat/api/messages", json={"query": "hello", "stream": False})
    assert r1.status_code == 401
    assert r1.json()["code"] == "missing_token"
    assert "WWW-Authenticate" in r1.headers
    assert r1.headers["X-Request-Id"] == r1.json()["request_id"]

    r2 = client.post(
        "/chat/api/messages",
        json={"query": "hello", "stream": False},
        headers={"Authorization": "Basic abc"},
    )
    assert r2.status_code == 401
    assert r2.json()["code"] == "missing_token"

    r3 = client.post(
        "/chat/api/messages",
        json={"query": "hello", "stream": False},
        headers={"Authorization": "Bearer   "},
    )
    assert r3.status_code == 401
    assert r3.json()["code"] == "missing_token"


def test_expired_and_invalid_tokens(
    client: TestClient,
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    # Malformed token
    r_mal = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert r_mal.status_code == 401
    assert r_mal.json()["code"] == "invalid_token"

    # Expired beyond 60s leeway
    expired = make_token("wang", ttl=-120)
    r_exp = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r_exp.status_code == 401
    assert r_exp.json()["code"] == "token_expired"

    # Expired within 60s leeway -> accepted
    leeway_ok = make_token("wang", ttl=-20)
    r_leeway = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {leeway_ok}"},
    )
    assert r_leeway.status_code == 200

    # Wrong signature
    wrong_sig = make_token("wang", private_key=rsa_keys["other_private"])
    r_sig = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {wrong_sig}"},
    )
    assert r_sig.status_code == 401
    assert r_sig.json()["code"] == "invalid_token"

    # Unknown kid
    unknown_kid = make_token("wang", kid="nonexistent-kid")
    r_kid = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {unknown_kid}"},
    )
    assert r_kid.status_code == 401
    assert r_kid.json()["code"] == "invalid_token"

    # Wrong iss
    bad_iss = make_token("wang", overrides={"iss": "http://evil-idp"})
    r_iss = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {bad_iss}"},
    )
    assert r_iss.status_code == 401
    assert r_iss.json()["code"] == "invalid_token"

    # Wrong aud
    bad_aud = make_token("wang", overrides={"aud": "other"})
    r_aud = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {bad_aud}"},
    )
    assert r_aud.status_code == 401
    assert r_aud.json()["code"] == "invalid_token"

    # Algorithm confusion (HS256)
    hs_token = jwt.encode(
        {
            "iss": "http://dev-idp:8010",
            "aud": "mcpgate",
            "sub": "u1001",
            "iat": 1700000000,
            "exp": 9999999999,
            "role": "agent",
            "regions": "east",
        },
        "0123456789abcdef0123456789abcdef",
        algorithm="HS256",
        headers={"kid": rsa_keys["kid"]},
    )
    r_alg = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {hs_token}"},
    )
    assert r_alg.status_code == 401
    assert r_alg.json()["code"] == "invalid_token"

    # Empty sub
    empty_sub = make_token("wang", overrides={"sub": "   "})
    r_sub = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {empty_sub}"},
    )
    assert r_sub.status_code == 401
    assert r_sub.json()["code"] == "invalid_token"

    # Missing required exp claim
    no_exp = make_token("wang", drop_claims={"exp"})
    r_no_exp = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {no_exp}"},
    )
    assert r_no_exp.status_code == 401
    assert r_no_exp.json()["code"] == "invalid_token"


def test_invalid_claims_and_regions_array(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    # Bad role
    bad_role = make_token("wang", overrides={"role": "admin"})
    r1 = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {bad_role}"},
    )
    assert r1.status_code == 403
    assert r1.json()["code"] == "invalid_claims"

    # Empty regions string
    empty_regions = make_token("wang", overrides={"regions": " , "})
    r2 = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {empty_regions}"},
    )
    assert r2.status_code == 403
    assert r2.json()["code"] == "invalid_claims"

    # Non-string / non-list regions
    int_regions = make_token("wang", overrides={"regions": 123})
    r3 = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {int_regions}"},
    )
    assert r3.status_code == 403
    assert r3.json()["code"] == "invalid_claims"

    # Regions list with non-string item
    mixed_regions = make_token("wang", overrides={"regions": ["east", 42]})
    r3b = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {mixed_regions}"},
    )
    assert r3b.status_code == 403
    assert r3b.json()["code"] == "invalid_claims"

    # Regions as JSON string array & optional claims omitted
    arr_token = make_token(
        "wang",
        overrides={"regions": ["east", "north"]},
        drop_claims={"preferred_username", "name", "jti"},
    )
    r4 = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers={"Authorization": f"Bearer {arr_token}"},
    )
    assert r4.status_code == 200
    assert r4.json()["conversation_id"] == "conv-1"


def test_configurable_claim_names(
    build_app: Callable[..., Any],
    make_token: Callable[..., str],
) -> None:
    custom_settings = Settings(
        dify_base_url="http://dify.internal",
        dify_api_key=TEST_DIFY_API_KEY,
        jwt_role_claim="corp_role",
        jwt_regions_claim="corp_groups",
    )
    app = build_app(custom_settings=custom_settings)
    token = make_token(
        "li",
        overrides={"corp_role": "supervisor", "corp_groups": ["east", "south"]},
        drop_claims={"role", "regions"},
    )
    with TestClient(app) as tc:
        resp = tc.post(
            "/chat/api/messages",
            json={"query": "查一下退款", "stream": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200


def test_jwks_unavailable_returns_503(
    client: TestClient,
    key_resolver: StaticKeyResolver,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    key_resolver.unavailable = True
    r = client.post(
        "/chat/api/messages",
        json={"query": "hi", "stream": False},
        headers=auth_headers("wang"),
    )
    assert r.status_code == 503
    assert r.json()["code"] == "auth_unavailable"


def test_jwks_key_resolver_distinguishes_token_and_jwks_errors(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    valid_token = make_token("wang")
    other_kid_token = make_token("wang", kid="rotated-kid")

    # 1. Token issues -> 401 invalid_token (must not call JWKS)
    resolver = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
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

    # 2. Cache hit, key rotation refresh hit, and unknown kid after refresh -> 401
    primary_jwk = MagicMock(key="PUBKEY_1", key_id=rsa_keys["kid"])
    rotated_jwk = MagicMock(key="PUBKEY_2", key_id="rotated-kid")
    resolver = JwksKeyResolver(
        "http://dev-idp:8010/.well-known/jwks.json",
        cache_seconds=60,
        refresh_cooldown_seconds=0,
    )
    resolver._client = MagicMock()
    resolver._client.match_kid.side_effect = lambda keys, kid: next(
        (k for k in keys if k.key_id == kid), None
    )
    resolver._client.get_signing_keys.return_value = [primary_jwk]
    assert resolver.get_signing_key(valid_token) == "PUBKEY_1"

    resolver._client.get_signing_keys.side_effect = [[primary_jwk], [primary_jwk, rotated_jwk]]
    assert resolver.get_signing_key(other_kid_token) == "PUBKEY_2"
    assert resolver._client.get_signing_keys.call_args_list[-1].kwargs == {"refresh": True}

    resolver._client.get_signing_keys.side_effect = [[primary_jwk], [primary_jwk]]
    with pytest.raises(AuthError) as missing_kid_exc:
        resolver.get_signing_key(other_kid_token)
    assert missing_kid_exc.value.status_code == 401
    assert missing_kid_exc.value.code == "invalid_token"

    # 3. JWKS fetch / parse / empty keyset errors -> 503 auth_unavailable
    for err in [
        PyJWKClientConnectionError("net err"),
        json.JSONDecodeError("Expecting value", "<html>502</html>", 0),
        PyJWKClientError("The JWKS endpoint did not return a JSON object"),
        PyJWKSetError("The JWK Set did not contain any keys"),
        PyJWKClientError("The JWKS endpoint did not contain any signing keys"),
        TypeError("unexpected jwks structure"),
        ConnectionResetError("connection reset by peer"),
        OSError("connection reset"),
    ]:
        resolver._client.get_signing_keys.side_effect = err
        with pytest.raises(AuthUnavailableError) as unavail_exc:
            resolver.get_signing_key(valid_token)
        assert unavail_exc.value.status_code == 503
        assert unavail_exc.value.code == "auth_unavailable"

    resolver._client.get_signing_keys.side_effect = None
    resolver._client.get_signing_keys.return_value = []
    with pytest.raises(AuthUnavailableError):
        resolver.get_signing_key(valid_token)

    # 4. Real PyJWKClient with malformed / non-JSON / empty JWKS payloads -> 503
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

    # 5. Real PyJWKClient with valid JWKS: matching kid succeeds, unknown kid -> 401
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


def test_m3_jwks_cooldown_rotation_cache_expiry_and_connection_reset(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    # Default config values for cooldown & timeout
    default_cfg = Settings(dify_api_key=TEST_DIFY_API_KEY)
    assert default_cfg.jwks_refresh_cooldown_seconds == 30.0
    assert default_cfg.jwks_timeout_seconds == 5.0

    jwk_1 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["public"]))
    jwk_1.update({"kid": rsa_keys["kid"], "use": "sig", "alg": "RS256"})
    jwk_2 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["other_public"]))
    jwk_2.update({"kid": "rotated-kid-2", "use": "sig", "alg": "RS256"})

    active_payload = [json.dumps({"keys": [jwk_1]}).encode("utf-8")]
    fetch_count = 0
    seen_timeouts: list[float] = []

    def _open_counting(*_args: Any, **kwargs: Any) -> MagicMock:
        nonlocal fetch_count
        fetch_count += 1
        seen_timeouts.append(kwargs.get("timeout"))
        resp = MagicMock()
        resp.read.return_value = active_payload[0]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    fake_opener = MagicMock()
    fake_opener.open.side_effect = _open_counting

    clock = [1000.0]
    resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=300,
        refresh_cooldown_seconds=30.0,
        timeout_seconds=5.0,
        monotonic=lambda: clock[0],
    )
    valid_token = make_token("wang")
    rotated_token = make_token(
        "wang",
        private_key=rsa_keys["other_private"],
        kid="rotated-kid-2",
    )

    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fake_opener):
        # 1. Warm cache with valid kid -> 1 fetch, timeout=5.0 passed to PyJWKClient
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 1
        assert seen_timeouts == [5.0]

        # 2. 100 random unknown kids within cooldown -> only 1 forced refresh total
        for idx in range(100):
            unknown_tok = make_token("wang", kid=f"random-kid-{idx}")
            with pytest.raises(AuthError) as exc_info:
                resolver.get_signing_key(unknown_tok)
            assert exc_info.value.status_code == 401
            assert exc_info.value.code == "invalid_token"
        assert fetch_count == 2

        # Still inside cooldown (29.5s elapsed) -> no additional fetch
        clock[0] += 29.5
        with pytest.raises(AuthError):
            resolver.get_signing_key(make_token("wang", kid="still-cooling"))
        assert fetch_count == 2

        # 3. Cache expiry during cooldown still fetches normally (refresh=False)
        assert resolver._client.jwk_set_cache is not None
        resolver._client.jwk_set_cache.put(None)
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 3

        # 4. After cooldown expires (>= 30s), unknown kid triggers a fresh pull & key rotation works
        active_payload[0] = json.dumps({"keys": [jwk_1, jwk_2]}).encode("utf-8")
        clock[0] += 0.5  # total 30.0s since forced refresh at t=1000.0
        rotated_key = resolver.get_signing_key(rotated_token)
        assert rotated_key is not None
        assert fetch_count == 4
        # Verify decoded token with rotated key succeeds
        decoded = jwt.decode(
            rotated_token,
            rotated_key,
            algorithms=["RS256"],
            issuer="http://dev-idp:8010",
            audience="mcpgate",
        )
        assert decoded["sub"] == "u1001"

    # 5. Immediate key rotation (first unknown kid with fresh resolver) succeeds in one refresh
    fresh_fetches = 0
    fresh_payload = [json.dumps({"keys": [jwk_1]}).encode("utf-8")]

    def _open_fresh(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal fresh_fetches
        fresh_fetches += 1
        resp = MagicMock()
        resp.read.return_value = fresh_payload[0]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    fresh_opener = MagicMock()
    fresh_opener.open.side_effect = _open_fresh
    fresh_resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=300,
        refresh_cooldown_seconds=30.0,
        timeout_seconds=5.0,
    )
    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fresh_opener):
        assert fresh_resolver.get_signing_key(valid_token) is not None
        fresh_payload[0] = json.dumps({"keys": [jwk_1, jwk_2]}).encode("utf-8")
        assert fresh_resolver.get_signing_key(rotated_token) is not None
        assert fresh_fetches == 2

    # 6. ConnectionResetError mid-read on real PyJWKClient -> 503 auth_unavailable
    reset_resp = MagicMock()
    reset_resp.read.side_effect = ConnectionResetError("peer reset while reading body")
    reset_resp.__enter__.return_value = reset_resp
    reset_resp.__exit__.return_value = False
    reset_opener = MagicMock()
    reset_opener.open.return_value = reset_resp
    reset_resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=60,
        refresh_cooldown_seconds=30.0,
        timeout_seconds=5.0,
    )
    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=reset_opener):
        with pytest.raises(AuthUnavailableError) as reset_exc:
            reset_resolver.get_signing_key(valid_token)
        assert reset_exc.value.status_code == 503
        assert reset_exc.value.code == "auth_unavailable"


def test_l1_failed_force_refresh_returns_503_during_cooldown_and_recovers(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    jwk_1 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["public"]))
    jwk_1.update({"kid": rsa_keys["kid"], "use": "sig", "alg": "RS256"})
    jwk_2 = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_keys["other_public"]))
    jwk_2.update({"kid": "rotated-kid-l1", "use": "sig", "alg": "RS256"})

    state: dict[str, Any] = {
        "mode": "ok_1",
        "fetches": 0,
    }

    def _open_jwks(*_args: Any, **_kwargs: Any) -> MagicMock:
        state["fetches"] += 1
        if state["mode"] == "fail":
            raise TimeoutError("idp timeout")
        payload = {"keys": [jwk_1]} if state["mode"] == "ok_1" else {"keys": [jwk_1, jwk_2]}
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    fake_opener = MagicMock()
    fake_opener.open.side_effect = _open_jwks
    clock = [2000.0]
    resolver = JwksKeyResolver(
        "http://127.0.0.1:9999/.well-known/jwks.json",
        cache_seconds=300,
        refresh_cooldown_seconds=30.0,
        timeout_seconds=5.0,
        monotonic=lambda: clock[0],
    )
    valid_token = make_token("wang")
    rotated_token = make_token(
        "wang",
        private_key=rsa_keys["other_private"],
        kid="rotated-kid-l1",
    )

    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=fake_opener):
        # Warm cache with initial key
        assert resolver.get_signing_key(valid_token) is not None
        assert state["fetches"] == 1

        # IdP fails during forced refresh on unknown kid -> 503 auth_unavailable
        state["mode"] = "fail"
        with pytest.raises(AuthUnavailableError) as first_fail:
            resolver.get_signing_key(rotated_token)
        assert first_fail.value.status_code == 503
        assert first_fail.value.code == "auth_unavailable"
        assert state["fetches"] == 2

        # Within cooldown: unknown kid returns 503 without any new JWKS request
        clock[0] += 10.0
        with pytest.raises(AuthUnavailableError) as cooled_fail:
            resolver.get_signing_key(rotated_token)
        assert cooled_fail.value.status_code == 503
        assert cooled_fail.value.code == "auth_unavailable"
        assert state["fetches"] == 2

        # Cached known kid still succeeds during cooldown
        assert resolver.get_signing_key(valid_token) is not None
        assert state["fetches"] == 2

        # After cooldown: IdP recovered -> forced refresh succeeds and rotated token passes
        state["mode"] = "ok_12"
        clock[0] += 20.0  # 30.0s elapsed
        rotated_key = resolver.get_signing_key(rotated_token)
        assert rotated_key is not None
        assert state["fetches"] == 3

        # Subsequent unknown kid within the new cooldown after a successful refresh -> 401
        clock[0] += 5.0
        with pytest.raises(AuthError) as after_recover:
            resolver.get_signing_key(make_token("wang", kid="nonexistent-after-recover"))
        assert after_recover.value.status_code == 401
        assert after_recover.value.code == "invalid_token"
        assert state["fetches"] == 3


def test_l2_jwks_config_validation_and_create_app_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # create_app forwards custom cooldown and timeout to JwksKeyResolver
    custom_cfg = Settings(
        dify_api_key=TEST_DIFY_API_KEY,
        jwks_refresh_cooldown_seconds=12.5,
        jwks_timeout_seconds=3.5,
    )
    app = create_app(settings=custom_cfg)
    resolver = app.state.signing_key_resolver
    assert isinstance(resolver, JwksKeyResolver)
    assert resolver._refresh_cooldown_seconds == 12.5
    assert resolver._timeout_seconds == 3.5
    assert resolver._client.timeout == 3.5

    # Zero cooldown is valid (explicitly disables cooldown)
    zero_cooldown_cfg = Settings(
        dify_api_key=TEST_DIFY_API_KEY,
        jwks_refresh_cooldown_seconds=0.0,
        jwks_timeout_seconds=0.5,
    )
    assert zero_cooldown_cfg.jwks_refresh_cooldown_seconds == 0.0
    assert zero_cooldown_cfg.jwks_timeout_seconds == 0.5

    # Invalid timeout values (<= 0 or non-finite) fail validation
    for bad_timeout in (0, 0.0, -1.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            Settings(dify_api_key=TEST_DIFY_API_KEY, jwks_timeout_seconds=bad_timeout)

    # Invalid cooldown values (< 0 or non-finite) fail validation
    for bad_cooldown in (-0.1, -10.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            Settings(dify_api_key=TEST_DIFY_API_KEY, jwks_refresh_cooldown_seconds=bad_cooldown)

    # Startup via create_app() with invalid env vars fails immediately
    monkeypatch.setenv("CHAT_JWKS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        create_app()
    monkeypatch.delenv("CHAT_JWKS_TIMEOUT_SECONDS")

    monkeypatch.setenv("CHAT_JWKS_REFRESH_COOLDOWN_SECONDS", "-5")
    with pytest.raises(ValidationError):
        create_app()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/chat/api/messages", {"query": "hello", "stream": False}),
        ("GET", "/chat/api/conversations/conv-1/messages", None),
    ],
)
def test_all_protected_endpoints_require_token(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401
    assert resp.json()["code"] == "missing_token"
