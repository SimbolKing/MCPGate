#!/usr/bin/env bash
set -euo pipefail

DIFY_REF="1.20.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${SCRIPT_DIR}/docker"
DIFY_SH="${DIFY_SCRIPT:-${SCRIPT_DIR}/dify.sh}"

FORCE=false
CLEAN=false
for arg in "$@"; do
  case "$arg" in
    --force)
      FORCE=true
      ;;
    --clean)
      CLEAN=true
      ;;
    -h|--help)
      echo "用法: $0 [--force [--clean]]"
      echo "取回 Dify ${DIFY_REF} 官方 docker/ 部署目录至 workflows/dify/docker/。"
      echo "  --force  已存在 docker/ 时先检查容器已停止、再下载校验，备份旧目录为"
      echo "           docker.bak.<时间戳> 后替换，默认自动从备份迁回 volumes/ 与 .env。"
      echo "  --clean  与 --force 配合使用，不迁回旧 volumes/ 与 .env（仍保留在备份目录中）。"
      echo "可选环境变量: DIFY_SOURCE=<本地 Dify git 仓库路径或 .tar.gz 归档>（离线取回，不访问网络）。"
      exit 0
      ;;
    *)
      echo "错误: 未知参数 $arg" >&2
      exit 1
      ;;
  esac
done

if [[ "${CLEAN}" == "true" && "${FORCE}" != "true" ]]; then
  echo "错误: --clean 必须与 --force 一起使用。" >&2
  exit 1
fi

if [[ -d "${TARGET_DIR}" && "${FORCE}" != "true" ]]; then
  echo "错误: ${TARGET_DIR} 已存在（可能包含运行态数据卷 volumes/）。如需替换请显式传入 --force（默认保留并迁回 volumes/ 与 .env）。" >&2
  exit 1
fi

if [[ -d "${TARGET_DIR}" && "${FORCE}" == "true" ]]; then
  if [[ -x "${DIFY_SH}" && -f "${TARGET_DIR}/docker-compose.yaml" ]]; then
    CONTAINER_STATES=""
    if CONTAINER_STATES="$("${DIFY_SH}" ps -a --format '{{.State}}' 2>/dev/null)"; then
      ACTIVE_STATES=""
      while IFS= read -r state; do
        state="${state//[[:space:]]/}"
        [[ -z "${state}" ]] && continue
        state_lower="$(printf '%s' "${state}" | tr '[:upper:]' '[:lower:]')"
        case "${state_lower}" in
          exited|created|dead)
            ;;
          *)
            if [[ -z "${ACTIVE_STATES}" ]]; then
              ACTIVE_STATES="${state_lower}"
            else
              ACTIVE_STATES="${ACTIVE_STATES}, ${state_lower}"
            fi
            ;;
        esac
      done <<< "${CONTAINER_STATES}"
      if [[ -n "${ACTIVE_STATES}" ]]; then
        echo "错误: 检测到存在未停止状态的 Dify 容器（状态: ${ACTIVE_STATES}），请先执行 ./dify.sh stop 停止全部容器后再运行 ./fetch.sh --force。" >&2
        exit 1
      fi
    fi
  fi
fi

if [[ -n "${DIFY_SOURCE:-}" && ! -d "${DIFY_SOURCE}" && ! -f "${DIFY_SOURCE}" ]]; then
  echo "错误: DIFY_SOURCE=${DIFY_SOURCE} 不存在。" >&2
  exit 1
fi

TMP_WORK="$(mktemp -d "${SCRIPT_DIR}/.fetch.tmp.XXXXXX")"
BACKUP_DIR=""
SWAPPING=false
MIGRATING=false

rollback_backup() {
  if [[ "${SWAPPING}" == "true" && -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" && ! -e "${TARGET_DIR}" ]]; then
    if mv "${BACKUP_DIR}" "${TARGET_DIR}"; then
      echo "已回滚: 已将备份目录 ${BACKUP_DIR} 恢复为 ${TARGET_DIR}" >&2
      SWAPPING=false
      MIGRATING=false
    else
      echo "错误: 回滚失败，备份仍保留在 ${BACKUP_DIR}，请手工恢复为 ${TARGET_DIR}。" >&2
      SWAPPING=false
    fi
  fi
}

print_migration_recovery() {
  local has_backup_volumes=false
  local has_target_volumes=false
  local has_backup_env=false
  local env_migrated=false
  local need_volumes=false
  local need_env=false
  local step_no=1

  [[ -d "${BACKUP_DIR}/volumes" ]] && has_backup_volumes=true
  [[ -d "${TARGET_DIR}/volumes" ]] && has_target_volumes=true
  [[ -f "${BACKUP_DIR}/.env" ]] && has_backup_env=true

  if [[ "${has_backup_volumes}" == "true" ]]; then
    need_volumes=true
  fi
  if [[ "${has_backup_env}" == "true" ]]; then
    if [[ -f "${TARGET_DIR}/.env" ]] && cmp -s "${BACKUP_DIR}/.env" "${TARGET_DIR}/.env" 2>/dev/null; then
      env_migrated=true
    else
      need_env=true
    fi
  fi

  echo "错误: 迁回旧运行态数据（volumes/ 或 .env）失败或被中断。" >&2
  echo "警告: 迁回完成前不要启动 Dify，否则将以空数据卷初始化。" >&2
  echo "请按以下实际状态从备份目录手动恢复：" >&2
  echo "  备份目录: ${BACKUP_DIR}" >&2
  echo "  目标目录: ${TARGET_DIR}" >&2

  if [[ "${has_backup_volumes}" == "false" && "${has_target_volumes}" == "true" ]]; then
    echo "  状态提示: volumes/ 已迁回，不要删除（当前位于 ${TARGET_DIR}/volumes）。" >&2
  fi
  if [[ "${env_migrated}" == "true" ]]; then
    echo "  状态提示: .env 已迁回（${TARGET_DIR}/.env 与备份一致）。" >&2
  fi

  if [[ "${need_volumes}" == "true" ]]; then
    echo "  ${step_no}. 迁回仍留在备份目录中的 volumes/：" >&2
    if [[ "${has_target_volumes}" == "true" ]]; then
      echo "     rm -rf \"${TARGET_DIR}/volumes\" && mv \"${BACKUP_DIR}/volumes\" \"${TARGET_DIR}/volumes\"" >&2
    else
      echo "     mv \"${BACKUP_DIR}/volumes\" \"${TARGET_DIR}/volumes\"" >&2
    fi
    step_no=$((step_no + 1))
  fi

  if [[ "${need_env}" == "true" ]]; then
    echo "  ${step_no}. 迁回仍需恢复的 .env：" >&2
    echo "     cp -p \"${BACKUP_DIR}/.env\" \"${TARGET_DIR}/.env\"" >&2
    step_no=$((step_no + 1))
  fi

  if [[ "${need_volumes}" == "false" && "${need_env}" == "false" ]]; then
    echo "  未发现剩余待迁回项，请核对 ${TARGET_DIR} 与 ${BACKUP_DIR} 内容后再执行 ./dify.sh up -d。" >&2
  else
    echo "  确认上述待迁回项均完成后再执行 ./dify.sh up -d。" >&2
  fi
}

cleanup() {
  if [[ -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" ]]; then
    if [[ ! -e "${TARGET_DIR}" ]]; then
      rollback_backup
    elif [[ "${MIGRATING}" == "true" ]]; then
      print_migration_recovery
    fi
  fi
  if [[ -n "${TMP_WORK:-}" && -d "${TMP_WORK}" ]]; then
    rm -rf "${TMP_WORK}"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STAGED_DIR="${TMP_WORK}/docker"

if [[ -n "${DIFY_SOURCE:-}" ]]; then
  if [[ -f "${DIFY_SOURCE}" ]]; then
    echo "正在从本地归档 ${DIFY_SOURCE} 提取 dify-${DIFY_REF}/docker/ 至临时目录 ..."
    tar -xzf "${DIFY_SOURCE}" -C "${TMP_WORK}" --strip-components=1 "dify-${DIFY_REF}/docker"
  else
    echo "正在从本地仓库 ${DIFY_SOURCE}（标签 ${DIFY_REF}）提取 docker/ 至临时目录 ..."
    git -C "${DIFY_SOURCE}" archive "${DIFY_REF}" docker | tar -x -C "${TMP_WORK}"
  fi
else
  ARCHIVE_URL="https://github.com/langgenius/dify/archive/refs/tags/${DIFY_REF}.tar.gz"
  TMP_ARCHIVE="${TMP_WORK}/dify-${DIFY_REF}.tar.gz"
  echo "正在下载 ${ARCHIVE_URL} ..."
  curl -fsSL "${ARCHIVE_URL}" -o "${TMP_ARCHIVE}"
  echo "正在解压 dify-${DIFY_REF}/docker/ 至临时目录 ..."
  tar -xzf "${TMP_ARCHIVE}" -C "${TMP_WORK}" --strip-components=1 "dify-${DIFY_REF}/docker"
fi

if [[ ! -f "${STAGED_DIR}/docker-compose.yaml" || ! -f "${STAGED_DIR}/.env.example" || ! -d "${STAGED_DIR}/nginx" || ! -d "${STAGED_DIR}/ssrf_proxy" ]]; then
  echo "错误: 临时目录缺少关键文件（docker-compose.yaml / .env.example / nginx/ / ssrf_proxy/），未改动现有目录。" >&2
  exit 1
fi

if [[ -d "${TARGET_DIR}" ]]; then
  TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
  BACKUP_DIR="${SCRIPT_DIR}/docker.bak.${TIMESTAMP}"
  SUFFIX=1
  while [[ -e "${BACKUP_DIR}" ]]; do
    BACKUP_DIR="${SCRIPT_DIR}/docker.bak.${TIMESTAMP}.${SUFFIX}"
    SUFFIX=$((SUFFIX + 1))
  done
  SWAPPING=true
  if [[ "${CLEAN}" != "true" ]]; then
    MIGRATING=true
  fi
  mv "${TARGET_DIR}" "${BACKUP_DIR}"
  echo "已将旧目录备份至: ${BACKUP_DIR}"
fi

if ! mv "${STAGED_DIR}" "${TARGET_DIR}"; then
  echo "错误: 移入新目录至 ${TARGET_DIR} 失败，正在回滚备份 ..." >&2
  rollback_backup
  exit 1
fi
SWAPPING=false

if [[ -n "${BACKUP_DIR}" ]]; then
  if [[ "${CLEAN}" == "true" ]]; then
    echo "提示: 已指定 --clean，未迁回旧 volumes/ 与 .env（均保留在备份目录 ${BACKUP_DIR} 中）。"
  else
    if [[ -d "${BACKUP_DIR}/volumes" ]]; then
      rm -rf "${TARGET_DIR}/volumes"
      mv "${BACKUP_DIR}/volumes" "${TARGET_DIR}/volumes"
      echo "已从备份迁回 volumes/ 至 ${TARGET_DIR}/volumes"
    fi
    if [[ -f "${BACKUP_DIR}/.env" ]]; then
      cp -p "${BACKUP_DIR}/.env" "${TARGET_DIR}/.env"
      echo "已从备份迁回 .env 至 ${TARGET_DIR}/.env"
    fi
    MIGRATING=false
  fi
fi

echo "完成: 已获取 Dify ${DIFY_REF} 的 docker/ 目录至 ${TARGET_DIR}"
if [[ -f "${TARGET_DIR}/.env" ]]; then
  echo "已保留现有 ${TARGET_DIR}/.env，可直接执行 ./dify.sh up -d。"
else
  echo "下一步:"
  echo "  cp dify.env.example docker/.env"
  echo "  # 生成随机令牌并填入 docker/.env 中的 DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN:"
  echo "  openssl rand -hex 32"
fi
