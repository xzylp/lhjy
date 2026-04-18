#!/usr/bin/env bash
# ashare-system-v2 运行日志 / 临时文件清理
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

RETENTION_DAYS="${ASHARE_RUNTIME_RETENTION_DAYS:-7}"
if ! [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] || [[ "${RETENTION_DAYS}" -lt 1 ]]; then
  echo "[ashare-v2] ASHARE_RUNTIME_RETENTION_DAYS 非法: ${RETENTION_DAYS}" >&2
  exit 2
fi

TARGETS=(
  "${PROJECT_DIR}/logs"
  "${PROJECT_DIR}/web/node_modules/.tmp"
)

FILE_PATTERNS=(
  "*.log"
  "*.log.*"
  "*.out"
  "*.out.*"
  "*.err"
  "*.err.*"
  "*.tmp"
  "*.tmp.*"
  "*.temp"
  "*.temp.*"
  "*.json"
  "*.json.*"
  "*.md"
  "*.md.*"
  "*.txt"
  "*.txt.*"
)

deleted_count=0

delete_matching_files() {
  local root="$1"
  shift
  if [[ ! -d "${root}" ]]; then
    return 0
  fi

  local -a expr=()
  local first=1
  for pattern in "$@"; do
    if [[ ${first} -eq 0 ]]; then
      expr+=( -o )
    fi
    expr+=( -name "${pattern}" )
    first=0
  done

  while IFS= read -r path; do
    [[ -z "${path}" ]] && continue
    rm -f -- "${path}"
    deleted_count=$((deleted_count + 1))
    printf '[ashare-v2] removed %s\n' "${path}"
  done < <(find "${root}" -type f -mtime +"${RETENTION_DAYS}" \( "${expr[@]}" \) -print)
}

for root in "${TARGETS[@]}"; do
  delete_matching_files "${root}" "${FILE_PATTERNS[@]}"
done

if [[ -d "${PROJECT_DIR}/logs/recovery_evidence" ]]; then
  while IFS= read -r path; do
    [[ -z "${path}" ]] && continue
    rm -rf -- "${path}"
    deleted_count=$((deleted_count + 1))
    printf '[ashare-v2] removed %s\n' "${path}"
  done < <(find "${PROJECT_DIR}/logs/recovery_evidence" -mindepth 1 -maxdepth 1 -type d -mtime +"${RETENTION_DAYS}" -print)
fi

for root in "${TARGETS[@]}" "${PROJECT_DIR}/logs/recovery_evidence"; do
  if [[ -d "${root}" ]]; then
    find "${root}" -mindepth 1 -type d -empty -delete
  fi
done

printf '[ashare-v2] cleanup completed | retention_days=%s | removed=%s\n' "${RETENTION_DAYS}" "${deleted_count}"
