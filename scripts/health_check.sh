#!/usr/bin/env bash
# ashare-system-v2 健康巡检
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

EXECUTION_PLANE="${ASHARE_EXECUTION_PLANE:-windows_gateway}"
SERVICE_HOST="${ASHARE_SERVICE_HOST:-127.0.0.1}"
SERVICE_PORT="${ASHARE_SERVICE_PORT:-8100}"

echo "[ashare-v2] 健康巡检..."
echo "[ashare-v2] 执行平面: ${EXECUTION_PLANE}"
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
.venv/Scripts/python.exe -m ashare_system.run healthcheck
