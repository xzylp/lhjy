#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT_DIR/scripts/ashare_api.sh"
MAX_CANDIDATES="${1:-10}"
ACCOUNT_ID="${2:-${ASHARE_ACCOUNT_ID:-8890130545}}"

usage() {
  cat <<'EOF'
Usage:
  smoke_quant_joint.sh [max_candidates] [account_id]

What it does:
  1. Probe ashare-system-v2 and runtime health
  2. Run one non-destructive runtime pipeline
  3. Read execution-precheck and execution-intents
  4. Preview-dispatch current intents with apply=false
  5. Read execution-dispatch/latest and client-brief
  6. Optionally print quant OpenClaw status when CLI is available

Notes:
  - This script never submits real orders.
  - It is intended for WSL-side quant / ashare joint smoke validation.
EOF
}

json_pretty() {
  python3 -m json.tool 2>/dev/null || cat
}

print_summary() {
  local kind="$1"
  python3 -c '
import json
import sys

kind = sys.argv[1]
data = json.load(sys.stdin)

if kind == "pipeline":
    out = {
        "job_id": data.get("job_id"),
        "generated_at": data.get("generated_at"),
        "account_id": data.get("account_id"),
        "candidates_evaluated": data.get("candidates_evaluated"),
        "top_picks": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "rank": item.get("rank"),
                "selection_score": item.get("selection_score"),
                "action": item.get("action"),
            }
            for item in data.get("top_picks", [])[:5]
        ],
        "summary": data.get("summary"),
    }
elif kind == "precheck":
    out = {
        "trade_date": data.get("trade_date"),
        "account_id": data.get("account_id"),
        "status": data.get("status"),
        "approved_count": data.get("approved_count"),
        "blocked_count": data.get("blocked_count"),
        "stock_test_budget_remaining": data.get("stock_test_budget_remaining"),
        "reverse_repo_value": data.get("reverse_repo_value"),
        "summary_lines": data.get("summary_lines", []),
        "items": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "approved": item.get("approved"),
                "proposed_quantity": item.get("proposed_quantity"),
                "proposed_value": item.get("proposed_value"),
                "primary_blocker_label": item.get("primary_blocker_label"),
            }
            for item in data.get("items", [])[:5]
        ],
    }
elif kind == "intents":
    out = {
        "trade_date": data.get("trade_date"),
        "account_id": data.get("account_id"),
        "status": data.get("status"),
        "intent_count": data.get("intent_count"),
        "blocked_count": data.get("blocked_count"),
        "summary_lines": data.get("summary_lines", []),
        "intents": [
            {
                "intent_id": item.get("intent_id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "side": item.get("side"),
                "quantity": item.get("quantity"),
                "price": item.get("price"),
                "estimated_value": item.get("estimated_value"),
            }
            for item in data.get("intents", [])[:5]
        ],
    }
elif kind == "dispatch":
    out = {
        "trade_date": data.get("trade_date"),
        "account_id": data.get("account_id"),
        "status": data.get("status"),
        "submitted_count": data.get("submitted_count"),
        "preview_count": data.get("preview_count"),
        "blocked_count": data.get("blocked_count"),
        "summary_notification": data.get("summary_notification"),
        "summary_lines": data.get("summary_lines", []),
        "receipts": [
            {
                "intent_id": item.get("intent_id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "order_id": (item.get("order") or {}).get("order_id"),
            }
            for item in data.get("receipts", [])[:5]
        ],
    }
elif kind == "client_brief":
    out = {
        "trade_date": data.get("trade_date"),
        "status": data.get("status"),
        "selected_display": data.get("selected_display"),
        "watchlist_display": data.get("watchlist_display"),
        "rejected_display": data.get("rejected_display"),
        "execution_precheck_status": data.get("execution_precheck_status"),
        "execution_precheck_approved_count": data.get("execution_precheck_approved_count"),
        "execution_precheck_blocked_count": data.get("execution_precheck_blocked_count"),
        "execution_dispatch_status": data.get("execution_dispatch_status"),
        "execution_dispatch_submitted_count": data.get("execution_dispatch_submitted_count"),
        "execution_dispatch_preview_count": data.get("execution_dispatch_preview_count"),
        "execution_dispatch_blocked_count": data.get("execution_dispatch_blocked_count"),
        "execution_dispatch_lines": data.get("execution_dispatch_lines", []),
        "lines": data.get("lines", []),
    }
elif kind == "final_brief":
    out = {
        "trade_date": data.get("trade_date"),
        "status": data.get("status"),
        "selected_display": data.get("selected_display"),
        "watchlist_display": data.get("watchlist_display"),
        "rejected_display": data.get("rejected_display"),
        "blockers": data.get("blockers", []),
        "lines": data.get("lines", []),
    }
else:
    out = data

print(json.dumps(out, ensure_ascii=False, indent=2))
' "$kind"
}

extract_trade_date() {
  python3 -c 'import json,sys; data=json.load(sys.stdin); print((data.get("generated_at") or data.get("trade_date") or "").split("T")[0])'
}

extract_intent_ids_json() {
  python3 -c 'import json,sys; data=json.load(sys.stdin); intents=data.get("intents") or data.get("items") or []; ids=[item.get("intent_id") for item in intents if item.get("intent_id")]; print(json.dumps(ids, ensure_ascii=False))'
}

extract_intent_count() {
  python3 -c 'import json,sys; data=json.load(sys.stdin); intents=data.get("intents") or data.get("items") or []; print(len([item for item in intents if item.get("intent_id")]))'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

echo "== quant status =="
if command -v openclaw >/dev/null 2>&1; then
  openclaw --profile quant status --all || true
else
  echo "openclaw CLI not found, skipped."
fi

echo
echo "== probe =="
"$API" probe
"$API" GET /health | json_pretty
"$API" GET /runtime/health | json_pretty

echo
echo "== runtime pipeline =="
PIPELINE_BODY="$(cat <<EOF
{"universe_scope":"main-board","max_candidates":$MAX_CANDIDATES,"auto_trade":false,"account_id":"$ACCOUNT_ID"}
EOF
)"
PIPELINE_RESULT="$("$API" POST /runtime/jobs/pipeline "$PIPELINE_BODY")"
printf '%s\n' "$PIPELINE_RESULT" | print_summary pipeline

TRADE_DATE="$(printf '%s\n' "$PIPELINE_RESULT" | extract_trade_date)"
if [[ -z "$TRADE_DATE" ]]; then
  echo "Unable to determine trade_date from runtime pipeline result." >&2
  exit 1
fi

echo
echo "== execution precheck =="
PRECHECK_RESULT="$("$API" GET "/system/discussions/execution-precheck?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}")"
printf '%s\n' "$PRECHECK_RESULT" | print_summary precheck

echo
echo "== execution intents =="
INTENTS_RESULT="$("$API" GET "/system/discussions/execution-intents?trade_date=${TRADE_DATE}&account_id=${ACCOUNT_ID}")"
printf '%s\n' "$INTENTS_RESULT" | print_summary intents

INTENT_COUNT="$(printf '%s\n' "$INTENTS_RESULT" | extract_intent_count)"
if [[ "$INTENT_COUNT" -eq 0 ]]; then
  echo
  echo "== preview dispatch =="
  echo "No execution intents available for preview."
else
  INTENT_IDS_JSON="$(printf '%s\n' "$INTENTS_RESULT" | extract_intent_ids_json)"
  DISPATCH_BODY="$(cat <<EOF
{"trade_date":"$TRADE_DATE","account_id":"$ACCOUNT_ID","intent_ids":$INTENT_IDS_JSON,"apply":false}
EOF
)"
  echo
  echo "== preview dispatch =="
  "$API" POST /system/discussions/execution-intents/dispatch "$DISPATCH_BODY" | print_summary dispatch
fi

echo
echo "== latest execution dispatch =="
"$API" GET "/system/discussions/execution-dispatch/latest?trade_date=${TRADE_DATE}" | print_summary dispatch

echo
echo "== client brief =="
"$API" GET "/system/discussions/client-brief?trade_date=${TRADE_DATE}" | print_summary client_brief

echo
echo "== final brief =="
"$API" GET "/system/discussions/final-brief?trade_date=${TRADE_DATE}" | print_summary final_brief
