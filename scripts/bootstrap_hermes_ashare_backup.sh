#!/usr/bin/env bash
set -euo pipefail

HERMES_BIN="${HERMES_BIN:-/home/yxz/.hermes/hermes-agent/venv/bin/hermes}"
PROFILE="${PROFILE:-ashare-backup}"
REPO_ROOT="${REPO_ROOT:-/srv/projects/ashare-system-v2}"
PROMPTS_DIR="${REPO_ROOT}/hermes/prompts"
DELIVER_TARGET="${HERMES_CRON_DELIVER:-local}"

job_exists() {
  local name="$1"
  "${HERMES_BIN}" -p "${PROFILE}" cron list 2>/dev/null | grep -Fq "${name}"
}

create_job() {
  local name="$1"
  local schedule="$2"
  local prompt_file="$3"

  if job_exists "${name}"; then
    echo "[skip] ${name} 已存在"
    return 0
  fi

  local prompt
  prompt="$(cat "${prompt_file}")"

  "${HERMES_BIN}" -p "${PROFILE}" cron create "${schedule}" "${prompt}" \
    --name "${name}" \
    --deliver "${DELIVER_TARGET}"

  echo "[ok] ${name} -> ${schedule}"
}

create_job "ashare-preopen-readiness" "15 9 * * 1-5" "${PROMPTS_DIR}/cron_preopen_readiness.md"
create_job "ashare-intraday-watch-am" "*/5 9-11 * * 1-5" "${PROMPTS_DIR}/cron_intraday_watch.md"
create_job "ashare-intraday-watch-pm" "*/5 13-15 * * 1-5" "${PROMPTS_DIR}/cron_intraday_watch.md"
create_job "ashare-position-watch-am" "*/10 9-11 * * 1-5" "${PROMPTS_DIR}/cron_position_watch.md"
create_job "ashare-position-watch-pm" "*/10 13-15 * * 1-5" "${PROMPTS_DIR}/cron_position_watch.md"
create_job "ashare-postclose-learning" "35 15 * * *" "${PROMPTS_DIR}/cron_postclose_learning.md"
create_job "ashare-nightly-sandbox" "0 23 * * *" "${PROMPTS_DIR}/cron_nightly_sandbox.md"

echo
echo "ashare-backup cron bootstrap 完成。"
echo "若要让任务自动触发，需要启动该 profile 的 gateway："
echo "  ${HERMES_BIN} -p ${PROFILE} gateway install"
echo "  ${HERMES_BIN} -p ${PROFILE} gateway start"
