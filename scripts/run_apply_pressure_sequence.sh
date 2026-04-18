#!/usr/bin/env bash
# apply=true 真实压测主线执行脚本
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
MAX_CANDIDATES=5
SYMBOLS_CSV=""
INTENT_ID=""
DO_APPLY=0
CONFIRM_TEXT=""
API_TIMEOUT="${ASHARE_API_TIMEOUT_SECONDS:-120}"
PYTHON_BIN="$(resolve_project_python || true)"

usage() {
    cat >&2 <<'EOF'
用法:
  bash scripts/run_apply_pressure_sequence.sh [options]

选项:
  --trade-date YYYY-MM-DD
  --account-id ACCOUNT_ID
  --symbols 600010.SH,002263.SZ
  --max-candidates N
  --intent-id INTENT_ID
  --apply
  --confirm APPLY

说明:
  - 默认只跑到 dispatch preview，不会真实派发
  - 只有同时传入 --apply --confirm APPLY 才会执行 apply=true
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
        --symbols)
            SYMBOLS_CSV="${2:-}"
            shift 2
            ;;
        --max-candidates)
            MAX_CANDIDATES="${2:-}"
            shift 2
            ;;
        --intent-id)
            INTENT_ID="${2:-}"
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
            echo "[apply-seq] 未知参数: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[apply-seq] 未找到可用 Python 解释器，请先配置 .venv 或 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

if [[ "${DO_APPLY}" -eq 1 && "${CONFIRM_TEXT}" != "APPLY" ]]; then
    echo "[apply-seq] 真实派发必须显式传入: --apply --confirm APPLY" >&2
    exit 2
fi

RUNTIME_FILE="$(mktemp)"
BOOTSTRAP_FILE="$(mktemp)"
ROUND1_FILE="$(mktemp)"
PRECHECK_FILE="$(mktemp)"
INTENTS_FILE="$(mktemp)"
PREVIEW_FILE="$(mktemp)"
APPLY_FILE="$(mktemp)"
trap 'rm -f "${RUNTIME_FILE}" "${BOOTSTRAP_FILE}" "${ROUND1_FILE}" "${PRECHECK_FILE}" "${INTENTS_FILE}" "${PREVIEW_FILE}" "${APPLY_FILE}"' EXIT

build_runtime_payload() {
    "${PYTHON_BIN}" - "${ACCOUNT_ID}" "${MAX_CANDIDATES}" "${SYMBOLS_CSV}" <<'PY'
import json
import sys

account_id, max_candidates, symbols_csv = sys.argv[1:4]
symbols = [item.strip() for item in symbols_csv.split(",") if item.strip()]
payload = {
    "account_id": account_id,
    "max_candidates": int(max_candidates),
}
if symbols:
    payload["symbols"] = symbols
print(json.dumps(payload, ensure_ascii=False))
PY
}

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
    "apply": apply_flag.lower() == "true",
}
if intent_id:
    payload["intent_ids"] = [intent_id]
print(json.dumps(payload, ensure_ascii=False))
PY
}

extract_first_intent_id() {
    local json_file="$1"
    "${PYTHON_BIN}" - "${json_file}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
items = list(payload.get("intents") or [])
print(str((items[0] or {}).get("intent_id") or "") if items else "")
PY
}

extract_summary() {
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
    "case_count": payload.get("case_count"),
    "intent_count": payload.get("intent_count"),
    "approved_count": payload.get("approved_count"),
    "blocked_count": payload.get("blocked_count"),
    "preview_count": payload.get("preview_count"),
    "queued_count": payload.get("queued_count"),
    "submitted_count": payload.get("submitted_count"),
}
print(json.dumps(summary, ensure_ascii=False))
PY
}

echo "[apply-seq] trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID} mode=$([[ "${DO_APPLY}" -eq 1 ]] && echo apply || echo preview)"
if [[ -n "${SYMBOLS_CSV}" ]]; then
    echo "[apply-seq] symbols=${SYMBOLS_CSV}"
else
    echo "[apply-seq] symbols=<runtime 默认范围>"
fi

RUNTIME_PAYLOAD="$(build_runtime_payload)"
ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" post "/runtime/jobs/pipeline" "${RUNTIME_PAYLOAD}" > "${RUNTIME_FILE}"
echo "[apply-seq] runtime: $(extract_summary runtime "${RUNTIME_FILE}")"

ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" post "/system/discussions/cycles/bootstrap" "{\"trade_date\":\"${TRADE_DATE}\"}" > "${BOOTSTRAP_FILE}"
echo "[apply-seq] bootstrap: $(extract_summary bootstrap "${BOOTSTRAP_FILE}")"

ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" post "/system/discussions/cycles/${TRADE_DATE}/rounds/1/start" "{}" > "${ROUND1_FILE}"
echo "[apply-seq] round1: $(extract_summary round1 "${ROUND1_FILE}")"

ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" get "/system/discussions/execution-precheck?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}" > "${PRECHECK_FILE}"
echo "[apply-seq] precheck: $(extract_summary precheck "${PRECHECK_FILE}")"

ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" get "/system/discussions/execution-intents?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}" > "${INTENTS_FILE}"
echo "[apply-seq] intents: $(extract_summary intents "${INTENTS_FILE}")"

if [[ -z "${INTENT_ID}" ]]; then
    INTENT_ID="$(extract_first_intent_id "${INTENTS_FILE}")"
fi

if [[ -z "${INTENT_ID}" ]]; then
    echo "[apply-seq] 当前没有可用 intent，停止在 intents 阶段。" >&2
    exit 2
fi

echo "[apply-seq] selected_intent=${INTENT_ID}"

PREVIEW_PAYLOAD="$(build_dispatch_payload "${TRADE_DATE}" "${ACCOUNT_ID}" "${INTENT_ID}" "false")"
ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" post "/system/discussions/execution-intents/dispatch" "${PREVIEW_PAYLOAD}" > "${PREVIEW_FILE}"
echo "[apply-seq] preview: $(extract_summary preview "${PREVIEW_FILE}")"

if [[ "${DO_APPLY}" -ne 1 ]]; then
    echo "[apply-seq] 已停在 preview。若要真实派发，请在交易时段执行："
    echo "  bash scripts/run_apply_pressure_sequence.sh --trade-date ${TRADE_DATE} --account-id ${ACCOUNT_ID} --intent-id ${INTENT_ID} --apply --confirm APPLY"
    exit 0
fi

echo "[apply-seq] 执行 apply 前再次做 readiness 检查..."
bash "${SCRIPT_DIR}/check_apply_pressure_readiness.sh" "${TRADE_DATE}" "${ACCOUNT_ID}"

APPLY_PAYLOAD="$(build_dispatch_payload "${TRADE_DATE}" "${ACCOUNT_ID}" "${INTENT_ID}" "true")"
ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" post "/system/discussions/execution-intents/dispatch" "${APPLY_PAYLOAD}" > "${APPLY_FILE}"
echo "[apply-seq] apply: $(extract_summary apply "${APPLY_FILE}")"
echo "[apply-seq] 真实派发已触发，请继续核对 gateway claim / receipt / 飞书回执 / QMT 对账。"
