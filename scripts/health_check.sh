#!/usr/bin/env bash
# ashare-system-v2 健康巡检
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

EXECUTION_PLANE="${ASHARE_EXECUTION_PLANE:-windows_gateway}"
SERVICE_HOST="${ASHARE_SERVICE_HOST:-127.0.0.1}"
SERVICE_PORT="${ASHARE_SERVICE_PORT:-8100}"
GO_PLATFORM_PORT="${ASHARE_GO_PLATFORM_PORT:-18793}"
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18890}"
PYTHON_BIN="$(resolve_project_python || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[ashare-v2] 未找到可用 Python 解释器，请先创建 .venv 或设置 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

echo "[ashare-v2] 健康巡检..."
echo "[ashare-v2] 执行平面: ${EXECUTION_PLANE}"
echo "[ashare-v2] Python: ${PYTHON_BIN}"

check_ok=true

report_check() {
    local label="$1"
    local status="$2"
    local detail="$3"
    printf '[ashare-v2] %-28s %-4s %s\n' "${label}" "${status}" "${detail}"
    if [[ "${status}" != "OK" ]]; then
        check_ok=false
    fi
}

check_user_unit_active() {
    local unit="$1"
    local state
    state="$(systemctl --user is-active "${unit}" 2>/dev/null || true)"
    if [[ "${state}" == "active" ]]; then
        report_check "systemd:${unit}" "OK" "${state}"
    else
        report_check "systemd:${unit}" "NO" "${state:-unknown}"
    fi
}

check_http_json() {
    local label="$1"
    local url="$2"
    local body
    if body="$(curl --silent --show-error --max-time 5 "${url}" 2>/dev/null)"; then
        report_check "${label}" "OK" "${url}"
        printf '%s\n' "${body}" | sed 's/^/[ashare-v2]   body: /'
    else
        report_check "${label}" "NO" "${url}"
    fi
}

check_http_code() {
    local label="$1"
    local url="$2"
    local code
    code="$(curl --silent --show-error --max-time 5 -o /dev/null -w '%{http_code}' "${url}" 2>/dev/null || true)"
    if [[ "${code}" == "200" ]]; then
        report_check "${label}" "OK" "${url}"
    else
        report_check "${label}" "NO" "${url} code=${code:-fail}"
    fi
}

check_listener() {
    local label="$1"
    local port="$2"
    if ss -ltn | awk '{print $4}' | rg -q ":${port}\$"; then
        report_check "${label}" "OK" "port=${port}"
    else
        report_check "${label}" "NO" "port=${port}"
    fi
}

check_single_process() {
    local label="$1"
    local pattern="$2"
    local count
    count="$(pgrep -af "${pattern}" | wc -l | tr -d ' ')"
    count="${count:-0}"
    if [[ "${count}" == "1" ]]; then
        report_check "${label}" "OK" "count=${count}"
    else
        report_check "${label}" "NO" "count=${count}"
    fi
}

echo "[ashare-v2] 用户级统一栈检查:"
check_user_unit_active "ashare-stack.target"
check_user_unit_active "ashare-system-v2.service"
check_user_unit_active "ashare-scheduler.service"
check_user_unit_active "ashare-go-data-platform.service"
check_user_unit_active "ashare-feishu-longconn.service"
check_user_unit_active "hermes-gateway-ashare-backup.service"
check_user_unit_active "openclaw-gateway.service"

echo "[ashare-v2] 端口监听检查:"
check_listener "listener:8100" "${SERVICE_PORT}"
check_listener "listener:18793" "${GO_PLATFORM_PORT}"
check_listener "listener:18890" "${OPENCLAW_GATEWAY_PORT}"

echo "[ashare-v2] HTTP 存活检查:"
check_http_json "http:/health" "http://127.0.0.1:${SERVICE_PORT}/health"
check_http_code "http:/dashboard" "http://127.0.0.1:${SERVICE_PORT}/dashboard/"
check_http_json "http:/system/health" "http://127.0.0.1:${SERVICE_PORT}/system/health"
check_http_json "http:/runtime/health" "http://127.0.0.1:${SERVICE_PORT}/runtime/health"
check_http_json "http:/research/health" "http://127.0.0.1:${SERVICE_PORT}/research/health"
check_http_json "http:/execution/health" "http://127.0.0.1:${SERVICE_PORT}/execution/health"
check_http_json "http:/market/health" "http://127.0.0.1:${SERVICE_PORT}/market/health"
check_http_json "http:/go-platform" "http://127.0.0.1:${GO_PLATFORM_PORT}/health"

echo "[ashare-v2] 重复进程检查:"
check_single_process "proc:serve" "ashare_system.run serve"
check_single_process "proc:scheduler" "ashare_system.run scheduler"
check_single_process "proc:go-data" "go_data_platform/go-data-platform"
check_single_process "proc:feishu" "ashare_system.run feishu-longconn"
check_single_process "proc:hermes" "hermes_cli.main --profile ashare-backup gateway run --replace"
check_single_process "proc:openclaw-gw" "openclaw-gateway"

if [ "$EXECUTION_PLANE" = "windows_gateway" ]; then
    echo "[ashare-v2] Linux control plane 启动检查项:"
    echo "  - Linux 不直接持有 QMT 会话，不假设本地下单"
    echo "  - 主 source_id=windows-vm-a, deployment_role=primary_gateway"
    echo "  - 主 bridge_path=linux_openclaw -> windows_gateway -> qmt_vm"
    echo "  - 备 source_id=windows-vm-b, deployment_role=backup_gateway"
    echo "  - 备 bridge_path=linux_openclaw -> windows_gateway_backup -> qmt_vm"
    echo "  - 启动后建议查看: http://${SERVICE_HOST}:${SERVICE_PORT}/system/deployment/linux-control-plane-startup-checklist"
    echo "  - Windows Gateway 接线包: http://${SERVICE_HOST}:${SERVICE_PORT}/system/deployment/windows-execution-gateway-onboarding-bundle"
fi

echo "[ashare-v2] 配置与适配器检查:"
"${PYTHON_BIN}" -m ashare_system.run healthcheck || check_ok=false

if [[ "${check_ok}" == "true" ]]; then
    echo "[ashare-v2] 总结: OK"
    exit 0
fi

echo "[ashare-v2] 总结: FAIL"
exit 1
