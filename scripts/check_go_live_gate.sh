#!/usr/bin/env bash
# 正式上线 / 压测准入总检查
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
PYTHON_BIN="$(resolve_project_python || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[go-live-gate] 未找到可用 Python 解释器，请先配置 .venv 或 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

fetch_json() {
    local route="$1"
    local filename="$2"
    if ! ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" get "${route}" > "${TMP_DIR}/${filename}.json" 2>"${TMP_DIR}/${filename}.err"; then
        printf '{"ok":false,"error":"fetch_failed","route":"%s"}\n' "${route}" > "${TMP_DIR}/${filename}.json"
    fi
}

fetch_json "/health" "health"
fetch_json "/system/operations/components" "operations_components"
fetch_json "/system/deployment/service-recovery-readiness?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}&include_details=false" "service_recovery"
fetch_json "/system/deployment/controlled-apply-readiness?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}&require_live=true&require_trading_session=true&include_details=false" "controlled_apply"
fetch_json "/system/feishu/longconn/status" "feishu_longconn"
fetch_json "/system/feishu/briefing?trade_date=${TRADE_DATE}" "feishu_briefing"
fetch_json "/system/agents/supervision-board?trade_date=${TRADE_DATE}&overdue_after_seconds=180" "supervision_board"
fetch_json "/system/discussions/execution-dispatch/latest?trade_date=${TRADE_DATE}" "execution_dispatch_latest"
fetch_json "/system/execution/gateway/receipts/latest" "execution_receipt_latest"
fetch_json "/system/readiness?account_id=${ACCOUNT_ID}" "readiness"

"${PYTHON_BIN}" - "${TRADE_DATE}" "${ACCOUNT_ID}" "${TMP_DIR}" <<'PY'
import json
import sys
from pathlib import Path


trade_date, account_id, tmp_dir = sys.argv[1:4]
root = Path(tmp_dir)


def load(name: str) -> dict:
    path = root / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "invalid_json", "name": name}


health = load("health")
components = load("operations_components")
service_recovery = load("service_recovery")
controlled_apply = load("controlled_apply")
feishu_longconn = load("feishu_longconn")
feishu_briefing = load("feishu_briefing")
supervision_board = load("supervision_board")
execution_dispatch = load("execution_dispatch_latest")
execution_receipt = load("execution_receipt_latest")
readiness = load("readiness")


def check_map(payload: dict) -> dict[str, dict]:
    return {
        str(item.get("name") or ""): item
        for item in list(payload.get("checks") or [])
        if isinstance(item, dict)
    }


service_checks = check_map(service_recovery)
apply_checks = check_map(controlled_apply)

component_list = list(components.get("components") or [])
component_status = {str(item.get("name") or ""): str(item.get("status") or "") for item in component_list if isinstance(item, dict)}

latest_receipt = execution_receipt.get("receipt") if isinstance(execution_receipt.get("receipt"), dict) else {}
dispatch_status = str(execution_dispatch.get("status") or "not_found")
receipt_status = str(latest_receipt.get("status") or "")
longconn_ok = str(feishu_longconn.get("status") or "") == "connected" and bool(feishu_longconn.get("is_fresh"))
briefing_ok = bool(feishu_briefing.get("summary_lines"))

overdue_agents = [
    item for item in list(supervision_board.get("items") or [])
    if isinstance(item, dict) and str(item.get("attention_status") or "") == "overdue"
]
attention_agents = [
    item for item in list(supervision_board.get("items") or [])
    if isinstance(item, dict) and str(item.get("attention_status") or "") in {"needs_work", "overdue"}
]

gate_results: list[tuple[str, bool, str]] = []

linux_services_ok = (
    str(service_recovery.get("status") or "") in {"ready", "degraded"}
    and component_status.get("feishu_longconn") == "connected"
    and longconn_ok
)
gate_results.append(
    (
        "准入1_linux_services",
        linux_services_ok,
        f"service_recovery={service_recovery.get('status')} feishu_component={component_status.get('feishu_longconn')} longconn={feishu_longconn.get('status')} fresh={feishu_longconn.get('is_fresh')}",
    )
)

windows_bridge_ok = (
    str(readiness.get("status") or "") in {"ready", "degraded"}
    and str(apply_checks.get("execution_bridge", {}).get("status") or "") == "ok"
)
gate_results.append(
    (
        "准入2_windows_bridge",
        windows_bridge_ok,
        f"readiness={readiness.get('status')} execution_bridge={apply_checks.get('execution_bridge', {}).get('status')} detail={apply_checks.get('execution_bridge', {}).get('detail')}",
    )
)

apply_closed_loop_ok = (
    dispatch_status in {"queued_for_gateway", "submitted"}
    and bool(execution_receipt.get("available"))
)
gate_results.append(
    (
        "准入3_apply_closed_loop",
        apply_closed_loop_ok,
        f"dispatch_status={dispatch_status} receipt_available={execution_receipt.get('available')} receipt_status={receipt_status or 'missing'}",
    )
)

agent_chain_ok = (
    str(service_recovery.get("status") or "") in {"ready", "degraded"}
    and str(supervision_board.get("available", True)).lower() != "false"
)
gate_results.append(
    (
        "准入4_agent_chain",
        agent_chain_ok,
        f"service_recovery={service_recovery.get('status')} supervision_available={supervision_board.get('available', True)} overdue={len(overdue_agents)}",
    )
)

feishu_delivery_ok = longconn_ok and briefing_ok and len(overdue_agents) == 0
gate_results.append(
    (
        "准入5_feishu_delivery",
        feishu_delivery_ok,
        f"longconn={feishu_longconn.get('status')} fresh={feishu_longconn.get('is_fresh')} briefing_lines={len(list(feishu_briefing.get('summary_lines') or []))} attention_agents={len(attention_agents)} overdue_agents={len(overdue_agents)}",
    )
)

overall_ok = all(item[1] for item in gate_results)
label = "READY" if overall_ok else "BLOCKED"
print(f"[go-live-gate] {label} trade_date={trade_date} account_id={account_id}")

for name, ok, detail in gate_results:
    print(f"- {name}: {'OK' if ok else 'NO'} | {detail}")

print("- 关键摘要:")
for line in list(feishu_briefing.get("summary_lines") or [])[:4]:
    print(f"  {line}")
for line in list(execution_dispatch.get("summary_lines") or [])[:3]:
    print(f"  {line}")

if not overall_ok:
    blocked = [name for name, ok, _detail in gate_results if not ok]
    print(f"- 未通过项: {', '.join(blocked)}")
    sys.exit(2)
PY
