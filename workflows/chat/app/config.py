from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dify_base_url: str = "http://dify.internal"
    dify_api_key: str = ""
    dify_timeout_seconds: float = 120.0
    jwt_issuer: str = "http://dev-idp:8010"
    jwt_audience: str = "mcpgate"
    jwt_jwks_url: str = "http://dev-idp:8010/.well-known/jwks.json"
    jwt_algorithms: str = "RS256"
    jwt_leeway_seconds: int = 60
    jwks_cache_seconds: int = 300
    jwks_refresh_cooldown_seconds: float = Field(default=30.0, ge=0, allow_inf_nan=False)
    jwks_timeout_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    jwt_role_claim: str = "role"
    jwt_regions_claim: str = "regions"
    dev_login_url: str = ""
    approval_console_url: str = "http://localhost:8020/console/"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    @property
    def jwt_algorithm_list(self) -> list[str]:
        return [alg.strip() for alg in self.jwt_algorithms.split(",") if alg.strip()]
