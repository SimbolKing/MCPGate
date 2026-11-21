from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError
from pydantic import ValidationError

from app.auth import JwksKeyResolver
from app.config import Settings
from app.errors import AuthError, AuthUnavailableError
from app.main import create_app
from app.manifest import ToolManifest
from tests.conftest import MANIFEST_PATH, FakeUpstream, StaticKeyResolver


def test_missing_and_non_bearer_token(client: TestClient) -> None:
    r1 = client.get("/api/v1/orders/A1023")
    assert r1.status_code == 401
    assert r1.json()["code"] == "missing_token"
    assert "WWW-Authenticate" in r1.headers
    assert r1.headers["X-Request-Id"] == r1.json()["request_id"]

    r2 = client.get("/api/v1/orders/A1023", headers={"Authorization": "Basic abc"})
    assert r2.status_code == 401
    assert r2.json()["code"] == "missing_token"

    r3 = client.get("/api/v1/orders/A1023", headers={"Authorization": "Bearer   "})
    assert r3.status_code == 401
    assert r3.json()["code"] == "missing_token"


def test_expired_and_invalid_tokens(
    client: TestClient,
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    # Expired beyond 60s leeway
    expired = make_token("wang", ttl=-120)
    r_exp = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {expired}"})
    assert r_exp.status_code == 401
    assert r_exp.json()["code"] == "token_expired"

    # Wrong signature
    wrong_sig = make_token("wang", private_key=rsa_keys["other_private"])
    r_sig = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {wrong_sig}"})
    assert r_sig.status_code == 401
    assert r_sig.json()["code"] == "invalid_token"

    # Unknown kid
    unknown_kid = make_token("wang", kid="nonexistent-kid")
    r_kid = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {unknown_kid}"})
    assert r_kid.status_code == 401
    assert r_kid.json()["code"] == "invalid_token"

    # Wrong iss
    bad_iss = make_token("wang", overrides={"iss": "http://evil-idp"})
    r_iss = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {bad_iss}"})
    assert r_iss.status_code == 401
    assert r_iss.json()["code"] == "invalid_token"

    # Wrong aud
    bad_aud = make_token("wang", overrides={"aud": "other"})
    r_aud = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {bad_aud}"})
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
    r_alg = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {hs_token}"})
    assert r_alg.status_code == 401
    assert r_alg.json()["code"] == "invalid_token"

    # Empty sub
    empty_sub = make_token("wang", overrides={"sub": "   "})
    r_sub = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {empty_sub}"})
    assert r_sub.status_code == 401
    assert r_sub.json()["code"] == "invalid_token"


def test_invalid_claims_and_regions_array(
    client: TestClient,
    make_token: Callable[..., str],
) -> None:
    # Bad role
    bad_role = make_token("wang", overrides={"role": "admin"})
    r1 = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {bad_role}"})
    assert r1.status_code == 403
    assert r1.json()["code"] == "invalid_claims"

    # Empty regions string
    empty_regions = make_token("wang", overrides={"regions": " , "})
    r2 = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {empty_regions}"})
    assert r2.status_code == 403
    assert r2.json()["code"] == "invalid_claims"

    # Non-string / non-list regions
    int_regions = make_token("wang", overrides={"regions": 123})
    r3 = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {int_regions}"})
    assert r3.status_code == 403
    assert r3.json()["code"] == "invalid_claims"

    # Regions as JSON string array (G3 compatibility) & optional claims omitted
    arr_token = make_token(
        "wang",
        overrides={"regions": ["east"]},
        drop_claims={"preferred_username", "name", "jti"},
    )
    r4 = client.get("/api/v1/orders/A1023", headers={"Authorization": f"Bearer {arr_token}"})
    assert r4.status_code == 200
    assert r4.json()["order_id"] == "A1023"


def test_jwks_unavailable_returns_503(
    client: TestClient,
    key_resolver: StaticKeyResolver,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    key_resolver.unavailable = True
    r = client.get("/api/v1/orders/A1023", headers=auth_headers("wang"))
    assert r.status_code == 503
    assert r.json()["code"] == "auth_unavailable"


def _public_jwk(pub_key: Any, kid: str, *, use: str = "sig") -> dict[str, Any]:
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    jwk_dict["use"] = use
    jwk_dict["alg"] = "RS256"
    return jwk_dict


def test_jwks_key_resolver_distinguishes_token_and_jwks_errors(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    valid_token = make_token("wang")
    other_kid_token = make_token(
        "wang",
        kid=rsa_keys["other_kid"],
        private_key=rsa_keys["other_private"],
    )
    no_kid_token = jwt.encode(
        {"sub": "u1001"},
        rsa_keys["private"],
        algorithm="RS256",
    )
    blank_kid_token = jwt.encode(
        {"sub": "u1001"},
        rsa_keys["private"],
        algorithm="RS256",
        headers={"kid": "   "},
    )

    # 1. Malformed token header / missing kid / blank kid -> 401 invalid_token (never hits JWKS)
    resolver = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
    resolver._client.get_signing_keys = MagicMock(side_effect=AssertionError("should not fetch"))
    for bad_tok in ("not-a-jwt", no_kid_token, blank_kid_token):
        with pytest.raises(AuthError) as exc_info:
            resolver.get_signing_key(bad_tok)
        assert exc_info.value.code == "invalid_token"

    # 2. Normal JWKS hit, key rotation refresh hit, and unknown kid -> 401
    primary_jwk = _public_jwk(rsa_keys["public"], rsa_keys["kid"])
    rotated_jwk = _public_jwk(rsa_keys["other_public"], rsa_keys["other_kid"])
    jwks_responses = [
        {"keys": [primary_jwk]},
        {"keys": [primary_jwk, rotated_jwk]},
    ]
    fetch_idx = 0

    def _fake_fetch() -> dict[str, Any]:
        nonlocal fetch_idx
        payload = jwks_responses[min(fetch_idx, len(jwks_responses) - 1)]
        fetch_idx += 1
        return payload

    resolver_ok = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
    resolver_ok._client.fetch_data = _fake_fetch  # type: ignore[method-assign]
    assert resolver_ok.get_signing_key(valid_token) is not None
    # Key rotation: not in cached first set, found after refresh=True
    assert resolver_ok.get_signing_key(other_kid_token) is not None
    # Unknown kid even after refresh -> 401 invalid_token (fresh resolver outside cooldown)
    resolver_miss = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
    resolver_miss._client.fetch_data = MagicMock(return_value={"keys": [primary_jwk]})
    unknown_kid_token = make_token("wang", kid="definitely-unknown")
    with pytest.raises(AuthError) as exc_unknown:
        resolver_miss.get_signing_key(unknown_kid_token)
    assert exc_unknown.value.code == "invalid_token"
    assert resolver_miss._client.fetch_data.call_count == 2

    # 3. JWKS fetch / parse / structure / empty-key failures -> 503 auth_unavailable
    for bad_exc in (
        PyJWKClientConnectionError("down"),
        json.JSONDecodeError("not json", "<html>502</html>", 0),
        PyJWKSetError("malformed set"),
        PyJWKClientError("The JWKS endpoint did not contain any signing keys"),
        TypeError("unexpected jwks payload type"),
        OSError("connection reset"),
        ConnectionResetError("connection reset by peer"),
    ):
        r_err = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
        r_err._client.get_signing_keys = MagicMock(side_effect=bad_exc)
        with pytest.raises(AuthUnavailableError):
            r_err.get_signing_key(valid_token)

    # Empty signing_keys list -> 503 auth_unavailable
    r_empty = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
    r_empty._client.get_signing_keys = MagicMock(return_value=[])
    with pytest.raises(AuthUnavailableError):
        r_empty.get_signing_key(valid_token)

    # 4. Real PyJWKClient parsing paths: non-dict, missing/empty/non-sig keys -> 503
    for bad_payload in (
        ["not-a-dict"],
        {"not_keys": []},
        {"keys": []},
        {"keys": [_public_jwk(rsa_keys["public"], rsa_keys["kid"], use="enc")]},
    ):
        r_bad = JwksKeyResolver("http://dev-idp:8010/.well-known/jwks.json", cache_seconds=60)
        r_bad._client.fetch_data = MagicMock(return_value=bad_payload)
        with pytest.raises(AuthUnavailableError):
            r_bad.get_signing_key(valid_token)


def test_jwks_unknown_kid_cooldown_rotation_timeout_and_connection_reset(
    make_token: Callable[..., str],
    rsa_keys: dict[str, Any],
) -> None:
    now = [1000.0]

    def _fake_monotonic() -> float:
        return now[0]

    primary_jwk = _public_jwk(rsa_keys["public"], rsa_keys["kid"])
    rotated_jwk = _public_jwk(rsa_keys["other_public"], rsa_keys["other_kid"])
    current_jwks = [{"keys": [primary_jwk]}]
    fetch_count = 0

    def _fake_open(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal fetch_count
        fetch_count += 1
        resp = MagicMock()
        resp.read.return_value = json.dumps(current_jwks[0]).encode()
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
        # Warm cache with valid token -> 1 fetch
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 1

        # 100 random unknown kids within cooldown -> only 1 forced refresh
        for i in range(100):
            now[0] = 1000.0 + (i * 0.2)
            unknown_tok = make_token("wang", kid=f"random-unknown-{i}")
            with pytest.raises(AuthError) as exc_info:
                resolver.get_signing_key(unknown_tok)
            assert exc_info.value.status_code == 401
            assert exc_info.value.code == "invalid_token"
        assert fetch_count == 2

        # Cache expiry during cooldown still allows normal fetch (refresh=False)
        assert resolver._client.jwk_set_cache is not None
        resolver._client.jwk_set_cache.put(None)  # type: ignore[arg-type]
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 3

        # After cooldown elapses, unknown kid triggers refresh and rotated key succeeds
        now[0] = 1030.0
        current_jwks[0] = {"keys": [primary_jwk, rotated_jwk]}
        assert resolver.get_signing_key(rotated_token) is not None
        assert fetch_count == 4

        # After another cooldown, still-missing kid triggers 1 more refresh and raises 401
        now[0] = 1061.0
        with pytest.raises(AuthError) as still_unknown:
            resolver.get_signing_key(make_token("wang", kid="still-missing"))
        assert still_unknown.value.status_code == 401
        assert still_unknown.value.code == "invalid_token"
        assert fetch_count == 5

    # ConnectionResetError mid-body read -> 503 auth_unavailable
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

    primary_jwk = _public_jwk(rsa_keys["public"], rsa_keys["kid"])
    rotated_jwk = _public_jwk(rsa_keys["other_public"], rsa_keys["other_kid"])
    state: dict[str, Any] = {"fail": False, "jwks": {"keys": [primary_jwk]}}
    fetch_count = 0

    def _fake_open(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal fetch_count
        fetch_count += 1
        if state["fail"]:
            raise OSError("idp temporarily unavailable")
        resp = MagicMock()
        resp.read.return_value = json.dumps(state["jwks"]).encode()
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
        # 1. Warm cache -> 1 fetch
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 1

        # 2. Forced refresh fails on unknown kid -> 503 auth_unavailable
        state["fail"] = True
        with pytest.raises(AuthUnavailableError) as exc1:
            resolver.get_signing_key(rotated_token)
        assert exc1.value.status_code == 503
        assert exc1.value.code == "auth_unavailable"
        assert fetch_count == 2

        # 3. Within cooldown, unknown kid returns 503 without any new JWKS fetch
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

        # Cached key still verifies normally during cooldown
        assert resolver.get_signing_key(valid_token) is not None
        assert fetch_count == 2

        # 4. After cooldown elapses and IdP recovers, same rotated token succeeds
        now[0] = 1030.0
        state["fail"] = False
        state["jwks"] = {"keys": [primary_jwk, rotated_jwk]}
        assert resolver.get_signing_key(rotated_token) is not None
        assert fetch_count == 3

        # 5. After successful refresh, unknown kid during new cooldown returns 401 without fetch
        now[0] = 1035.0
        with pytest.raises(AuthError) as exc_after_ok:
            resolver.get_signing_key(make_token("wang", kid="missing-after-ok"))
        assert exc_after_ok.value.status_code == 401
        assert exc_after_ok.value.code == "invalid_token"
        assert fetch_count == 3


def test_create_app_passes_jwks_settings_and_rejects_invalid_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_cfg = Settings(
        database_url=f"sqlite:///{tmp_path / 'jwks_cfg.db'}",
        tools_manifest=str(MANIFEST_PATH),
        jwks_refresh_cooldown_seconds=12.5,
        jwks_timeout_seconds=2.5,
    )
    app = create_app(
        settings=custom_cfg,
        manifest=ToolManifest.from_file(MANIFEST_PATH),
    )
    resolver = app.state.signing_key_resolver
    assert isinstance(resolver, JwksKeyResolver)
    assert resolver._refresh_cooldown_seconds == 12.5
    assert resolver._client.timeout == 2.5
    app.state.upstream.close()
    app.state.engine.dispose()

    # Cooldown = 0 is valid
    zero_cooldown = Settings(jwks_refresh_cooldown_seconds=0.0, jwks_timeout_seconds=0.5)
    assert zero_cooldown.jwks_refresh_cooldown_seconds == 0.0
    assert zero_cooldown.jwks_timeout_seconds == 0.5

    # Invalid values fail startup configuration validation
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

    monkeypatch.setenv("GUARD_JWKS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        create_app(manifest=ToolManifest.from_file(MANIFEST_PATH))


def test_multiple_authorization_headers_rejected_and_verified_header_forwarded(
    client: TestClient,
    fake_upstream: FakeUpstream,
    make_token: Callable[..., str],
) -> None:
    wang_tok = make_token("wang")
    sun_tok = make_token("sun")

    # Two Authorization headers -> 401 invalid_token, never reaches upstream
    r_dup = client.get(
        "/api/v1/orders/A1023",
        headers=[
            ("Authorization", f"Bearer {wang_tok}"),
            ("Authorization", f"Bearer {sun_tok}"),
        ],
    )
    assert r_dup.status_code == 401
    assert r_dup.json()["code"] == "invalid_token"
    assert len(fake_upstream.requests) == 0

    # Same for write endpoint and console endpoint
    r_dup_write = client.post(
        "/api/v1/refunds",
        json={
            "order_id": "A1023",
            "amount": 10,
            "reason_category": "other",
            "reason": "test",
        },
        headers=[
            ("Authorization", f"Bearer {wang_tok}"),
            ("Authorization", f"Bearer {sun_tok}"),
        ],
    )
    assert r_dup_write.status_code == 401
    assert r_dup_write.json()["code"] == "invalid_token"
    assert len(fake_upstream.requests) == 0

    # Single Authorization header with exact raw value is forwarded verbatim
    raw_auth = f"Bearer   {wang_tok}"
    r_ok = client.get("/api/v1/orders/A1023", headers={"Authorization": raw_auth})
    assert r_ok.status_code == 200
    assert fake_upstream.requests[-1].headers["authorization"] == raw_auth


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/orders/A1023"),
        ("GET", "/api/v1/orders"),
        ("POST", "/api/v1/refunds"),
        ("GET", "/guard/v1/approvals"),
        ("GET", "/guard/v1/approvals/AP000001"),
        ("GET", "/guard/v1/console/approvals"),
        ("GET", "/guard/v1/console/approvals/AP000001"),
        ("GET", "/guard/v1/console/approvals/AP000001/order"),
        ("POST", "/guard/v1/console/approvals/AP000001/approve"),
        ("POST", "/guard/v1/console/approvals/AP000001/reject"),
        ("POST", "/guard/v1/console/approvals/AP000001/retry"),
        ("GET", "/guard/v1/console/audit-logs"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
    ],
)
def test_all_protected_endpoints_require_token(
    client: TestClient,
    method: str,
    path: str,
) -> None:
    resp = client.request(method, path)
    assert resp.status_code == 401
    assert resp.json()["code"] == "missing_token"


def test_public_endpoints_and_no_openapi(
    client: TestClient,
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    assert client.get("/healthz").status_code == 200
    r_root = client.get("/", follow_redirects=False)
    assert r_root.status_code == 307
    assert r_root.headers["location"] == "/console/"

    # OpenAPI / Swagger are disabled (returns 404 route_not_found when authenticated)
    assert client.get("/openapi.json", headers=auth_headers("wang")).status_code == 404
    assert client.get("/docs", headers=auth_headers("wang")).status_code == 404
