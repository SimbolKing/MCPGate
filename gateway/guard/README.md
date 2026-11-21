# gateway/guard — MCPGate 审批与审计服务

`guard` 是 MCPGate 网关层的审批与审计服务，作为 Higress MCP 路由的后端：

- **独立验签**：所有接口（`/healthz`、静态页 `/console/`、开发登录 `/guard/v1/console/login` 除外）均校验 `Authorization: Bearer <JWT>`（RS256）。
- **白名单代理**：仅转发 `tools.manifest.json` 中 `owner == "upstream"` 的只读（`GET`）工具请求到 `mock-api`，请求头严格白名单透传。
- **写操作审批**：拦截写工具调用（`create_refund`），先用申请人 JWT 做预检读，通过后生成 `pending` 审批单并返回 HTTP 202；主管在审批页同意后，用主管本人当场 JWT 执行退款。
- **全量审计**：同步写入 `audit_events`（SQLite）并输出标准输出 JSON 日志；支持主管按角色与负责区域查询。

## 本地运行

```bash
cd gateway/guard
uv sync
cp .env.example .env
uv run uvicorn app.main:create_app --factory --port 8020 --reload
```

审批页：`http://localhost:8020/console/`
审计页：`http://localhost:8020/console/audit.html`

## 重置数据

```bash
# 容器内
docker compose exec guard python -m app.reset --yes

# 本地
cd gateway/guard && uv run python -m app.reset --yes
```

## 测试与检查

覆盖率分支开关（`branch = true`）与整体覆盖率门槛（`fail_under = 90`）已在 `pyproject.toml` 中固化。

```bash
cd gateway/guard
uv run pytest --cov=app --cov-branch
uv run ruff check .
uv run ruff format --check .
uv lock --check
```

## 手工验证清单（控制台）

审批页与审计页无前端构建链，发版或改动静态页后按以下清单手工核对：

1. 主管账号登录审批页（`/console/`），确认待处理列表正常展示、分页与按订单号查询可用；客服账号登录时提示仅主管可用。
2. 打开 `pending` 审批单详情，核对申请内容、提交时订单快照、实时订单现状及时间线；执行“同意并执行”与“拒绝”并确认状态与审计时间线更新。
3. 迟到执行结果出现后，保持打开 `execution_failed` 详情页等待轮询，确认出现刷新提示；点击刷新后可见迟到执行冲突提示与对应退款单号（或单号未知时的核对提示）。
4. 切换至审计日志页（`/console/audit.html`），按时间、审批单号、操作人等条件筛选并确认结果准确。
