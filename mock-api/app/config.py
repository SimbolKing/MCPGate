from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOCKAPI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:////data/mock.db"
    reset_on_startup: bool = True
    seed_file: str = "seed/seed.json"
    jwt_issuer: str = "http://dev-idp:8010"
    jwt_audience: str = "mcpgate"
    jwt_jwks_url: str = "http://dev-idp:8010/.well-known/jwks.json"
    jwt_algorithms: str = "RS256"
    jwt_leeway_seconds: int = 60
    jwks_cache_seconds: int = 300
    jwks_refresh_cooldown_seconds: float = Field(default=30.0, ge=0.0, allow_inf_nan=False)
    jwks_timeout_seconds: float = Field(default=5.0, gt=0.0, allow_inf_nan=False)
    jwt_role_claim: str = "role"
    jwt_regions_claim: str = "regions"
    public_base_url: str = "http://mock-api:8000"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    @property
    def jwt_algorithm_list(self) -> list[str]:
        return [alg.strip() for alg in self.jwt_algorithms.split(",") if alg.strip()]

    def resolve_seed_path(self) -> Path:
        path = Path(self.seed_file)
        if path.is_absolute() or path.exists():
            return path
        candidate = PROJECT_ROOT / path
        if candidate.exists():
            return candidate
        return path
