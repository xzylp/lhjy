#!/usr/bin/env bash
# apply=true 真实压测前置检查
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
MAX_APPLY_INTENTS="${ASHARE_APPLY_READY_MAX_INTENTS:-1}"
INTENT_IDS_CSV="${ASHARE_APPLY_READY_INTENT_IDS:-}"
ALLOWED_SYMBOLS_CSV="${ASHARE_APPLY_READY_ALLOWED_SYMBOLS:-}"
REQUIRE_LIVE="${ASHARE_APPLY_READY_REQUIRE_LIVE:-false}"
REQUIRE_TRADING_SESSION="${ASHARE_APPLY_READY_REQUIRE_TRADING_SESSION:-false}"
MAX_EQUITY_POSITION_LIMIT="${ASHARE_APPLY_READY_MAX_EQUITY_POSITION_LIMIT:-0.2}"
MAX_SINGLE_AMOUNT="${ASHARE_APPLY_READY_MAX_SINGLE_AMOUNT:-50000}"
MIN_REVERSE_REPO_RESERVED_AMOUNT="${ASHARE_APPLY_READY_MIN_REVERSE_REPO_RESERVED_AMOUNT:-70000}"
MAX_STOCK_TEST_BUDGET_AMOUNT="${ASHARE_APPLY_READY_MAX_STOCK_BUDGET_AMOUNT:-}"
MAX_APPLY_SUBMISSIONS_PER_DAY="${ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY:-}"
BLOCKED_TIME_WINDOWS="${ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS:-}"
PYTHON_BIN="$(resolve_project_python || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[apply-pressure] 未找到可用 Python 解释器，请先配置 .venv 或 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

READY_FILE="$(mktemp)"
ERR_FILE="$(mktemp)"
trap 'rm -f "${READY_FILE}" "${ERR_FILE}"' EXIT

ROUTE="/system/deployment/controlled-apply-readiness?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}&max_apply_intents=${MAX_APPLY_INTENTS}&require_live=${REQUIRE_LIVE}&require_trading_session=${REQUIRE_TRADING_SESSION}&max_equity_position_limit=${MAX_EQUITY_POSITION_LIMIT}&max_single_amount=${MAX_SINGLE_AMOUNT}&min_reverse_repo_reserved_amount=${MIN_REVERSE_REPO_RESERVED_AMOUNT}&include_details=false"
if [[ -n "${MAX_STOCK_TEST_BUDGET_AMOUNT}" ]]; then
    ROUTE="${ROUTE}&max_stock_test_budget_amount=${MAX_STOCK_TEST_BUDGET_AMOUNT}"
fi
if [[ -n "${INTENT_IDS_CSV}" ]]; then
    ROUTE="${ROUTE}&intent_ids=${INTENT_IDS_CSV}"
fi
if [[ -n "${ALLOWED_SYMBOLS_CSV}" ]]; then
    ROUTE="${ROUTE}&allowed_symbols=${ALLOWED_SYMBOLS_CSV}"
fi
if [[ -n "${MAX_APPLY_SUBMISSIONS_PER_DAY}" ]]; then
    ROUTE="${ROUTE}&max_apply_submissions_per_day=${MAX_APPLY_SUBMISSIONS_PER_DAY}"
fi
if [[ -n "${BLOCKED_TIME_WINDOWS}" ]]; then
    ROUTE="${ROUTE}&blocked_time_windows=${BLOCKED_TIME_WINDOWS}"
fi

if ! ASHARE_API_TIMEOUT_SECONDS="${API_TIMEOUT}" "$SCRIPT_DIR/ashare_api.sh" get "${ROUTE}" > "${READY_FILE}" 2>"${ERR_FILE}"; then
    error_text="$(tr '\n' ' ' < "${ERR_FILE}")"
    if grep -q "404" "${ERR_FILE}"; then
        echo "[apply-pressure] ROUTE_UNAVAILABLE trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID}" >&2
        echo "- 路由未在线: ${ROUTE}" >&2
        echo "- curl: ${error_text}" >&2
        exit 3
    fi
    echo "[apply-pressure] REQUEST_FAILED trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID}" >&2
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
print(f"[apply-pressure] {label} trade_date={trade_date} account_id={account_id}")

for item in list(payload.get("checks") or []):
    name = str(item.get("name") or "-")
    ok = str(item.get("status") or "") == "ok"
    detail = str(item.get("detail") or "")
    print(f"- {name}: {'OK' if ok else 'NO'} | {detail}")

first_intent = payload.get("first_intent") or {}
if first_intent:
    print(
        "- 首条 intent: "
        f"{first_intent.get('intent_id')} {first_intent.get('symbol')} qty={first_intent.get('quantity')} price={first_intent.get('price')}"
    )
else:
    print("- 首条 intent: 无")

summary_lines = list(payload.get("summary_lines") or [])
if summary_lines:
    print("- 摘要:")
    for line in summary_lines[:6]:
        print(f"  {line}")

if status != "ready":
    blocked = [str(item.get("name") or "") for item in list(payload.get("checks") or []) if str(item.get("status") or "") == "blocked"]
    print(f"- 未满足项: {', '.join(blocked) if blocked else 'unknown'}")
    sys.exit(2)
PY
