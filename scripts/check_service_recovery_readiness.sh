#!/usr/bin/env bash
# 服务重启 / 断链恢复前置检查
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

TRADE_DATE="${1:-$(date +%F)}"
ACCOUNT_ID="${2:-${ASHARE_ACCOUNT_ID:-8890130545}}"
API_TIMEOUT="${ASHARE_API_TIMEOUT_SECONDS:-60}"
MAX_WORKSPACE_AGE_SECONDS="${ASHARE_RECOVERY_MAX_WORKSPACE_AGE_SECONDS:-1800}"
MAX_SIGNAL_AGE_SECONDS="${ASHARE_RECOVERY_MAX_SIGNAL_AGE_SECONDS:-1800}"
REQUIRE_EXECUTION_BRIDGE="${ASHARE_RECOVERY_REQUIRE_EXECUTION_BRIDGE:-true}"
PYTHON_BIN="$(resolve_project_python || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[service-recovery] 未找到可用 Python 解释器，请先配置 .venv 或 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

READY_FILE="$(mktemp)"
ERR_FILE="$(mktemp)"
trap 'rm -f "${READY_FILE}" "${ERR_FILE}"' EXIT

ROUTE="/system/deployment/service-recovery-readiness?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}&max_workspace_age_seconds=${MAX_WORKSPACE_AGE_SECONDS}&max_signal_age_seconds=${MAX_SIGNAL_AGE_SECONDS}&require_execution_bridge=${REQUIRE_EXECUTION_BRIDGE}&include_details=false"

if ! ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" get "${ROUTE}" > "${READY_FILE}" 2>"${ERR_FILE}"; then
    error_text="$(tr '\n' ' ' < "${ERR_FILE}")"
    if grep -q "404" "${ERR_FILE}"; then
        echo "[service-recovery] ROUTE_UNAVAILABLE trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID}" >&2
        echo "- 路由未在线: ${ROUTE}" >&2
        echo "- curl: ${error_text}" >&2
        exit 3
    fi
    echo "[service-recovery] REQUEST_FAILED trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID}" >&2
    echo "- 路由: ${ROUTE}" >&2
    echo "- curl: ${error_text}" >&2
    exit 1
fi

"${PYTHON_BIN}" - "${TRADE_DATE}" "${ACCOUNT_ID}" "${READY_FILE}" <<'PY'
import json
import sys
from pathlib import Path


trade_date, account_id, payload_path = sys.argv[1:4]
payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
status = str(payload.get("status") or "blocked")
label = "READY" if status == "ready" else status.upper()
print(f"[service-recovery] {label} trade_date={trade_date} account_id={account_id}")

for item in list(payload.get("checks") or []):
    name = str(item.get("name") or "-")
    ok = str(item.get("status") or "") == "ok"
    detail = str(item.get("detail") or "")
    print(f"- {name}: {'OK' if ok else 'NO'} | {detail}")

summary_lines = list(payload.get("summary_lines") or [])
if summary_lines:
    print("- 摘要:")
    for line in summary_lines[:6]:
        print(f"  {line}")

if status != "ready":
    blocked = [
        str(item.get("name") or "")
        for item in list(payload.get("checks") or [])
        if str(item.get("status") or "") == "blocked"
    ]
    print(f"- 未恢复项: {', '.join(blocked) if blocked else 'unknown'}")
    sys.exit(2)
PY
