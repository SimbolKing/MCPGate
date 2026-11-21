# workflows/chat — MCPGate 售后助手聊天入口服务

`chat` 是 MCPGate 的薄前端聊天入口服务，只负责四件事：登录、独立 JWT 验签、把消息连同客服 JWT 转交 Dify Chatflow、将流式/非流式回答及脱敏后的调用轨迹与制度出处返回给浏览器。

- **独立验签**：除 `/`、`/static/*`、`/healthz`、`/chat/api/config`、`/chat/api/login` 外，所有接口均校验 `Authorization: Bearer <JWT>`（RS256，固定算法白名单，60 秒 leeway）。
- **凭据不回传**：向 Dify 发送 `inputs.user_token = <JWT>` 与 `user = <sub>`；转发给浏览器的 SSE 事件与历史记录中严禁包含节点 `inputs`、`outputs`（`call_summary` 白名单字段除外）、`process_data` 及应用 API Key。
- **双模式支持**：`POST /chat/api/messages` 支持 `stream: true`（SSE：`delta` / `trace` / `done` / `error`）与 `stream: false`（聚合 JSON，供脚本与评测使用）。

## 本地运行

```bash
cd workflows/chat
uv sync
cp .env.example .env
# 在 .env 中填入 Dify 售后助手应用的 CHAT_DIFY_API_KEY
uv run uvicorn app.main:create_app --factory --port 8030 --reload
```

聊天页：`http://localhost:8030/`

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 聊天页（静态文件） |
| `GET` | `/chat/api/config` | 前端配置（是否开启开发登录、审批控制台链接） |
| `POST` | `/chat/api/login` | 开发登录转发至 `CHAT_DEV_LOGIN_URL` |
| `POST` | `/chat/api/messages` | 发送消息（支持 SSE 流式与 `stream: false` 聚合返回）；`conversation_id` 须为 UUID，若 Dify 返回会话不存在（404）自动去掉 `conversation_id` 重开新会话一次 |
| `GET` | `/chat/api/conversations/{conversation_id}/messages` | 查询本人指定会话历史（已移除 `inputs` 等敏感字段）；`conversation_id` 须为 UUID，会话不存在或不属于当前登录人时返回 `404 conversation_not_found` |
| `GET` | `/healthz` | 健康检查 |

### 主要错误码（G8 三键结构）

| HTTP | `code` | 说明 |
|---|---|---|
| 401 | `missing_token` / `invalid_token` / `token_expired` / `login_failed` | 令牌缺失、无效、过期，或开发登录凭据错误 |
| 403 | `invalid_claims` | 令牌中的角色或区域声明无效 |
| 404 | `conversation_not_found` / `login_not_configured` / `route_not_found` | 会话不存在或不属于当前登录人、未配置开发登录、未知路由 |
| 405 | `method_not_allowed` | 当前接口不支持该请求方法（保留 `Allow` 响应头） |
| 422 | `validation_error` | 参数校验失败（含非 UUID 的 `conversation_id`） |
| 502 | `dify_error` | Dify 上游错误或超时（原始错误脱敏截断后仅写日志） |
| 503 | `dify_not_configured` / `auth_unavailable` | 未配置 `CHAT_DIFY_API_KEY` 或 JWKS / 登录服务不可用 |

## 测试与检查

```bash
cd workflows/chat
uv run pytest --cov=app --cov-branch --cov-report=term-missing -v
uv run ruff check .
uv run ruff format --check .
uv lock --check
```
