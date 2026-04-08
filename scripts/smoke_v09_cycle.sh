#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT_DIR/scripts/ashare_api.sh"
MAX_CANDIDATES="${1:-10}"

echo "== probe =="
"$API" probe

echo
echo "== runtime pipeline =="
PIPELINE_BODY="$(cat <<EOF
{"universe_scope":"main-board","max_candidates":$MAX_CANDIDATES,"auto_trade":false,"account_id":"8890130545"}
EOF
)"
PIPELINE_RESULT="$("$API" POST /runtime/jobs/pipeline "$PIPELINE_BODY")"
printf '%s\n' "$PIPELINE_RESULT"

TRADE_DATE="$(printf '%s\n' "$PIPELINE_RESULT" | python3 -c 'import json,sys; data=json.load(sys.stdin); print((data.get("generated_at") or "").split("T")[0])')"
if [[ -z "$TRADE_DATE" ]]; then
  echo "Unable to determine trade_date from runtime pipeline result." >&2
  exit 1
fi

echo
echo "== bootstrap cycle =="
"$API" POST /system/discussions/cycles/bootstrap "{\"trade_date\":\"$TRADE_DATE\"}"

echo
echo "== start round 1 =="
"$API" POST "/system/discussions/cycles/${TRADE_DATE}/rounds/1/start"

echo
echo "== cycle detail =="
"$API" GET "/system/discussions/cycles/${TRADE_DATE}"

echo
echo "== discussion summary =="
"$API" GET "/system/discussions/summary?trade_date=${TRADE_DATE}"
