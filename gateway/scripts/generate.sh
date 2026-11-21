#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd -- "$GATEWAY_DIR/.." && pwd)"

CHECK_MODE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK_MODE=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
用法: gateway/scripts/generate.sh [--check]

  默认      从 mock-api/openapi.json 与模板重新生成 gateway/generated/
  --check   生成到临时目录并与 gateway/generated/ 逐文件比对，存在漂移时返回非 0
EOF
      exit 0
      ;;
    *)
      echo "错误: 未知参数 $1" >&2
      exit 2
      ;;
  esac
done

# 解析简单 KEY=VALUE（不执行 shell 代码）
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

PARAMS_FILE="$GATEWAY_DIR/params.env"
if [[ ! -f "$PARAMS_FILE" ]]; then
  echo "错误: 缺少参数文件 $PARAMS_FILE" >&2
  exit 1
fi

MCP_SERVER_NAME="$(read_env_value "$PARAMS_FILE" MCP_SERVER_NAME || true)"
MCP_SERVER_PLUGIN_VERSION="$(read_env_value "$PARAMS_FILE" MCP_SERVER_PLUGIN_VERSION || true)"
JWT_AUTH_PLUGIN_VERSION="$(read_env_value "$PARAMS_FILE" JWT_AUTH_PLUGIN_VERSION || true)"
OPENAPI_TO_MCP_REF="$(read_env_value "$PARAMS_FILE" OPENAPI_TO_MCP_REF || true)"
GO_IMAGE="$(read_env_value "$PARAMS_FILE" GO_IMAGE || true)"
JWKS_FILE="$(read_env_value "$PARAMS_FILE" JWKS_FILE || true)"

for var_name in MCP_SERVER_NAME MCP_SERVER_PLUGIN_VERSION JWT_AUTH_PLUGIN_VERSION OPENAPI_TO_MCP_REF GO_IMAGE JWKS_FILE; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "错误: $PARAMS_FILE 缺少参数 $var_name" >&2
    exit 1
  fi
done

if [[ "$JWKS_FILE" == /* ]]; then
  RESOLVED_JWKS_FILE="$JWKS_FILE"
else
  RESOLVED_JWKS_FILE="$REPO_ROOT/$JWKS_FILE"
fi

# 共享 JWT 变量：进程环境 > 仓库根 .env > G3 默认值
ROOT_ENV_FILE="$REPO_ROOT/.env"
if [[ -z "${MCPGATE_JWT_ISSUER:-}" ]]; then
  MCPGATE_JWT_ISSUER="$(read_env_value "$ROOT_ENV_FILE" MCPGATE_JWT_ISSUER || true)"
fi
MCPGATE_JWT_ISSUER="${MCPGATE_JWT_ISSUER:-http://dev-idp:8010}"

if [[ -z "${MCPGATE_JWT_AUDIENCE:-}" ]]; then
  MCPGATE_JWT_AUDIENCE="$(read_env_value "$ROOT_ENV_FILE" MCPGATE_JWT_AUDIENCE || true)"
fi
MCPGATE_JWT_AUDIENCE="${MCPGATE_JWT_AUDIENCE:-mcpgate}"

export MCP_SERVER_PLUGIN_VERSION JWT_AUTH_PLUGIN_VERSION MCPGATE_JWT_ISSUER MCPGATE_JWT_AUDIENCE

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# 始终先生成到临时目录：任一步或契约检查失败时不污染 gateway/generated/
RAW_OUT="$TMP_DIR/aftersale.mcp.yaml"

cd "$REPO_ROOT"

docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOPROXY \
  -v "$REPO_ROOT":/work \
  -v "$TMP_DIR":/out \
  -w /work "$GO_IMAGE" \
  go run "github.com/higress-group/openapi-to-mcpserver/cmd/openapi-to-mcp@$OPENAPI_TO_MCP_REF" \
    --input mock-api/openapi.json \
    --output /out/aftersale.mcp.yaml \
    --server-name "$MCP_SERVER_NAME" \
    --template gateway/templates/mcp-template.yaml

(
  cd "$GATEWAY_DIR"
  uv run python scripts/render.py \
    --raw-mcp "$RAW_OUT" \
    --output-dir "$TMP_DIR" \
    --jwks "$RESOLVED_JWKS_FILE" \
    --mcp-server-plugin-version "$MCP_SERVER_PLUGIN_VERSION" \
    --jwt-auth-plugin-version "$JWT_AUTH_PLUGIN_VERSION" \
    --jwt-issuer "$MCPGATE_JWT_ISSUER" \
    --jwt-audience "$MCPGATE_JWT_AUDIENCE"
)

TARGET_DIR="$GATEWAY_DIR/generated"

if [[ "$CHECK_MODE" -eq 1 ]]; then
  if [[ ! -d "$TARGET_DIR" ]]; then
    echo "漂移检查失败: 目录不存在 $TARGET_DIR" >&2
    exit 1
  fi
  if ! diff -ru "$TARGET_DIR" "$TMP_DIR"; then
    echo "漂移检查失败: gateway/generated/ 与当前输入不一致，请运行 gateway/scripts/generate.sh 更新产物。" >&2
    exit 1
  fi
  echo "漂移检查通过: gateway/generated/ 与当前输入完全一致。"
  exit 0
fi

mkdir -p "$TARGET_DIR/wasmplugins"
cp "$TMP_DIR/aftersale.mcp.yaml" "$TARGET_DIR/aftersale.mcp.yaml"
cp "$TMP_DIR/tools.manifest.json" "$TARGET_DIR/tools.manifest.json"
cp "$TMP_DIR/wasmplugins/mcpgate-mcp-server.yaml" "$TARGET_DIR/wasmplugins/mcpgate-mcp-server.yaml"
cp "$TMP_DIR/wasmplugins/mcpgate-jwt-auth.yaml" "$TARGET_DIR/wasmplugins/mcpgate-jwt-auth.yaml"

echo "已更新 gateway/generated/ 全部产物。"
