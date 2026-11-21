# dev-idp（开发用身份签发器）

> **安全警示**：本目录下的 `keys/private.pem` 与 `users.json` **仅用于本地开发与评测**，严禁用于任何真实生产或预发环境。接入真实企业时，应整体替换为企业统一登录（OIDC/IdP），下游服务仅需修改 issuer、audience 与 JWKS 配置。

## 预置账号（G2）

所有开发账号的默认密码均为 `dev-password`（定义于 `users.json`）：

| sub | username | 姓名 | role | regions |
|---|---|---|---|---|
| `u1001` | `wang` | 王磊 | `agent` | `east` |
| `u1002` | `zhao` | 赵宇 | `agent` | `north` |
| `u1003` | `chen` | 陈晨 | `agent` | `south` |
| `u2001` | `li` | 李娜 | `supervisor` | `east` |
| `u2002` | `zhou` | 周涛 | `supervisor` | `north,south` |
| `u2003` | `sun` | 孙浩 | `supervisor` | `east,north,south` |

## 接口

- `GET /healthz`：健康检查，返回 `{"status": "ok"}`
- `GET /.well-known/jwks.json`：返回 `keys/jwks.json` 公钥集
- `POST /token`：请求体 `{"username": "...", "password": "..."}`，成功返回 `{"access_token": "...", "token_type": "Bearer", "expires_in": 28800}`，失败返回 401

## 常用命令

```bash
# 安装依赖
uv sync

# 本地启动服务
uv run uvicorn dev_idp.app:app --port 8010

# 命令行签发 token（支持 --ttl 负数生成过期 token）
uv run python -m dev_idp.mint --user wang
uv run python -m dev_idp.mint --user wang --ttl -120

# 重新生成 RSA-2048 开发密钥对与 jwks.json
uv run python -m dev_idp.genkeys

# 测试与代码检查
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
