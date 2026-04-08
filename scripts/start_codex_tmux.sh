#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SESSION_NAME="codex-queue"
TASK_FILE=""
EXTRA_ARGS=()

usage() {
    cat <<'EOF'
Usage:
  bash scripts/start_codex_tmux.sh --task-file <file> [options passed through]

Options:
  --task-file <file>         Task file path
  --session-name <name>      tmux session name. Default: codex-queue
  -h, --help                 Show this help

Examples:
  bash scripts/start_codex_tmux.sh --task-file discuss/codex-quant-task-queue.txt
  bash scripts/start_codex_tmux.sh --task-file discuss/codex-quant-task-queue.txt --continue-on-error
EOF
}

resolve_path() {
    local path="$1"
    if [[ "$path" = /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s\n' "$PROJECT_DIR/$path"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-file)
            TASK_FILE="${2:-}"
            shift 2
            ;;
        --session-name)
            SESSION_NAME="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ -z "$TASK_FILE" ]]; then
    printf 'Missing required option: --task-file\n' >&2
    usage >&2
    exit 1
fi

TASK_FILE="$(resolve_path "$TASK_FILE")"

if [[ ! -f "$TASK_FILE" ]]; then
    printf 'Task file not found: %s\n' "$TASK_FILE" >&2
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    printf 'tmux not found in PATH\n' >&2
    exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    printf 'tmux session already exists: %s\n' "$SESSION_NAME" >&2
    printf 'Attach: tmux attach -t %s\n' "$SESSION_NAME"
    exit 1
fi

RUNNER_CMD="cd '$PROJECT_DIR' && bash 'scripts/run_codex_tasks.sh' --task-file '$TASK_FILE'"
for arg in "${EXTRA_ARGS[@]}"; do
    RUNNER_CMD+=" $(printf '%q' "$arg")"
done

tmux new-session -d -s "$SESSION_NAME" "$RUNNER_CMD"

printf 'Started tmux session: %s\n' "$SESSION_NAME"
printf 'Attach: tmux attach -t %s\n' "$SESSION_NAME"
printf 'Task file: %s\n' "$TASK_FILE"
