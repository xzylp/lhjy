#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT_DIR/scripts/ashare_api.sh"

usage() {
  cat <<'EOF'
Usage:
  smoke_discussion_flow.sh [max_candidates]

What it does:
  1. Probe ashare-system-v2 health
  2. Trigger one runtime pipeline
  3. Print today's candidate cases
  4. Print today's discussion summary

Notes:
  - This is a non-destructive smoke script.
  - It does not write discussion opinions or execute trades.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

MAX_CANDIDATES="${1:-10}"
TODAY="$(date +%F)"

echo "== probe =="
"$API" probe
"$API" GET /health

echo
echo "== runtime pipeline =="
PIPELINE_BODY="$(cat <<EOF
{"universe_scope":"main-board","max_candidates":$MAX_CANDIDATES,"auto_trade":false,"account_id":"8890130545"}
EOF
)"
"$API" POST /runtime/jobs/pipeline "$PIPELINE_BODY"

echo
echo "== candidate cases =="
"$API" GET "/system/cases?trade_date=${TODAY}&limit=${MAX_CANDIDATES}"

echo
echo "== discussion summary =="
"$API" GET "/system/discussions/summary?trade_date=${TODAY}"
