from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GUARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:////data/guard.db"
    upstream_base_url: str = "http://mock-api:8000"
    upstream_timeout_seconds: float = 10.0
    tools_manifest: str = "/app/tools.manifest.json"
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
    approver_role: str = "supervisor"
    allow_self_approval: bool = False
    approval_ttl_hours: int = 72
    max_body_bytes: int = Field(default=65536, gt=0)
    audit_retention_days: int = 180
    audit_redact_fields: str = ""
    dev_login_url: str = ""
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    @property
    def jwt_algorithm_list(self) -> list[str]:
        return [alg.strip() for alg in self.jwt_algorithms.split(",") if alg.strip()]

    @property
    def audit_redact_field_set(self) -> frozenset[str]:
        return frozenset(
            field.strip() for field in self.audit_redact_fields.split(",") if field.strip()
        )

    def resolve_manifest_path(self) -> Path:
        path = Path(self.tools_manifest)
        if path.is_absolute() or path.exists():
            return path
        candidate = PROJECT_ROOT / path
        if candidate.exists():
            return candidate
        fallback = PROJECT_ROOT.parent / "generated/tools.manifest.json"
        if fallback.exists():
            return fallback
        return path
