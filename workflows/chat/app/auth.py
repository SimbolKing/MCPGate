from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import Settings
from app.errors import AuthError, AuthUnavailableError, ForbiddenError
from app.log import log_auth_failed

logger = logging.getLogger("chat.auth")

bearer_scheme = HTTPBearer(scheme_name="BearerAuth", bearerFormat="JWT", auto_error=False)

ALLOWED_ROLES = frozenset({"agent", "supervisor"})


@dataclass(frozen=True)
class Principal:
    sub: str
    username: str
    name: str | None
    role: str
    regions: frozenset[str]
    jti: str | None


class SigningKeyResolver(Protocol):
    def get_signing_key(self, token: str) -> Any: ...


class JwksKeyResolver:
    def __init__(
        self,
        jwks_url: str,
        cache_seconds: int,
        *,
        refresh_cooldown_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = PyJWKClient(
            jwks_url,
            cache_keys=True,
            lifespan=cache_seconds,
            timeout=timeout_seconds,
        )
        self._lock = threading.RLock()
        self._refresh_cooldown_seconds = refresh_cooldown_seconds
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._last_forced_refresh: float | None = None
        self._last_refresh_failed: bool = False

    def _fetch_signing_keys(self, *, refresh: bool = False) -> list[Any]:
        try:
            signing_keys = self._client.get_signing_keys(refresh=refresh)
        except (jwt.PyJWTError, ValueError, TypeError, OSError) as exc:
            raise AuthUnavailableError() from exc
        if not signing_keys:
            raise AuthUnavailableError()
        return signing_keys

    def _can_force_refresh(self, now: float) -> bool:
        if self._last_forced_refresh is None:
            return True
        return (now - self._last_forced_refresh) >= self._refresh_cooldown_seconds

    def get_signing_key(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("invalid_token", "身份令牌无效，请重新登录。") from exc

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid.strip():
            raise AuthError("invalid_token", "身份令牌无效，请重新登录。")

        with self._lock:
            signing_keys = self._fetch_signing_keys(refresh=False)
            signing_key = self._client.match_kid(signing_keys, kid)
            if signing_key is None:
                now = self._monotonic()
                if self._can_force_refresh(now):
                    self._last_forced_refresh = now
                    try:
                        signing_keys = self._fetch_signing_keys(refresh=True)
                    except AuthUnavailableError:
                        self._last_refresh_failed = True
                        raise
                    self._last_refresh_failed = False
                    signing_key = self._client.match_kid(signing_keys, kid)
                elif self._last_refresh_failed:
                    raise AuthUnavailableError()
        if signing_key is None:
            raise AuthError("invalid_token", "身份令牌无效，请重新登录。")
        return signing_key.key


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_signing_key_resolver(request: Request) -> SigningKeyResolver:
    return request.app.state.signing_key_resolver


def _parse_regions(raw_regions: Any) -> frozenset[str]:
    if isinstance(raw_regions, str):
        return frozenset(part.strip() for part in raw_regions.split(",") if part.strip())
    if isinstance(raw_regions, list) and all(isinstance(item, str) for item in raw_regions):
        return frozenset(item.strip() for item in raw_regions if item.strip())
    return frozenset()


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_authenticated_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    key_resolver: SigningKeyResolver = Depends(get_signing_key_resolver),
) -> tuple[Principal, str]:
    request_id = getattr(request.state, "request_id", "")

    if credentials is None or not credentials.credentials.strip():
        log_auth_failed(
            logger,
            request_id=request_id,
            reason="missing_or_non_bearer_authorization_header",
        )
        raise AuthError("missing_token", "缺少有效的身份令牌，请先登录。")

    token = credentials.credentials.strip()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        log_auth_failed(logger, request_id=request_id, reason="malformed_token_header")
        raise AuthError("invalid_token", "身份令牌无效，请重新登录。") from exc

    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in settings.jwt_algorithm_list:
        log_auth_failed(logger, request_id=request_id, reason="disallowed_algorithm")
        raise AuthError("invalid_token", "身份令牌无效，请重新登录。")

    try:
        signing_key = key_resolver.get_signing_key(token)
    except AuthUnavailableError:
        log_auth_failed(logger, request_id=request_id, reason="jwks_unavailable")
        raise
    except AuthError:
        log_auth_failed(logger, request_id=request_id, reason="signing_key_resolution_failed")
        raise

    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=settings.jwt_algorithm_list,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        log_auth_failed(logger, request_id=request_id, reason="expired_signature")
        raise AuthError("token_expired", "身份令牌已过期，请重新登录。") from exc
    except jwt.PyJWTError as exc:
        log_auth_failed(logger, request_id=request_id, reason=type(exc).__name__)
        raise AuthError("invalid_token", "身份令牌无效，请重新登录。") from exc

    sub = _optional_str(payload.get("sub"))
    if sub is None:
        log_auth_failed(logger, request_id=request_id, reason="empty_sub_claim")
        raise AuthError("invalid_token", "身份令牌无效，请重新登录。")

    role = payload.get(settings.jwt_role_claim)
    if not isinstance(role, str) or role not in ALLOWED_ROLES:
        log_auth_failed(logger, request_id=request_id, reason="invalid_role_claim")
        raise ForbiddenError("invalid_claims", "身份令牌中的角色声明无效。")

    regions = _parse_regions(payload.get(settings.jwt_regions_claim))
    if not regions:
        log_auth_failed(logger, request_id=request_id, reason="empty_regions_claim")
        raise ForbiddenError("invalid_claims", "身份令牌中缺少有效的负责区域声明。")

    username = _optional_str(payload.get("preferred_username")) or sub
    name = _optional_str(payload.get("name"))
    jti = _optional_str(payload.get("jti"))

    principal = Principal(
        sub=sub,
        username=username,
        name=name,
        role=role,
        regions=regions,
        jti=jti,
    )
    request.state.principal = principal
    return principal, token
