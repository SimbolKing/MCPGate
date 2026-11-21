# Dify 1.20.0 本地部署封装

本目录以"不修改上游文件"的方式封装 Dify 1.20.0 官方 Docker Compose 部署：全部定制集中在 `dify.env.example`（复制到 `docker/.env`）与 `compose.override.yaml`。

## 前提条件

- Docker Compose **v2.35.0+**（需要支持 `env_file` 优先级与 `!override` 语法）。
- Docker Desktop 建议分配 **6 GB 以上内存**（Dify 11 个常驻服务空闲约 1.6–2.8 GB，加上 `mcpgate` 项目的 5 个服务）。
- 先在仓库根目录启动 `mcpgate` 项目，使外部网络 `mcpgate-net` 存在：

```bash
docker compose up -d dev-idp mock-api guard higress
gateway/scripts/apply.sh
```

## 获取与启动

```bash
cd workflows/dify
./fetch.sh
cp dify.env.example docker/.env
# 生成 64 位十六进制令牌并填入 docker/.env 的 DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN
openssl rand -hex 32

./dify.sh up -d
./dify.sh ps --all
```

> 离线或已有本地 Dify 检出/归档时，可设置 `DIFY_SOURCE=/path/to/dify`（git 仓库或 `.tar.gz` 归档）执行 `./fetch.sh` 从标签 `1.20.0` 提取 `docker/`。
>
> 如需重新获取并替换已有的 `docker/` 目录，**须先执行 `./dify.sh stop` 停止运行中的 Dify 容器**，再执行 `./fetch.sh --force`（若检测到仍有运行中的容器，脚本会拒绝执行并提示先停止）：脚本会先下载解压到临时目录并校验关键文件完整，再将旧 `docker/` 改名为带时间戳的 `docker.bak.<时间戳>` 备份目录后移入新目录（移入失败自动回滚恢复原 `docker/`），默认自动从备份迁回 `volumes/` 与 `.env`；仅当显式传入 `./fetch.sh --force --clean` 时才不迁回（旧数据仍保留在备份目录中）。

## 连通性自检

启动前先检查覆盖合并后的网络配置，确认 `nginx` 的 `networks` 下有 `default` 与 `mcpgate` 两个键（且 `mcpgate.aliases` 含 `dify.internal`），顶层 `networks.mcpgate.name` 为 `mcpgate-net`（确保 `nginx` 既能访问 Dify 内部 `api`/`web`，也能被 `mcpgate-net` 上的 `chat` 通过 `http://dify.internal` 访问）：

```bash
./dify.sh config | sed -n '/^  nginx:/,/^  [a-z]/p'
./dify.sh config | awk '/^networks:/{p=1;print;next} /^[^ ]/{p=0} p'
```

启动完成后可验证 `ssrf_proxy` 放行 `higress` 生效：

```bash
./dify.sh exec ssrf_proxy sh -c 'getent hosts higress'
./dify.sh exec api curl -s -o /dev/null -w '%{http_code}\n' \
  -x http://ssrf_proxy:3128 -X POST http://higress:8080/aftersale/mcp
```

预期返回 `401`（已到达 Higress 并因未携带 JWT 被拒）；若返回 `403` 说明 SSRF 放行未生效。

## 首次控制台配置

1. 打开 `http://localhost:8088/install` 创建管理员账号。
2. 进入 **设置 -> 模型供应商**：安装所需供应商插件并填写 API Key，在 **系统模型设置** 中指定默认推理模型与 Embedding 模型。
3. 进入 **知识库 -> API**：创建知识库 API Key，填入 `knowledge-base/.env` 的 `KB_DIFY_API_KEY`。

## 日常启停与省资源

- 整体暂停（数据保留在 `docker/volumes/`）：`./dify.sh stop`
- 恢复运行：`./dify.sh start`
- 仅对话/评测、无需打开控制台时可停掉前端：`./dify.sh stop web`（重启 `nginx` 前需先恢复 `web`）。
