#!/usr/bin/env bash
# ashare-system-v2 飞书长连接服务管理
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"

if [[ -f ".env" ]]; then
    set -a
    source ".env"
    set +a
fi

ACTION="status"
SERVICE_SCOPE="${ASHARE_FEISHU_LONGCONN_SCOPE:-user}"
SERVICE_NAME="${SERVICE_NAME:-ashare-feishu-longconn.service}"
LOG_LINES=100
WAIT_TIMEOUT="${ASHARE_FEISHU_LONGCONN_WAIT_TIMEOUT:-45}"
WAIT_INTERVAL="${ASHARE_FEISHU_LONGCONN_WAIT_INTERVAL:-3}"

usage() {
    cat >&2 <<'EOF'
Usage:
  ashare_feishu_longconn_ctl.sh [--user|--system] start
  ashare_feishu_longconn_ctl.sh [--user|--system] stop
  ashare_feishu_longconn_ctl.sh [--user|--system] restart
  ashare_feishu_longconn_ctl.sh [--user|--system] status
  ashare_feishu_longconn_ctl.sh [--user|--system] logs [line_count]
  ashare_feishu_longconn_ctl.sh [--user|--system] enable
  ashare_feishu_longconn_ctl.sh [--user|--system] disable
  ashare_feishu_longconn_ctl.sh [--user|--system] verify

说明:
  - 默认按用户级 systemd (`systemctl --user`) 管理
  - `restart/start` 后会等待 `/system/feishu/longconn/status` 回到 `connected + is_fresh=true`
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            SERVICE_SCOPE="user"
            shift
            ;;
        --system)
            SERVICE_SCOPE="system"
            shift
            ;;
        start|stop|restart|status|logs|enable|disable|verify)
            ACTION="$1"
            shift
            if [[ "${ACTION}" == "logs" && $# -gt 0 ]]; then
                LOG_LINES="$1"
                shift
            fi
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ashare-feishu-longconn] 未知参数: $1" >&2
            usage
            exit 2
            ;;
    esac
done

systemctl_cmd() {
    if [[ "${SERVICE_SCOPE}" == "user" ]]; then
        systemctl --user "$@"
    else
        sudo systemctl "$@"
    fi
}

journalctl_cmd() {
    if [[ "${SERVICE_SCOPE}" == "user" ]]; then
        journalctl --user -u "${SERVICE_NAME}" -n "${LOG_LINES}" --no-pager
    else
        sudo journalctl -u "${SERVICE_NAME}" -n "${LOG_LINES}" --no-pager
    fi
}

verify_longconn_health() {
    local timeout_seconds="$1"
    local interval_seconds="$2"
    local deadline=$(( $(date +%s) + timeout_seconds ))
    local last_status="unknown"
    local last_fresh="unknown"
    local payload=""
    local python_bin=""
    local result=""
    local reported_status=""
    local pid_alive=""

    while [[ $(date +%s) -le ${deadline} ]]; do
        if payload="$("$SCRIPT_DIR/ashare_api.sh" get "/system/feishu/longconn/status" 2>/dev/null)"; then
            if python_bin="$(resolve_project_python 2>/dev/null)" && [[ -n "${python_bin}" ]]; then
                if result="$("${python_bin}" - "${payload}" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
status = str(payload.get("status") or "unknown")
is_fresh = bool(payload.get("is_fresh"))
reported_status = str(payload.get("reported_status") or "")
pid_alive = bool(payload.get("pid_alive"))
print(f"{status}|{str(is_fresh).lower()}|{reported_status}|{str(pid_alive).lower()}")
PY
)"; then
                    IFS='|' read -r last_status last_fresh reported_status pid_alive <<< "${result}"
                    if [[ "${last_status}" == "connected" && "${last_fresh}" == "true" ]]; then
                        echo "[ashare-feishu-longconn] verify OK status=${last_status} reported_status=${reported_status} pid_alive=${pid_alive}"
                        return 0
                    fi
                fi
            fi
        fi
        sleep "${interval_seconds}"
    done

    echo "[ashare-feishu-longconn] verify timeout status=${last_status} is_fresh=${last_fresh}" >&2
    return 1
}

case "${ACTION}" in
    start)
        systemctl_cmd start "${SERVICE_NAME}"
        systemctl_cmd --no-pager --full status "${SERVICE_NAME}"
        verify_longconn_health "${WAIT_TIMEOUT}" "${WAIT_INTERVAL}"
        ;;
    stop)
        systemctl_cmd stop "${SERVICE_NAME}"
        ;;
    restart)
        systemctl_cmd restart "${SERVICE_NAME}"
        systemctl_cmd --no-pager --full status "${SERVICE_NAME}"
        verify_longconn_health "${WAIT_TIMEOUT}" "${WAIT_INTERVAL}"
        ;;
    status)
        systemctl_cmd --no-pager --full status "${SERVICE_NAME}"
        ;;
    logs)
        journalctl_cmd
        ;;
    enable)
        systemctl_cmd enable "${SERVICE_NAME}"
        ;;
    disable)
        systemctl_cmd disable "${SERVICE_NAME}"
        ;;
    verify)
        verify_longconn_health "${WAIT_TIMEOUT}" "${WAIT_INTERVAL}"
        ;;
    *)
        usage
        exit 2
        ;;
esac
