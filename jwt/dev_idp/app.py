from __future__ import annotations

import hmac
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dev_idp.tokens import DevIdpSettings, find_user, load_jwks, mint_token


class TokenRequest(BaseModel):
    username: str = Field(..., min_length=1, description="登录名")
    password: str = Field(..., min_length=1, description="密码")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT 访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(..., description="有效期（秒）")


def create_app(settings: DevIdpSettings | None = None) -> FastAPI:
    cfg = settings or DevIdpSettings()
    api = FastAPI(title="MCPGate Dev IdP", version="1.0.0")

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/.well-known/jwks.json")
    def jwks() -> dict[str, Any]:
        return load_jwks(cfg.jwks_file)

    @api.post("/token", response_model=TokenResponse)
    def issue_token(body: TokenRequest, request: Request) -> Any:
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        user = find_user(body.username, cfg.users_file)
        if user is None or not hmac.compare_digest(
            str(user.get("password", "")).encode("utf-8"),
            body.password.encode("utf-8"),
        ):
            return JSONResponse(
                status_code=401,
                content={
                    "code": "invalid_credentials",
                    "message": "用户名或密码错误。",
                    "request_id": request_id,
                },
                headers={"X-Request-Id": request_id},
            )
        token, expires_in = mint_token(user, cfg)
        return JSONResponse(
            status_code=200,
            content={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": expires_in,
            },
            headers={"X-Request-Id": request_id},
        )

    return api


app = create_app()
