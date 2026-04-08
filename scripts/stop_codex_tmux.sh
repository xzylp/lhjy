#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${1:-codex-queue}"

if ! command -v tmux >/dev/null 2>&1; then
    printf 'tmux not found in PATH\n' >&2
    exit 1
fi

if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    printf 'tmux session not found: %s\n' "$SESSION_NAME" >&2
    exit 1
fi

tmux kill-session -t "$SESSION_NAME"
printf 'Stopped tmux session: %s\n' "$SESSION_NAME"
