#!/usr/bin/env bash
# 输出当前 Linux 控制面给 Windows Execution Gateway 的接线摘要
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

SERVICE_PORT="${ASHARE_SERVICE_PORT:-8100}"
PYTHON_BIN="$(resolve_project_python || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[ashare-v2] 未找到可用 Python 解释器，请先创建 .venv 或设置 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

BUNDLE_URL="http://127.0.0.1:${SERVICE_PORT}/system/deployment/windows-execution-gateway-onboarding-bundle"
BUNDLE_JSON="$(curl -fsS "${BUNDLE_URL}")"

export BUNDLE_JSON
"${PYTHON_BIN}" - <<'PY'
import json
import os

payload = json.loads(os.environ["BUNDLE_JSON"])
worker = payload.get("worker_entrypoint") or {}
paths = payload.get("report_paths") or {}

print("[windows-gateway] 接线摘要")
print(f"control_plane_base_url: {payload.get('control_plane_base_url', '')}")
print(f"startup_checklist_status: {(payload.get('linux_control_plane') or {}).get('startup_checklist_status', '')}")
print(f"health_post_path: {(payload.get('execution_bridge_template') or {}).get('path', '')}")
print(f"postclose_master_path: {paths.get('postclose_master', '')}")
print("")
print("[windows-gateway] 推荐 worker 命令")
print(worker.get("recommended_xtquant_command", ""))
print("")
print("[windows-gateway] 健康上报 curl 样例")
print(((payload.get("deployment_contract_sample") or {}).get("http_samples") or {}).get("curl_post_example", ""))
print("")
print("[windows-gateway] 只读检查路径")
for key in [
    "readiness",
    "execution_bridge_health_template",
    "linux_control_plane_startup_checklist",
    "postclose_master",
    "postclose_deployment_handoff",
]:
    value = paths.get(key)
    if value:
        print(f"{key}: {value}")
PY
