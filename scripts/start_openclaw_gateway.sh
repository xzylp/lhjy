#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${1:-/tmp/openclaw-gateway.log}"
mkdir -p "$(dirname "$LOG_FILE")"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

EXECUTION_PLANE="${ASHARE_EXECUTION_PLANE:-windows_gateway}"

unset HTTP_PROXY
unset HTTPS_PROXY
unset http_proxy
unset https_proxy
unset ALL_PROXY
unset all_proxy

export NO_PROXY="127.0.0.1,localhost,open.feishu.cn,ws-open.feishu.cn"
export no_proxy="$NO_PROXY"

echo "[openclaw] 启动 Linux control plane gateway"
echo "[openclaw] execution_plane=${EXECUTION_PLANE} / log_file=${LOG_FILE}"
if [ "$EXECUTION_PLANE" = "windows_gateway" ]; then
    echo "[openclaw] 下单写口仍位于 Windows Execution Gateway，本机不直接持有 QMT 会话"
fi

exec /home/yxz/.npm-global/bin/openclaw gateway >>"$LOG_FILE" 2>&1
