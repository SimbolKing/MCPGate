#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd -- "$GATEWAY_DIR/.." && pwd)"

readonly DEFAULT_APPLY_TIMEOUT_SECONDS=60
readonly DEFAULT_MCP_ENDPOINT="http://127.0.0.1:8080/aftersale/mcp"

APPLY_TIMEOUT_SECONDS="$DEFAULT_APPLY_TIMEOUT_SECONDS"
MCP_ENDPOINT="$DEFAULT_MCP_ENDPOINT"

usage() {
  cat <<'EOF'
用法: gateway/scripts/apply.sh [--timeout <秒>] [--endpoint <URL>]

  --timeout <秒>     等待路由与 jwt-auth 生效的超时秒数（正整数，默认 60）
  --endpoint <URL>   探测的 MCP 端点地址（默认 http://127.0.0.1:8080/aftersale/mcp）
  -h, --help         显示本帮助信息
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "错误: --timeout 需要提供一个正整数秒数" >&2
        exit 2
      fi
      APPLY_TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --timeout=*)
      APPLY_TIMEOUT_SECONDS="${1#*=}"
      shift
      ;;
    --endpoint)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "错误: --endpoint 需要提供一个 URL" >&2
        exit 2
      fi
      MCP_ENDPOINT="$2"
      shift 2
      ;;
    --endpoint=*)
      MCP_ENDPOINT="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "错误: 未知参数 $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$APPLY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "错误: --timeout 必须是正整数，当前为: $APPLY_TIMEOUT_SECONDS" >&2
  exit 2
fi

if [[ -z "$MCP_ENDPOINT" ]]; then
  echo "错误: --endpoint 不能为空" >&2
  exit 2
fi

read_env_value() {
  local file="$1"
  local target_key="$2"
  [[ -f "$file" ]] || return 1
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    line="${line#export }"
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    if [[ "$key" == "$target_key" ]]; then
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      if [[ ( "$val" == \"*\" && "$val" == *\" ) || ( "$val" == \'*\' && "$val" == *\' ) ]]; then
        val="${val:1:${#val}-2}"
      fi
      printf '%s' "$val"
      return 0
    fi
  done < "$file"
  return 1
}

extract_metadata_name() {
  local file="$1"
  awk '
    /^metadata:[[:space:]]*$/ { in_meta = 1; next }
    in_meta && /^[^[:space:]]/ { in_meta = 0 }
    in_meta && /^[[:space:]]+name:[[:space:]]*/ {
      val = $0
      sub(/^[[:space:]]+name:[[:space:]]*/, "", val)
      sub(/[[:space:]]*#.*$/, "", val)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
      gsub(/^["'\'']|["'\'']$/, "", val)
      print val
      exit
    }
  ' "$file"
}

container_health() {
  local service="$1"
  local cid
  cid="$(docker compose ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    echo "missing"
    return 0
  fi
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo "unknown"
}

check_plugin_wasm() {
  local plugin_name="$1"
  local version="$2"
  local wasm_path="/usr/share/nginx/html/plugins/${plugin_name}/${version}/plugin.wasm"
  if ! docker compose exec -T higress test -f "$wasm_path"; then
    echo "错误: higress 容器内缺少插件文件: $wasm_path" >&2
    echo "容器内 ${plugin_name} 实际可用版本如下：" >&2
    docker compose exec -T higress ls -1 "/usr/share/nginx/html/plugins/${plugin_name}" >&2 || true
    echo "请修正 gateway/params.env 后重新执行 gateway/scripts/generate.sh。" >&2
    exit 1
  fi
}

wait_for_mcp_401() {
  local timeout="$1"
  local start_ts="$SECONDS"
  local deadline=$((start_ts + timeout))
  local code="000"
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    local remaining=$((deadline - SECONDS))
    local max_time=$((remaining < 5 ? remaining : 5))
    local connect_timeout=$((max_time < 3 ? max_time : 3))
    local curl_rc=0
    local raw_code=""
    raw_code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --connect-timeout "$connect_timeout" \
      --max-time "$max_time" \
      -X POST "$MCP_ENDPOINT" \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' 2>/dev/null)" || curl_rc=$?
    if [[ "$curl_rc" -ne 0 || -z "$raw_code" ]]; then
      code="000"
    else
      code="$raw_code"
    fi
    if [[ "$code" == "401" ]]; then
      return 0
    fi
    remaining=$((deadline - SECONDS))
    if [[ "$remaining" -le 0 ]]; then
      break
    fi
    local sleep_secs=$((remaining < 2 ? remaining : 2))
    sleep "$sleep_secs"
  done
  echo "最近一次探测 HTTP 状态码: $code" >&2
  return 1
}

wait_for_higress_healthy() {
  local timeout="$1"
  local start_ts="$SECONDS"
  local deadline=$((start_ts + timeout))
  local status="unknown"
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    status="$(container_health higress)"
    if [[ "$status" == "healthy" ]]; then
      return 0
    fi
    local remaining=$((deadline - SECONDS))
    if [[ "$remaining" -le 0 ]]; then
      break
    fi
    local sleep_secs=$((remaining < 2 ? remaining : 2))
    sleep "$sleep_secs"
  done
  echo "higress 重启后状态仍为: $status" >&2
  return 1
}

print_troubleshooting() {
  cat >&2 <<'EOF'
配置未在超时时间内生效，请按以下排查入口检查（详见 PROPOSAL.md 2.14）：
  1. 查看资源是否落盘：
     docker compose exec higress ls -la /data/wasmplugins /data/mcpbridges /data/ingresses
  2. 查看控制面与数据面日志：
     docker compose exec higress tail -n 100 /var/log/higress/controller.log
     docker compose exec higress tail -n 100 /var/log/higress/pilot.log
     docker compose exec higress tail -n 100 /var/log/higress/apiserver.log
     docker compose exec higress tail -n 100 /var/log/higress/gateway.log
     docker compose exec higress tail -n 100 /var/log/higress/plugin-server.log
EOF
}

cd "$REPO_ROOT"

PARAMS_FILE="$GATEWAY_DIR/params.env"
if [[ ! -f "$PARAMS_FILE" ]]; then
  echo "错误: 缺少参数文件 $PARAMS_FILE" >&2
  exit 1
fi

MCP_SERVER_PLUGIN_VERSION="$(read_env_value "$PARAMS_FILE" MCP_SERVER_PLUGIN_VERSION || true)"
JWT_AUTH_PLUGIN_VERSION="$(read_env_value "$PARAMS_FILE" JWT_AUTH_PLUGIN_VERSION || true)"

for var_name in MCP_SERVER_PLUGIN_VERSION JWT_AUTH_PLUGIN_VERSION; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "错误: $PARAMS_FILE 缺少参数 $var_name" >&2
    exit 1
  fi
done

# 1. 前置检查
echo "[1/7] 前置检查：higress 与 guard 容器状态、内置插件版本..."
higress_status="$(container_health higress)"
if [[ "$higress_status" != "healthy" ]]; then
  echo "错误: higress 容器未处于 healthy 状态（当前: ${higress_status}）。请先执行 docker compose up -d higress 并等待健康检查通过。" >&2
  exit 1
fi

guard_status="$(container_health guard)"
if [[ "$guard_status" != "healthy" ]]; then
  echo "警告: guard 容器未运行或不健康（当前: ${guard_status}），继续应用 Higress 配置；guard 就绪前工具调用将返回 503。" >&2
fi

check_plugin_wasm "mcp-server" "$MCP_SERVER_PLUGIN_VERSION"
check_plugin_wasm "jwt-auth" "$JWT_AUTH_PLUGIN_VERSION"

# 2. 文件检查（按送入顺序：两个 WasmPlugin -> McpBridge -> Ingress）
echo "[2/7] 检查本地资源文件与 metadata.name..."
SRC_FILES=(
  "gateway/generated/wasmplugins/mcpgate-jwt-auth.yaml"
  "gateway/generated/wasmplugins/mcpgate-mcp-server.yaml"
  "gateway/resources/mcpbridges/default.yaml"
  "gateway/resources/ingresses/aftersale-mcp.yaml"
)
DST_FILES=(
  "/data/wasmplugins/mcpgate-jwt-auth.yaml"
  "/data/wasmplugins/mcpgate-mcp-server.yaml"
  "/data/mcpbridges/default.yaml"
  "/data/ingresses/aftersale-mcp.yaml"
)

for src in "${SRC_FILES[@]}"; do
  if [[ ! -f "$src" ]]; then
    echo "错误: 缺少资源文件 $src（如为生成产物，请先运行 gateway/scripts/generate.sh）" >&2
    exit 1
  fi
  expected_name="$(basename "$src" .yaml)"
  actual_name="$(extract_metadata_name "$src")"
  if [[ "$actual_name" != "$expected_name" ]]; then
    echo "错误: $src 的 metadata.name (${actual_name:-<空>}) 与文件名 (${expected_name}) 不一致" >&2
    exit 1
  fi
done

# 3. 建目录
echo "[3/7] 确保容器内目标目录存在..."
docker compose exec -T higress mkdir -p /data/wasmplugins /data/ingresses /data/mcpbridges

# 4. 送入文件（顺序：两个 WasmPlugin -> McpBridge -> Ingress）
echo "[4/7] 送入配置文件到 higress:/data ..."
for i in "${!SRC_FILES[@]}"; do
  docker compose cp "${SRC_FILES[$i]}" "higress:${DST_FILES[$i]}"
done

# 5. 触发监听
echo "[5/7] 触发 apiserver 文件监听（touch）..."
for dst in "${DST_FILES[@]}"; do
  docker compose exec -T higress touch "$dst"
done

# 6. 等待生效
echo "[6/7] 等待路由与 jwt-auth 生效（轮询 $MCP_ENDPOINT 直到返回 401，最长 ${APPLY_TIMEOUT_SECONDS}s）..."
if wait_for_mcp_401 "$APPLY_TIMEOUT_SECONDS"; then
  echo "配置已生效：$MCP_ENDPOINT 已返回预期 401。"
  exit 0
fi

# 7. 兜底重启一次
echo "[7/7] 等待超时，尝试重启 higress 一次并重新等待..." >&2
docker compose restart higress
wait_for_higress_healthy "$APPLY_TIMEOUT_SECONDS" || true
if wait_for_mcp_401 "$APPLY_TIMEOUT_SECONDS"; then
  echo "重启后配置已生效：$MCP_ENDPOINT 已返回预期 401。"
  exit 0
fi

print_troubleshooting
exit 1
