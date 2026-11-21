# mock-api（售后订单系统）

模拟企业内部售后订单系统：提供带 RS256 JWT 鉴权与行级区域权限的 REST 接口，以及确定性降级的 OpenAPI 3.0.3 文档（`openapi.json`）。

## 业务接口

| operationId | 方法 | 路径 | 读/写 | 可调用角色 |
|---|---|---|---|---|
| `get_order` | GET | `/api/v1/orders/{order_id}` | 读 | `agent` / `supervisor` |
| `list_orders` | GET | `/api/v1/orders` | 读 | `agent` / `supervisor` |
| `get_refund` | GET | `/api/v1/refunds/{refund_id}` | 读 | `agent` / `supervisor` |
| `list_refunds` | GET | `/api/v1/refunds` | 读 | `supervisor` |
| `create_refund` | POST | `/api/v1/refunds` | **写** | `agent` / `supervisor` |

非业务端点（不鉴权，不进入 OpenAPI）：`GET /healthz`、`GET /openapi.json`、`GET /docs`。

## 本地运行

```bash
# 1. 启动签发器（另一终端）
cd ../jwt && uv sync && uv run uvicorn dev_idp.app:app --port 8010

# 2. 启动订单系统
uv sync
cp .env.example .env
uv run uvicorn app.main:create_app --factory --port 8000 --reload

# 3. 验证
TOKEN=$(cd ../jwt && uv run python -m dev_idp.mint --user wang)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/orders/A1023
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/orders/B2087
```

## 常用命令

```bash
# 测试与覆盖率
uv run pytest --cov=app --cov-branch --cov-report=term-missing

# 代码检查与格式化校验
uv run ruff check . && uv run ruff format --check .

# 导出 OpenAPI 3.0.3 文档
uv run python -m app.export_openapi --output openapi.json

# 重置种子数据（重置后新建首张退款单为 R000105）
uv run python -m app.seed
```
