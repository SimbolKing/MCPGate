#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [[ ! -f "docker/docker-compose.yaml" ]]; then
  echo "错误: 尚未获取 Dify 官方部署文件，请先执行 ./fetch.sh" >&2
  exit 1
fi

exec docker compose -p dify --project-directory docker \
  -f docker/docker-compose.yaml \
  -f compose.override.yaml \
  "$@"
