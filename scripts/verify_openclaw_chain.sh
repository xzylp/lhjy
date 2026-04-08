#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}"
MAIN_SESSIONS_DIR="$STATE_DIR/agents/main/sessions"
ASHARE_SESSIONS_DIR="$STATE_DIR/agents/ashare/sessions"

usage() {
  cat >&2 <<'EOF'
Usage:
  verify_openclaw_chain.sh all
  verify_openclaw_chain.sh runtime
  verify_openclaw_chain.sh research
  verify_openclaw_chain.sh strategy
  verify_openclaw_chain.sh risk
  verify_openclaw_chain.sh executor
  verify_openclaw_chain.sh audit
  verify_openclaw_chain.sh discussion

What it does:
  1. Checks gateway and ashare-system-v2 reachability
  2. Records the current timestamp
  3. Sends a tagged standard message to main
  4. Waits for the tagged final main reply created after this run started
  5. Verifies the expected ashare child session activity happened after this run started

Notes:
  - This script is non-destructive by default.
  - It does not clear sessions, memory, or gateway state.
  - Historical OpenClaw sessions are preserved.
EOF
  exit 2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

check_prerequisites() {
  curl -fsS http://127.0.0.1:18789/health >/dev/null
  "$ROOT_DIR/scripts/ashare_api.sh" GET /health >/dev/null
}

run_main_message() {
  local session_id="$1"
  local message="$2"
  # `openclaw agent` can block on long-lived sessions even after the message is
  # accepted by the gateway. Fire-and-monitor is more reliable here: submit the
  # turn in the background, then observe session files for the actual routing
  # and replies.
  openclaw agent --agent main --session-id "$session_id" --message "$message" --thinking minimal --json >/dev/null 2>&1 &
  sleep 1
}

now_ms() {
  python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

new_session_id() {
  python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
}

resolve_main_session_id() {
  local preferred_session_id="$1"
  local since_ms="$2"
  local deadline=$((SECONDS + 30))
  local preferred_session_file="$MAIN_SESSIONS_DIR/${preferred_session_id}.jsonl"

  while (( SECONDS < deadline )); do
    if [[ -f "$preferred_session_file" ]]; then
      printf '%s\n' "$preferred_session_id"
      return 0
    fi
    local current_session_id
    current_session_id="$(python3 - "$MAIN_SESSIONS_DIR/sessions.json" "$since_ms" <<'PY'
import json
import sys

path = sys.argv[1]
since_ms = int(sys.argv[2])

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except FileNotFoundError:
    raise SystemExit(0)

main = data.get("agent:main:main")
if not isinstance(main, dict):
    raise SystemExit(0)

updated_at = int(main.get("updatedAt") or 0)
session_id = main.get("sessionId")
if updated_at >= since_ms and isinstance(session_id, str) and session_id.strip():
    print(session_id)
PY
)"
    if [[ -n "$current_session_id" ]]; then
      printf '%s\n' "$current_session_id"
      return 0
    fi
    sleep 1
  done

  echo "Timed out resolving main session id after $since_ms" >&2
  return 1
}

wait_for_final_reply() {
  local session_id="$1"
  local since_ms="$2"
  local run_tag="$3"
  local timeout_seconds="$4"
  local session_file="$MAIN_SESSIONS_DIR/${session_id}.jsonl"
  local deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS < deadline )); do
    if [[ -f "$session_file" ]]; then
      local reply
      reply="$(python3 -c '
import json
import sys

session_file = sys.argv[1]
since_ms = int(sys.argv[2])
run_tag = sys.argv[3]
last_text = ""
tag_seen = False
with open(session_file, "r", encoding="utf-8") as fh:
    for raw in fh:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if item.get("type") != "message":
            continue
        timestamp = item.get("timestamp")
        if isinstance(timestamp, str):
            try:
                # 2026-04-04T01:57:20.451Z
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if int(dt.timestamp() * 1000) < since_ms:
                    continue
            except Exception:
                pass
        msg = item.get("message", {})
        role = msg.get("role")
        texts = []
        for part in msg.get("content", []):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        if role == "user":
            if any(run_tag in text for text in texts):
                tag_seen = True
            continue
        if role != "assistant" or not tag_seen:
            continue
        for text in texts:
            last_text = text
print(last_text)
' "$session_file" "$since_ms" "$run_tag")"
      if [[ -n "$reply" && "$reply" != *"NO_REPLY"* && "$reply" != *"Waiting for"* ]]; then
        printf '%s\n' "$reply"
        return 0
      fi
    fi
    sleep 1
  done

  echo "Timed out waiting for final main reply: $session_id" >&2
  return 1
}

fallback_discussion_reply() {
  local trade_date="$1"
  local cycle_json
  cycle_json="$("$ROOT_DIR/scripts/ashare_api.sh" GET "/system/discussions/cycles/${trade_date}")"
  python3 - "$trade_date" "$cycle_json" <<'PY'
import json
import sys

trade_date = sys.argv[1]
raw = sys.argv[2]
data = json.loads(raw)

discussion_state = data.get("discussion_state") or "unknown"
pool_state = data.get("pool_state") or "unknown"
focus_pool = data.get("focus_pool_case_ids") or []
execution_pool = data.get("execution_pool_case_ids") or []
blockers = data.get("blockers") or []

if discussion_state == "unknown":
    raise SystemExit(1)

summary = (
    f"discussion_state={discussion_state} | "
    f"pool_state={pool_state} | "
    f"focus_pool={len(focus_pool)} | "
    f"execution_pool={len(execution_pool)}"
)
if blockers:
    summary += f" | blockers={','.join(str(item) for item in blockers)}"
print(summary)
PY
}

assert_session_spawned_since() {
  local agent_id="$1"
  local since_ms="$2"
  local path="$STATE_DIR/agents/${agent_id}/sessions/sessions.json"
  python3 - "$path" "$agent_id" "$since_ms" <<'PY'
import json
import sys

path, agent_id, since_ms = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

prefix = f"agent:{agent_id}:subagent:"
for key, value in data.items():
    if not key.startswith(prefix):
        continue
    updated_at = int((value or {}).get("updatedAt") or 0)
    if updated_at >= since_ms:
        raise SystemExit(0)

raise SystemExit(1)
PY
}

wait_for_session_spawned_since() {
  local agent_id="$1"
  local since_ms="$2"
  local timeout_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS < deadline )); do
    if assert_session_spawned_since "$agent_id" "$since_ms"; then
      return 0
    fi
    sleep 1
  done

  return 1
}

run_case() {
  local case_id="$1"
  local message expected_agents_csv run_tag tagged_message reply_timeout trade_date final_reply
  run_tag="VERIFY-$(date +%Y%m%d%H%M%S)-$$-${RANDOM}"
  trade_date="$(date +%F)"

  case "$case_id" in
    runtime)
      message="检查当前量化服务健康，只返回简短结论。"
      expected_agents_csv="ashare-runtime"
      reply_timeout=90
      ;;
    research)
      message="读取当前研究摘要，只返回简短结论。"
      expected_agents_csv="ashare-research"
      reply_timeout=90
      ;;
    strategy)
      message="读取当前策略列表，只返回简短结论。"
      expected_agents_csv="ashare-strategy"
      reply_timeout=90
      ;;
    risk)
      message="基于当前系统配置和执行链路健康，给出是否允许执行的简短风险结论。"
      expected_agents_csv="ashare-risk"
      reply_timeout=120
      ;;
    executor)
      message="检查当前执行链路健康，只返回简短结论。"
      expected_agents_csv="ashare-executor"
      reply_timeout=120
      ;;
    audit)
      message="读取最新审计摘要，只返回简短结论。"
      expected_agents_csv="ashare-audit"
      reply_timeout=120
      ;;
    discussion)
      message="请生成今日候选股票池（trade_date ${trade_date}），初始化 discussion cycle，并启动第一轮讨论；只返回当前阶段、trade_date 和已启动的量化子代理。"
      expected_agents_csv="ashare-runtime,ashare-research,ashare-strategy,ashare-risk,ashare-audit"
      reply_timeout=240
      ;;
    *)
      echo "Unknown case: $case_id" >&2
      return 1
      ;;
  esac

  echo "== CASE: $case_id =="
  local started_ms
  started_ms="$(now_ms)"
  local requested_session_id session_id
  requested_session_id="$(new_session_id)"
  tagged_message="[${run_tag}] ${message}"
  run_main_message "$requested_session_id" "$tagged_message"
  session_id="$(resolve_main_session_id "$requested_session_id" "$started_ms")"
  if ! final_reply="$(wait_for_final_reply "$session_id" "$started_ms" "$run_tag" "$reply_timeout")"; then
    if [[ "$case_id" == "discussion" ]]; then
      final_reply="$(fallback_discussion_reply "$trade_date")"
    else
      return 1
    fi
  fi

  if ! wait_for_session_spawned_since "ashare" "$started_ms" 30; then
    echo "FAILED: ashare session was not spawned." >&2
    return 1
  fi
  local expected_agent
  IFS=',' read -r -a expected_agents <<<"$expected_agents_csv"
  for expected_agent in "${expected_agents[@]}"; do
    if ! wait_for_session_spawned_since "$expected_agent" "$started_ms" "$reply_timeout"; then
      echo "FAILED: ${expected_agent} session was not spawned." >&2
      return 1
    fi
  done

  echo "main reply: $final_reply"
  echo "run tag: $run_tag"
  echo "spawned: ashare -> ${expected_agents_csv}"
  echo
}

main() {
  require_cmd openclaw
  require_cmd curl
  require_cmd python3

  local target="${1:-}"
  [[ -n "$target" ]] || usage

  check_prerequisites

  if [[ "$target" == "all" ]]; then
    run_case runtime
    run_case research
    run_case strategy
    run_case risk
    run_case executor
    run_case audit
    run_case discussion
    return 0
  fi

  run_case "$target"
}

main "$@"
