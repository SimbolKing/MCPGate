"""Token provider calling dev-idp mint command with in-memory caching (5.1, 5.8.3)."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from harness.config import REPO_ROOT, Settings
from harness.schema import TokenType


class TokenError(RuntimeError):
    """Raised when minting a JWT fails."""


def tamper_jwt(token: str) -> str:
    """Modify a middle character of a JWT signature so decoded bytes always change (2.13 V3)."""
    if not token:
        raise TokenError("无法篡改空 token")
    parts = token.split(".")
    if len(parts) != 3 or not parts[2]:
        raise TokenError("待篡改的 token 不是合法三段式 JWT")
    sig = parts[2]
    idx = 0 if len(sig) <= 2 else min(10, len(sig) // 2)
    replacement = "A" if sig[idx] != "A" else "B"
    parts[2] = sig[:idx] + replacement + sig[idx + 1 :]
    return ".".join(parts)


class TokenProvider:
    """Acquires and caches JWTs in memory only; never persists tokens to disk."""

    def __init__(
        self,
        settings: Settings,
        *,
        repo_root: Path = REPO_ROOT,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._settings = settings
        self._repo_root = repo_root
        self._runner = runner
        self._valid_cache: dict[str, str] = {}
        self._expired_cache: dict[str, str] = {}
        self._issued: list[str] = []

    def _mint(self, username: str, ttl: int | None = None) -> str:
        cmd = [*shlex.split(self._settings.mint_cmd), "--user", username]
        if ttl is not None:
            cmd.extend(["--ttl", str(ttl)])
        proc = self._runner(
            cmd,
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise TokenError(
                f"签发 {username} 的 token 失败 (exit={proc.returncode}): {proc.stderr.strip()}"
            )
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines or lines[-1].count(".") != 2:
            raise TokenError(f"签发 {username} 返回的不是合法 JWT")
        token = lines[-1]
        if token not in self._issued:
            self._issued.append(token)
        return token

    def get_token(self, username: str, token_type: TokenType = "valid") -> str | None:
        if token_type == "none":
            return None
        if token_type == "valid":
            if username not in self._valid_cache:
                self._valid_cache[username] = self._mint(username)
            return self._valid_cache[username]
        if token_type == "expired":
            if username not in self._expired_cache:
                self._expired_cache[username] = self._mint(username, ttl=-300)
            return self._expired_cache[username]
        if token_type == "tampered":
            valid = self.get_token(username, "valid")
            assert valid is not None
            tampered = tamper_jwt(valid)
            if tampered not in self._issued:
                self._issued.append(tampered)
            return tampered
        raise TokenError(f"不支持的 token 类型: {token_type}")

    def prime_accounts(self, usernames: list[str], *, expired_user: str | None = None) -> None:
        for user in usernames:
            self.get_token(user, "valid")
        if expired_user is not None:
            self.get_token(expired_user, "expired")

    def all_issued_tokens(self) -> list[str]:
        return list(self._issued)


class StaticTokenProvider:
    """In-memory token provider for tests (never invokes subprocess)."""

    def __init__(
        self,
        valid_tokens: dict[str, str],
        expired_tokens: dict[str, str] | None = None,
    ) -> None:
        self._valid = dict(valid_tokens)
        self._expired = dict(expired_tokens or {})
        self._issued: list[str] = []

    def get_token(
        self,
        username: str,
        token_type: Literal["valid", "expired", "tampered", "none"] = "valid",
    ) -> str | None:
        if token_type == "none":
            return None
        if token_type == "valid":
            tok = self._valid[username]
        elif token_type == "expired":
            tok = self._expired.get(username) or self._valid[username] + ".exp"
        elif token_type == "tampered":
            tok = tamper_jwt(self._valid[username])
        else:
            raise TokenError(f"不支持的 token 类型: {token_type}")
        if tok not in self._issued:
            self._issued.append(tok)
        return tok

    def prime_accounts(self, usernames: list[str], *, expired_user: str | None = None) -> None:
        for user in usernames:
            self.get_token(user, "valid")
        if expired_user is not None:
            self.get_token(expired_user, "expired")

    def all_issued_tokens(self) -> list[str]:
        return list(self._issued)
