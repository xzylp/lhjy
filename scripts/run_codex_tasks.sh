#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

TASK_FILE=""
LOG_ROOT="$PROJECT_DIR/logs/codex"
MODEL=""
PROFILE=""
SANDBOX_MODE="workspace-write"
CHAIN_MODE="resume-last"
CONTINUE_ON_ERROR=0
ENABLE_SEARCH=0
DANGEROUS_MODE=0

usage() {
    cat <<'EOF'
Usage:
  bash scripts/run_codex_tasks.sh --task-file <file> [options]

Options:
  --task-file <file>         Task file path. Supports multi-line tasks separated by a line with ---
  --log-root <dir>           Log root directory. Default: <project>/logs/codex
  --model <name>             Optional Codex model override
  --profile <name>           Optional Codex profile
  --sandbox <mode>           read-only | workspace-write | danger-full-access
  --fresh-each-task          Do not resume previous task context
  --continue-on-error        Continue with next task when one task fails
  --search                   Enable Codex live web search
  --danger-full-access       Bypass approvals and sandboxing. Use only in isolated environments
  -h, --help                 Show this help

Task file format:
  - Blank lines are allowed
  - Lines starting with # before a task block are ignored
  - Use a line with exactly --- to end the current task block
EOF
}

log() {
    local message="$1"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '[%s] %s\n' "$ts" "$message" | tee -a "$RUNNER_LOG"
}

resolve_path() {
    local path="$1"
    if [[ "$path" = /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s\n' "$PROJECT_DIR/$path"
    fi
}

has_non_whitespace() {
    local value="$1"
    [[ -n "${value//[$' \t\r\n']/}" ]]
}

flush_task_block() {
    if ! has_non_whitespace "$CURRENT_TASK"; then
        CURRENT_TASK=""
        return
    fi

    TASK_COUNT=$((TASK_COUNT + 1))
    local task_path
    task_path="$TASKS_DIR/task-$(printf '%03d' "$TASK_COUNT").md"
    printf '%s' "$CURRENT_TASK" > "$task_path"
    TASK_FILES+=("$task_path")
    CURRENT_TASK=""
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-file)
            TASK_FILE="${2:-}"
            shift 2
            ;;
        --log-root)
            LOG_ROOT="${2:-}"
            shift 2
            ;;
        --model)
            MODEL="${2:-}"
            shift 2
            ;;
        --profile)
            PROFILE="${2:-}"
            shift 2
            ;;
        --sandbox)
            SANDBOX_MODE="${2:-}"
            shift 2
            ;;
        --fresh-each-task)
            CHAIN_MODE="fresh"
            shift
            ;;
        --continue-on-error)
            CONTINUE_ON_ERROR=1
            shift
            ;;
        --search)
            ENABLE_SEARCH=1
            shift
            ;;
        --danger-full-access)
            DANGEROUS_MODE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$TASK_FILE" ]]; then
    printf 'Missing required option: --task-file\n' >&2
    usage >&2
    exit 1
fi

TASK_FILE="$(resolve_path "$TASK_FILE")"
LOG_ROOT="$(resolve_path "$LOG_ROOT")"

if [[ ! -f "$TASK_FILE" ]]; then
    printf 'Task file not found: %s\n' "$TASK_FILE" >&2
    exit 1
fi

mkdir -p "$LOG_ROOT"
RUN_ID="$(date '+%Y%m%d-%H%M%S')"
RUN_DIR="$LOG_ROOT/$RUN_ID"
TASKS_DIR="$RUN_DIR/tasks"
mkdir -p "$RUN_DIR" "$TASKS_DIR"

RUNNER_LOG="$RUN_DIR/runner.log"
SUMMARY_FILE="$RUN_DIR/summary.tsv"
printf 'task\tstatus\texit_code\tprompt_file\tjson_file\tlast_message_file\n' > "$SUMMARY_FILE"

CURRENT_TASK=""
TASK_COUNT=0
TASK_FILES=()

while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "---" ]]; then
        flush_task_block
        continue
    fi

    if ! has_non_whitespace "$CURRENT_TASK" && [[ "$line" =~ ^[[:space:]]*# ]]; then
        continue
    fi

    CURRENT_TASK+="$line"$'\n'
done < "$TASK_FILE"
flush_task_block

if [[ "${#TASK_FILES[@]}" -eq 0 ]]; then
    printf 'No tasks found in: %s\n' "$TASK_FILE" >&2
    exit 1
fi

build_prefix() {
    CMD_PREFIX=(codex -C "$PROJECT_DIR")

    if [[ "$DANGEROUS_MODE" -eq 1 ]]; then
        CMD_PREFIX+=(--dangerously-bypass-approvals-and-sandbox)
    else
        CMD_PREFIX+=(-a never -s "$SANDBOX_MODE")
    fi

    if [[ -n "$MODEL" ]]; then
        CMD_PREFIX+=(-m "$MODEL")
    fi

    if [[ -n "$PROFILE" ]]; then
        CMD_PREFIX+=(-p "$PROFILE")
    fi

    if [[ "$ENABLE_SEARCH" -eq 1 ]]; then
        CMD_PREFIX+=(--search)
    fi
}

run_task() {
    local index="$1"
    local prompt_file="$2"
    local json_file="$RUN_DIR/task-$(printf '%03d' "$index").jsonl"
    local last_file="$RUN_DIR/task-$(printf '%03d' "$index").last.txt"
    local mode_label="new"
    local -a cmd

    build_prefix
    if [[ "$index" -gt 1 && "$CHAIN_MODE" == "resume-last" ]]; then
        mode_label="resume-last"
        cmd=("${CMD_PREFIX[@]}" exec resume --last --skip-git-repo-check --json -o "$last_file" -)
    else
        cmd=("${CMD_PREFIX[@]}" exec --skip-git-repo-check --json -o "$last_file" -)
    fi

    log "Task $(printf '%03d' "$index") start mode=$mode_label prompt=$(basename "$prompt_file")"

    set +e
    cat "$prompt_file" | "${cmd[@]}" | tee "$json_file"
    local exit_code=${PIPESTATUS[1]}
    set -e

    local status="ok"
    if [[ "$exit_code" -ne 0 ]]; then
        status="failed"
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$(printf '%03d' "$index")" \
        "$status" \
        "$exit_code" \
        "$prompt_file" \
        "$json_file" \
        "$last_file" >> "$SUMMARY_FILE"

    if [[ "$status" == "ok" ]]; then
        log "Task $(printf '%03d' "$index") finished exit_code=$exit_code"
        return 0
    fi

    log "Task $(printf '%03d' "$index") failed exit_code=$exit_code"
    return "$exit_code"
}

trap 'log "Interrupted"; exit 130' INT TERM

log "Run directory: $RUN_DIR"
log "Project dir: $PROJECT_DIR"
log "Task file: $TASK_FILE"
log "Task count: ${#TASK_FILES[@]}"
log "Chain mode: $CHAIN_MODE"

for task_file in "${TASK_FILES[@]}"; do
    task_index="${task_file##*-}"
    task_index="${task_index%.md}"
    run_task "$task_index" "$task_file" || {
        if [[ "$CONTINUE_ON_ERROR" -eq 1 ]]; then
            log "Continue on error enabled, moving to next task"
            continue
        fi
        log "Stopping on first failure"
        exit 1
    }
done

log "All tasks processed"
log "Summary file: $SUMMARY_FILE"
