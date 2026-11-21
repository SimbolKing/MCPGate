# workflows：Dify Chatflow、部署封装与聊天入口

本目录包含第 4 章的三个核心部分：

- **Chatflow DSL 与代码节点**：`aftersale.dsl.template.yml`、`code/`、`scripts/`、`tests/`
- **Dify 1.20.0 部署封装**：`dify/`
- **聊天入口 `chat`**：`chat/` 与 `compose.yaml`

## 从零搭建步骤

1. **启动 `mcpgate` 基础服务并应用网关配置**（仓库根目录）：
   ```bash
   docker compose up -d dev-idp mock-api guard higress
   gateway/scripts/apply.sh
   ```
2. **获取并启动 Dify 1.20.0**（详见 `dify/README.md`）：
   ```bash
   cd workflows/dify
   ./fetch.sh
   cp dify.env.example docker/.env
   # 用 openssl rand -hex 32 填充 docker/.env 的 DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN
   ./dify.sh up -d
   ```
   打开 `http://localhost:8088/install` 创建管理员账号、安装并配置推理模型与 Embedding 模型插件、创建知识库 API Key。
3. **同步并验证知识库**（`knowledge-base/`）：
   ```bash
   cd knowledge-base
   cp .env.example .env   # 填入 KB_DIFY_API_KEY 与模型配置
   uv run python scripts/sync.py
   uv run python scripts/probe.py
   ```
4. **渲染、导入并发布 Chatflow DSL**：
   ```bash
   cd workflows
   cp .env.example .env   # 填入 WORKFLOWS_LLM_PROVIDER / WORKFLOWS_LLM_MODEL 等
   uv run python scripts/render_dsl.py
   ```
   产物写出至 `workflows/build/aftersale.dsl.yml`（同时自动运行 `check_dsl.py` D1–D12）。
   在 Dify 控制台进入 **工作室 -> 导入 DSL 文件**，导入 `build/aftersale.dsl.yml` 后点击 **发布**，再在 **访问 API -> API 密钥** 创建密钥并写入 `workflows/.env` 的 `CHAT_DIFY_API_KEY`。
5. **启动聊天入口 `chat`**（仓库根目录）：
   ```bash
   docker compose up -d --build chat
   ```
   打开 `http://localhost:8030` 登录使用。
6. **端到端验证**：
   ```bash
   cd workflows
   uv run python scripts/e2e.py
   ```
   运行结束后按第 3 章 3.11.4 重置 `mock-api` 与 `guard` 的数据。

## 画布修改后的回写流程

代码节点只在 `workflows/code/` 修改；提示词、节点配置等在 Dify 画布修改后：

1. 在控制台导出 YAML。
2. 归一化回模板并执行静态检查：
   ```bash
   cd workflows
   uv run python scripts/normalize_dsl.py /path/to/exported.yml
   ```
3. 检查差异并确认 `uv run pytest` 通过。

## 离线单元测试与静态检查

```bash
cd workflows
uv run python scripts/check_dsl.py
uv run pytest --cov=code --cov-branch --cov-report=term-missing
uv run ruff check . && uv run ruff format --check .
```
