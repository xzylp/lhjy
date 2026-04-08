#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}"
TMP_ROOT="$STATE_DIR/tmp"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="$STATE_DIR/reset-backups/${STAMP}-main-ashare-reset"

mkdir -p "$TMP_ROOT" "$BACKUP_ROOT/sessions" "$BACKUP_ROOT/memory"
export TMPDIR="$TMP_ROOT"

agents=(
  main
  ashare
  ashare-runtime
  ashare-research
  ashare-strategy
  ashare-risk
  ashare-executor
  ashare-audit
)

memory_scopes=(
  agent:main
  agent:ashare
  agent:ashare-runtime
  agent:ashare-research
  agent:ashare-strategy
  agent:ashare-risk
  agent:ashare-executor
  agent:ashare-audit
)

backup_and_reset_sessions() {
  local agent="$1"
  local dir="$STATE_DIR/agents/$agent/sessions"
  if [[ ! -d "$dir" ]]; then
    return 0
  fi

  cp -a "$dir" "$BACKUP_ROOT/sessions/${agent}-sessions"
  rm -rf "$dir"
  mkdir -p "$dir"
  printf '{}\n' > "$dir/sessions.json"
}

export_memory_scope() {
  local scope="$1"
  local outfile="$BACKUP_ROOT/memory/${scope//:/_}.json"
  openclaw memory-pro export --scope "$scope" --output "$outfile" >/dev/null 2>&1 || true
}

delete_memory_scope() {
  local scope="$1"
  openclaw memory-pro delete-bulk --scope "$scope" >/dev/null 2>&1 || true
}

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop openclaw-gateway.service >/dev/null 2>&1 || true
  systemctl --user disable openclaw-gateway.service >/dev/null 2>&1 || true
fi

for agent in "${agents[@]}"; do
  backup_and_reset_sessions "$agent"
done

for scope in "${memory_scopes[@]}"; do
  export_memory_scope "$scope"
  delete_memory_scope "$scope"
done

echo "OpenClaw main/ashare reset completed."
echo "Backup: $BACKUP_ROOT"
echo "Next:"
echo "  1. Windows: scripts/start_unattended.ps1"
echo "  2. WSL: ./scripts/start_openclaw_gateway.sh"
echo "  3. WSL: ./scripts/ashare_api.sh probe"
