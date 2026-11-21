from __future__ import annotations

import json
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from dev_idp.app import create_app
from dev_idp.genkeys import generate_keypair
from dev_idp.mint import main as mint_main
from dev_idp.tokens import PACKAGE_ROOT, DevIdpSettings, load_users

EXPECTED_ACCOUNTS = {
    "wang": ("u1001", "王磊", "agent", "east"),
    "zhao": ("u1002", "赵宇", "agent", "north"),
    "chen": ("u1003", "陈晨", "agent", "south"),
    "li": ("u2001", "李娜", "supervisor", "east"),
    "zhou": ("u2002", "周涛", "supervisor", "north,south"),
    "sun": ("u2003", "孙浩", "supervisor", "east,north,south"),
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_jwks_structure(client: TestClient) -> None:
    resp = client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body and len(body["keys"]) == 1
    jwk = body["keys"][0]
    assert jwk["kty"] == "RSA"
    assert jwk["kid"] == "mcpgate-dev-1"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "RS256"
    assert "n" in jwk and "e" in jwk


def test_users_json_matches_g2() -> None:
    users = load_users(str(PACKAGE_ROOT / "users.json"))
    assert len(users) == 6
    by_name = {u["username"]: u for u in users}
    assert set(by_name.keys()) == set(EXPECTED_ACCOUNTS.keys())
    for username, (sub, name, role, regions_csv) in EXPECTED_ACCOUNTS.items():
        u = by_name[username]
        assert u["sub"] == sub
        assert u["name"] == name
        assert u["role"] == role
        assert ",".join(u["regions"]) == regions_csv
        assert u["password"]


@pytest.mark.parametrize("username", list(EXPECTED_ACCOUNTS.keys()))
def test_post_token_and_rs256_verify_roundtrip(client: TestClient, username: str) -> None:
    jwks_data = client.get("/.well-known/jwks.json").json()
    jwk_obj = jwt.PyJWK.from_dict(jwks_data["keys"][0])

    resp = client.post("/token", json={"username": username, "password": "dev-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 28800
    token = body["access_token"]

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["typ"] == "JWT"
    assert header["kid"] == "mcpgate-dev-1"

    claims = jwt.decode(
        token,
        jwk_obj.key,
        algorithms=["RS256"],
        issuer="http://dev-idp:8010",
        audience="mcpgate",
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
    sub, name, role, regions_csv = EXPECTED_ACCOUNTS[username]
    assert claims["sub"] == sub
    assert claims["preferred_username"] == username
    assert claims["name"] == name
    assert claims["role"] == role
    assert claims["regions"] == regions_csv
    assert claims["exp"] - claims["iat"] == 28800
    assert claims["jti"]


def test_post_token_invalid_credentials(client: TestClient) -> None:
    resp_wrong_pw = client.post("/token", json={"username": "wang", "password": "wrong"})
    assert resp_wrong_pw.status_code == 401
    assert resp_wrong_pw.json()["code"] == "invalid_credentials"
    assert "request_id" in resp_wrong_pw.json()

    resp_unknown = client.post("/token", json={"username": "nobody", "password": "dev-password"})
    assert resp_unknown.status_code == 401
    assert resp_unknown.json()["code"] == "invalid_credentials"

    resp_non_ascii = client.post("/token", json={"username": "wang", "password": "中文密码123"})
    assert resp_non_ascii.status_code == 401
    non_ascii_body = resp_non_ascii.json()
    assert non_ascii_body["code"] == "invalid_credentials"
    assert non_ascii_body["request_id"] == resp_non_ascii.headers["X-Request-Id"]


def test_mint_cli_valid_and_expired_token(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    jwks_data = client.get("/.well-known/jwks.json").json()
    jwk_obj = jwt.PyJWK.from_dict(jwks_data["keys"][0])

    rc = mint_main(["--user", "zhou", "--ttl", "120"])
    assert rc == 0
    token = capsys.readouterr().out.strip()
    claims = jwt.decode(
        token,
        jwk_obj.key,
        algorithms=["RS256"],
        issuer="http://dev-idp:8010",
        audience="mcpgate",
    )
    assert claims["sub"] == "u2002"
    assert claims["regions"] == "north,south"
    assert claims["exp"] - claims["iat"] == 120

    # 负 TTL 生成已过期 token
    rc_exp = mint_main(["--user", "wang", "--ttl", "-120"])
    assert rc_exp == 0
    expired_token = capsys.readouterr().out.strip()
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(
            expired_token,
            jwk_obj.key,
            algorithms=["RS256"],
            issuer="http://dev-idp:8010",
            audience="mcpgate",
            leeway=60,
        )

    # 未知用户返回非零退出码
    rc_err = mint_main(["--user", "nonexistent"])
    assert rc_err == 1
    assert "unknown user" in capsys.readouterr().err


def test_genkeys_creates_valid_pair(tmp_path: Path) -> None:
    key_path = tmp_path / "private.pem"
    jwks_path = tmp_path / "jwks.json"
    jwks_out = generate_keypair(key_path, jwks_path, "test-kid-1")

    pem_text = key_path.read_text(encoding="ascii")
    assert "DEV ONLY" in pem_text
    assert "BEGIN PRIVATE KEY" in pem_text

    loaded_jwks = json.loads(jwks_path.read_text(encoding="utf-8"))
    assert loaded_jwks == jwks_out
    assert loaded_jwks["keys"][0]["kid"] == "test-kid-1"
    assert loaded_jwks["keys"][0]["alg"] == "RS256"
    assert loaded_jwks["keys"][0]["use"] == "sig"

    settings = DevIdpSettings(
        key_file=str(key_path),
        jwks_file=str(jwks_path),
        kid="test-kid-1",
    )
    tmp_client = TestClient(create_app(settings))
    resp = tmp_client.post("/token", json={"username": "wang", "password": "dev-password"})
    assert resp.status_code == 200
    jwk_obj = jwt.PyJWK.from_dict(tmp_client.get("/.well-known/jwks.json").json()["keys"][0])
    decoded = jwt.decode(
        resp.json()["access_token"],
        jwk_obj.key,
        algorithms=["RS256"],
        issuer="http://dev-idp:8010",
        audience="mcpgate",
    )
    assert decoded["sub"] == "u1001"
