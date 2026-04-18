#!/usr/bin/env bash
# 单 intent 受控 apply 执行脚本
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

TRADE_DATE="$(date +%F)"
ACCOUNT_ID="${ASHARE_ACCOUNT_ID:-8890130545}"
INTENT_ID=""
ALLOWED_SYMBOL=""
DO_APPLY=0
CONFIRM_TEXT=""
API_TIMEOUT="${ASHARE_API_TIMEOUT_SECONDS:-120}"
MAX_APPLY_SUBMISSIONS_PER_DAY="${ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY:-1}"
BLOCKED_TIME_WINDOWS="${ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS:-09:25-09:35,14:55-15:00}"
OUTPUT_ROOT="${ASHARE_RECOVERY_EVIDENCE_ROOT:-$PROJECT_DIR/logs/recovery_evidence}"
EVIDENCE_TAG=""
PYTHON_BIN="$(resolve_project_python || true)"

usage() {
    cat >&2 <<'EOF'
用法:
  bash scripts/run_controlled_single_apply.sh [options]

选项:
  --trade-date YYYY-MM-DD
  --account-id ACCOUNT_ID
  --intent-id INTENT_ID                  必填
  --allowed-symbol SYMBOL               必填，例如 000001.SZ
  --evidence-tag LABEL                  可选，默认自动生成
  --apply
  --confirm APPLY

说明:
  - 默认只做严格准入检查 + 单 intent preview
  - 只有同时传入 --apply --confirm APPLY 才会执行 apply=true
  - 本脚本不会主动跑 runtime / bootstrap / round start；要求 discussion 与 intents 已经准备好
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trade-date)
            TRADE_DATE="${2:-}"
            shift 2
            ;;
        --account-id)
            ACCOUNT_ID="${2:-}"
            shift 2
            ;;
        --intent-id)
            INTENT_ID="${2:-}"
            shift 2
            ;;
        --allowed-symbol)
            ALLOWED_SYMBOL="${2:-}"
            shift 2
            ;;
        --evidence-tag)
            EVIDENCE_TAG="${2:-}"
            shift 2
            ;;
        --apply)
            DO_APPLY=1
            shift
            ;;
        --confirm)
            CONFIRM_TEXT="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[controlled-apply] 未知参数: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[controlled-apply] 未找到可用 Python 解释器，请先配置 .venv 或 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

if [[ -z "${INTENT_ID}" || -z "${ALLOWED_SYMBOL}" ]]; then
    echo "[controlled-apply] 必须同时传入 --intent-id 和 --allowed-symbol" >&2
    usage
    exit 2
fi

if [[ "${DO_APPLY}" -eq 1 && "${CONFIRM_TEXT}" != "APPLY" ]]; then
    echo "[controlled-apply] 真实派发必须显式传入: --apply --confirm APPLY" >&2
    exit 2
fi

READINESS_FILE="$(mktemp)"
PREVIEW_FILE="$(mktemp)"
APPLY_FILE="$(mktemp)"
PREVIEW_SNAPSHOT_FILE="$(mktemp)"
APPLY_SNAPSHOT_FILE="$(mktemp)"
trap 'rm -f "${READINESS_FILE}" "${PREVIEW_FILE}" "${APPLY_FILE}" "${PREVIEW_SNAPSHOT_FILE}" "${APPLY_SNAPSHOT_FILE}"' EXIT

if [[ -z "${EVIDENCE_TAG}" ]]; then
    EVIDENCE_TAG="controlled_apply_${TRADE_DATE}_${ALLOWED_SYMBOL}"
fi

build_dispatch_payload() {
    local trade_date="$1"
    local account_id="$2"
    local intent_id="$3"
    local apply_flag="$4"
    "${PYTHON_BIN}" - "${trade_date}" "${account_id}" "${intent_id}" "${apply_flag}" <<'PY'
import json
import sys

trade_date, account_id, intent_id, apply_flag = sys.argv[1:5]
payload = {
    "trade_date": trade_date,
    "account_id": account_id,
    "intent_ids": [intent_id],
    "apply": apply_flag.lower() == "true",
}
print(json.dumps(payload, ensure_ascii=False))
PY
}

extract_dispatch_summary() {
    local label="$1"
    local json_file="$2"
    "${PYTHON_BIN}" - "${label}" "${json_file}" <<'PY'
import json
import sys
from pathlib import Path

label = sys.argv[1]
payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
summary = {
    "label": label,
    "ok": payload.get("ok"),
    "status": payload.get("status"),
    "preview_count": payload.get("preview_count"),
    "queued_count": payload.get("queued_count"),
    "submitted_count": payload.get("submitted_count"),
    "blocked_count": payload.get("blocked_count"),
}
print(json.dumps(summary, ensure_ascii=False))
PY
}

collect_evidence() {
    local stage="$1"
    local safe_stage
    safe_stage="$(printf '%s' "${stage}" | tr ' /' '__')"
    bash "${SCRIPT_DIR}/collect_recovery_evidence.sh" \
        --trade-date "${TRADE_DATE}" \
        --account-id "${ACCOUNT_ID}" \
        --tag "${EVIDENCE_TAG}_${safe_stage}" \
        --output-root "${OUTPUT_ROOT}"
}

capture_route() {
    local route="$1"
    local output_file="$2"
    if ! ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "${SCRIPT_DIR}/ashare_api.sh" get "${route}" > "${output_file}" 2>/dev/null; then
        printf '{"ok":false,"error":"fetch_failed","route":"%s"}\n' "${route}" > "${output_file}"
    fi
}

build_apply_snapshot() {
    local label="$1"
    local dispatch_file="$2"
    local snapshot_file="$3"
    local latest_dispatch_file latest_receipt_file account_state_file
    latest_dispatch_file="$(mktemp)"
    latest_receipt_file="$(mktemp)"
    account_state_file="$(mktemp)"
    capture_route "/system/discussions/execution-dispatch/latest?trade_date=${TRADE_DATE}" "${latest_dispatch_file}"
    capture_route "/system/execution/gateway/receipts/latest" "${latest_receipt_file}"
    capture_route "/system/account-state?account_id=${ACCOUNT_ID}" "${account_state_file}"
    "${PYTHON_BIN}" - "${label}" "${dispatch_file}" "${latest_dispatch_file}" "${latest_receipt_file}" "${account_state_file}" > "${snapshot_file}" <<'PY'
import json
import sys
from pathlib import Path

label, dispatch_path, latest_dispatch_path, latest_receipt_path, account_state_path = sys.argv[1:6]

def load(path_str: str) -> dict:
    try:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "invalid_json", "path": path_str}

dispatch = load(dispatch_path)
latest_dispatch = load(latest_dispatch_path)
latest_receipt = load(latest_receipt_path)
account_state = load(account_state_path)

snapshot = {
    "label": label,
    "dispatch_status": dispatch.get("status"),
    "dispatch_ok": dispatch.get("ok"),
    "dispatch_counts": {
        "preview_count": dispatch.get("preview_count"),
        "queued_count": dispatch.get("queued_count"),
        "submitted_count": dispatch.get("submitted_count"),
        "blocked_count": dispatch.get("blocked_count"),
    },
    "latest_dispatch_status": latest_dispatch.get("status"),
    "latest_receipt_available": latest_receipt.get("available"),
    "latest_receipt_status": ((latest_receipt.get("receipt") or {}).get("status") if isinstance(latest_receipt.get("receipt"), dict) else None),
    "latest_receipt_id": ((latest_receipt.get("receipt") or {}).get("receipt_id") if isinstance(latest_receipt.get("receipt"), dict) else None),
    "account_total_asset": account_state.get("total_asset"),
    "account_cash": account_state.get("cash"),
}
print(json.dumps(snapshot, ensure_ascii=False))
PY
    rm -f "${latest_dispatch_file}" "${latest_receipt_file}" "${account_state_file}"
}

echo "[controlled-apply] trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID} intent_id=${INTENT_ID} symbol=${ALLOWED_SYMBOL} mode=$([[ "${DO_APPLY}" -eq 1 ]] && echo apply || echo preview)"
echo "[controlled-apply] 严格准入口径: max_apply_intents=1 require_live=true require_trading_session=true allowed_symbols=${ALLOWED_SYMBOL} max_apply_submissions_per_day=${MAX_APPLY_SUBMISSIONS_PER_DAY} blocked_time_windows=${BLOCKED_TIME_WINDOWS}"
echo "[controlled-apply] 证据标签: ${EVIDENCE_TAG}"

collect_evidence "before_preview"

ASHARE_APPLY_READY_INTENT_IDS="${INTENT_ID}" \
ASHARE_APPLY_READY_MAX_INTENTS=1 \
ASHARE_APPLY_READY_ALLOWED_SYMBOLS="${ALLOWED_SYMBOL}" \
ASHARE_APPLY_READY_REQUIRE_LIVE=true \
ASHARE_APPLY_READY_REQUIRE_TRADING_SESSION="${ASHARE_APPLY_READY_REQUIRE_TRADING_SESSION:-true}" \
ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY="${MAX_APPLY_SUBMISSIONS_PER_DAY}" \
ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS="${BLOCKED_TIME_WINDOWS}" \
ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" \
bash "${SCRIPT_DIR}/check_apply_pressure_readiness.sh" "${TRADE_DATE}" "${ACCOUNT_ID}" > "${READINESS_FILE}"

cat "${READINESS_FILE}"

PREVIEW_PAYLOAD="$(build_dispatch_payload "${TRADE_DATE}" "${ACCOUNT_ID}" "${INTENT_ID}" "false")"
ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "${SCRIPT_DIR}/ashare_api.sh" post "/system/discussions/execution-intents/dispatch" "${PREVIEW_PAYLOAD}" > "${PREVIEW_FILE}"
echo "[controlled-apply] preview: $(extract_dispatch_summary preview "${PREVIEW_FILE}")"
build_apply_snapshot "preview" "${PREVIEW_FILE}" "${PREVIEW_SNAPSHOT_FILE}"
echo "[controlled-apply] preview_snapshot: $(cat "${PREVIEW_SNAPSHOT_FILE}")"
collect_evidence "after_preview"

if [[ "${DO_APPLY}" -ne 1 ]]; then
    echo "[controlled-apply] 已停在 preview。若要真实派发，请执行："
    echo "  bash scripts/run_controlled_single_apply.sh --trade-date ${TRADE_DATE} --account-id ${ACCOUNT_ID} --intent-id ${INTENT_ID} --allowed-symbol ${ALLOWED_SYMBOL} --apply --confirm APPLY"
    exit 0
fi

collect_evidence "before_apply"
APPLY_PAYLOAD="$(build_dispatch_payload "${TRADE_DATE}" "${ACCOUNT_ID}" "${INTENT_ID}" "true")"
ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "${SCRIPT_DIR}/ashare_api.sh" post "/system/discussions/execution-intents/dispatch" "${APPLY_PAYLOAD}" > "${APPLY_FILE}"
echo "[controlled-apply] apply: $(extract_dispatch_summary apply "${APPLY_FILE}")"
build_apply_snapshot "apply" "${APPLY_FILE}" "${APPLY_SNAPSHOT_FILE}"
echo "[controlled-apply] apply_snapshot: $(cat "${APPLY_SNAPSHOT_FILE}")"
collect_evidence "after_apply"
echo "[controlled-apply] 真实派发已触发，请继续核对 gateway claim / receipt / 飞书执行回执 / QMT 对账。"
