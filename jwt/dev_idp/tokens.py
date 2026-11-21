from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class DevIdpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVIDP_", extra="ignore")

    issuer: str = "http://dev-idp:8010"
    audience: str = "mcpgate"
    kid: str = "mcpgate-dev-1"
    token_ttl_seconds: int = 28800
    key_file: str = "keys/private.pem"
    jwks_file: str = "keys/jwks.json"
    users_file: str = "users.json"


def resolve_path(path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute() or path.exists():
        return path
    candidate = PACKAGE_ROOT / path
    if candidate.exists():
        return candidate
    return path


def load_users(users_file: str) -> list[dict[str, Any]]:
    path = resolve_path(users_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"users file {path} must contain a JSON array")
    return data


def find_user(username: str, users_file: str) -> dict[str, Any] | None:
    for user in load_users(users_file):
        if user.get("username") == username:
            return user
    return None


def load_private_key(key_file: str) -> Any:
    path = resolve_path(key_file)
    return load_pem_private_key(path.read_bytes(), password=None)


def load_jwks(jwks_file: str) -> dict[str, Any]:
    path = resolve_path(jwks_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "keys" not in data:
        raise ValueError(f"jwks file {path} must contain a 'keys' field")
    return data


def normalize_regions(raw_regions: str | list[str]) -> str:
    if isinstance(raw_regions, list):
        items = [r.strip() for r in raw_regions if isinstance(r, str) and r.strip()]
        return ",".join(items)
    items = [r.strip() for r in raw_regions.split(",") if r.strip()]
    return ",".join(items)


def mint_token(
    user: dict[str, Any],
    settings: DevIdpSettings,
    *,
    ttl: int | None = None,
    now: int | None = None,
) -> tuple[str, int]:
    issued_at = int(time.time()) if now is None else int(now)
    expires_in = settings.token_ttl_seconds if ttl is None else int(ttl)
    payload: dict[str, Any] = {
        "iss": settings.issuer,
        "aud": settings.audience,
        "sub": user["sub"],
        "iat": issued_at,
        "exp": issued_at + expires_in,
        "jti": str(uuid.uuid4()),
        "preferred_username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "regions": normalize_regions(user["regions"]),
    }
    private_key = load_private_key(settings.key_file)
    token = jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": settings.kid, "typ": "JWT"},
    )
    return token, expires_in
