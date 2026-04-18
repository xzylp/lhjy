#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${1:-/tmp/openclaw-gateway.log}"
mkdir -p "$(dirname "$LOG_FILE")"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"
rotate_log_file_if_needed "${LOG_FILE}" "${OPENCLAW_GATEWAY_LOG_MAX_MB:-50}"

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

EXECUTION_PLANE="${ASHARE_EXECUTION_PLANE:-windows_gateway}"
OPENCLAW_CMD="$(resolve_openclaw_bin || true)"
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18890}"
OPENCLAW_GATEWAY_BIND="${OPENCLAW_GATEWAY_BIND:-lan}"
OPENCLAW_GATEWAY_ALLOW_UNCONFIGURED="${OPENCLAW_GATEWAY_ALLOW_UNCONFIGURED:-true}"
OPENCLAW_GATEWAY_AUTH="${OPENCLAW_GATEWAY_AUTH:-}"

if [[ -z "${OPENCLAW_CMD}" ]]; then
    echo "[openclaw] 未找到 openclaw 可执行文件，请先安装 openclaw 或设置 OPENCLAW_BIN" >&2
    exit 1
fi

unset HTTP_PROXY
unset HTTPS_PROXY
unset http_proxy
unset https_proxy
unset ALL_PROXY
unset all_proxy

export NO_PROXY="127.0.0.1,localhost,open.feishu.cn,ws-open.feishu.cn"
export no_proxy="$NO_PROXY"

OPENCLAW_ARGS=(gateway --bind "${OPENCLAW_GATEWAY_BIND}" --port "${OPENCLAW_GATEWAY_PORT}")
if [[ "${OPENCLAW_GATEWAY_ALLOW_UNCONFIGURED}" == "true" ]]; then
    OPENCLAW_ARGS+=(--allow-unconfigured)
fi
if [[ -n "${OPENCLAW_GATEWAY_AUTH}" ]]; then
    OPENCLAW_ARGS+=(--auth "${OPENCLAW_GATEWAY_AUTH}")
fi

echo "[openclaw] 启动 Linux control plane gateway"
echo "[openclaw] execution_plane=${EXECUTION_PLANE} / log_file=${LOG_FILE}"
echo "[openclaw] binary=${OPENCLAW_CMD}"
echo "[openclaw] bind=${OPENCLAW_GATEWAY_BIND} / port=${OPENCLAW_GATEWAY_PORT} / allow_unconfigured=${OPENCLAW_GATEWAY_ALLOW_UNCONFIGURED}"
if [ "$EXECUTION_PLANE" = "windows_gateway" ]; then
    echo "[openclaw] 下单写口仍位于 Windows Execution Gateway，本机不直接持有 QMT 会话"
fi

exec "${OPENCLAW_CMD}" "${OPENCLAW_ARGS[@]}" >>"$LOG_FILE" 2>&1
