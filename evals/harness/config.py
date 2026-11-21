"""Configuration for evals harness via EVALS_* environment variables (5.11.2)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EVALS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVALS_DIR.parent


class Settings(BaseSettings):
    """Runtime settings for `evals/harness` loaded from env and `evals/.env`."""

    model_config = SettingsConfigDict(
        env_prefix="EVALS_",
        env_file=str(EVALS_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    chat_url: str = Field(default="http://localhost:8030")
    mcp_url: str = Field(default="http://localhost:8080/aftersale/mcp")
    guard_url: str = Field(default="http://localhost:8020")

    mint_cmd: str = Field(default="docker compose exec -T dev-idp python -m dev_idp.mint")
    reset_guard_cmd: str = Field(default="docker compose exec -T guard python -m app.reset --yes")
    reset_mockapi_cmd: str = Field(default="docker compose restart mock-api")

    ready_timeout_seconds: float = Field(default=60.0, gt=0)
    http_timeout_seconds: float = Field(default=15.0, gt=0)
    chat_timeout_seconds: float = Field(default=150.0, gt=0)
    turn_interval_seconds: float = Field(default=0.0, ge=0)

    judge_base_url: str = Field(default="")
    judge_api_key: str = Field(default="")
    judge_model: str = Field(default="")

    report_dir: str = Field(default="reports/runs")

    @property
    def judge_configured(self) -> bool:
        return bool(
            self.judge_base_url.strip() and self.judge_api_key.strip() and self.judge_model.strip()
        )

    def resolve_report_dir(self, base_dir: Path = EVALS_DIR) -> Path:
        path = Path(self.report_dir)
        if path.is_absolute():
            return path
        return (base_dir / path).resolve()
