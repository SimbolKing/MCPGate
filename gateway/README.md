# gateway（Higress 转 MCP 与 JWT 透传）

本目录管理 MCPGate 网关层的 Higress 配置与脚本（第 2 章），将 `mock-api` 的 REST 接口发布为无状态 Streamable HTTP MCP Server `aftersale`，在入口执行 RS256 JWT 验签并将 `Authorization: Bearer <JWT>` 原样透传给路由后端 `guard`。

## 目录说明

- `compose.yaml`：`higress`（第 3 阶段追加 `guard`）服务与命名卷定义
- `.env.example`：`HIGRESS_*` 与 Compose 层可覆盖的 `GUARD_*` 变量模板
- `params.env`：生成参数（MCP Server 名、内置插件版本、转换器固定提交、JWKS 路径）
- `templates/`：转换器凭证透传模板、审批语义与网关自有工具定义、两个 WasmPlugin 骨架
- `resources/`：手写的 Higress 资源（`mcpbridges/default.yaml`、`ingresses/aftersale-mcp.yaml`）
- `generated/`：生成产物（不手改），由 `scripts/generate.sh` 生成并由 `--check` 保证无漂移
- `scripts/`：`generate.sh`、`render.py`、`apply.sh`、`smoke.py`

## 安装与固定镜像 Digest

1. 复制环境变量模板：

```bash
cp gateway/.env.example gateway/.env
```

2. 拉取镜像并固定为 digest（避免 `latest` 漂移；拉取超时可换用 `higress-registry.us-west-1.cr.aliyuncs.com` 或 `higress-registry.ap-southeast-7.cr.aliyuncs.com`）：

```bash
IMG=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/all-in-one:latest
docker pull "$IMG"
docker image inspect --format '{{index .RepoDigests 0}}' "$IMG"
```

将输出的 `higress-registry.../higress/all-in-one@sha256:...` 写入 `gateway/.env` 的 `HIGRESS_IMAGE`。

## 生成、应用与验证

以下命令均在**仓库根目录**执行：

```bash
# 1. 从 mock-api/openapi.json 与模板重新生成 gateway/generated/
gateway/scripts/generate.sh

# 2. 漂移检查（CI 使用，不一致返回非 0）
gateway/scripts/generate.sh --check

# 3. 启动依赖服务与 Higress，并应用四个配置资源（可选 --timeout <秒> 与 --endpoint <URL>）
docker compose up -d dev-idp mock-api guard higress
gateway/scripts/apply.sh
# 示例：自定义等待超时或探测端点
# gateway/scripts/apply.sh --timeout 90 --endpoint http://127.0.0.1:8080/aftersale/mcp

# 4. 端到端验证（guard 尚未就绪时可先运行 --only V1-V7）
python3 gateway/scripts/smoke.py --only V1-V7
python3 gateway/scripts/smoke.py
```

### 常用维护命令

```bash
# 查看容器内生效的 WasmPlugin 配置
docker compose exec higress cat /data/wasmplugins/mcpgate-mcp-server.yaml

# 改了 gateway/.env 后重建容器（卷中配置保留）
docker compose up -d higress

# 彻底重置 Higress 运行态
docker compose rm -sf higress && docker volume rm mcpgate_higress-data

# 本地单元测试与静态检查
cd gateway && uv run pytest && uv run ruff check . && uv run ruff format --check .
```

## 故障排查

容器内各组件日志位于 `/var/log/higress/`：

- `gateway.log`：Envoy 访问日志与 Wasm 插件日志（`mcp-server` 带 `[mcp-server] [<服务名>/<工具名>] [<request_id>]` 前缀）
- `controller.log`：Ingress / McpBridge / WasmPlugin 解析与转换错误
- `pilot.log`：xDS 配置下发日志
- `apiserver.log`：`/data` 文件存储与监听日志
- `plugin-server.log`：内置 Wasm 插件分发日志

```bash
docker compose exec higress tail -f /var/log/higress/gateway.log
docker compose exec higress ls -la /data/wasmplugins /data/mcpbridges /data/ingresses
```

本地排障需要查看 Wasm 调试日志时，可在 `gateway/.env` 临时设置 `HIGRESS_COMPONENT_LOG_LEVEL=wasm:debug` 后重建容器，排查完毕改回 `misc:error`。完整故障表见 `PROPOSAL.md` 2.14.2。
