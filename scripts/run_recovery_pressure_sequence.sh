#!/usr/bin/env bash
# 服务重启 / 断链恢复压测编排脚本
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

TRADE_DATE="$(date +%F)"
ACCOUNT_ID="${ASHARE_ACCOUNT_ID:-8890130545}"
OUTPUT_ROOT="${ASHARE_RECOVERY_EVIDENCE_ROOT:-$PROJECT_DIR/logs/recovery_evidence}"
MODE="dry-run"
COMPONENTS="control-plane,scheduler,feishu,openclaw,hermes,windows-bridge"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_ROOT=""
OPENCLAW_BIN="$(resolve_openclaw_bin || true)"
HERMES_BIN="${HERMES_BIN:-/home/yxz/.hermes/hermes-agent/venv/bin/hermes}"
HERMES_PROFILE="${HERMES_PROFILE:-ashare-backup}"

usage() {
    cat <<'EOF'
用法:
  bash scripts/run_recovery_pressure_sequence.sh [options]

说明:
  默认仅执行 dry-run：做前置检查、输出动作清单，并为每个阶段收集前后证据。
  只有显式传入 --execute 时，才会实际调用 Linux 侧服务重启脚本。

选项:
  --trade-date YYYY-MM-DD
  --account-id ACCOUNT_ID
  --components LIST           逗号分隔，默认 control-plane,scheduler,feishu,openclaw,hermes,windows-bridge
  --output-root PATH
  --run-id LABEL
  --execute                   实际执行 Linux 侧 restart
  --dry-run                   仅留档和打印步骤（默认）
  -h, --help

组件说明:
  control-plane   Linux control plane service
  scheduler       Linux scheduler service
  feishu          飞书长连接 service
  openclaw        OpenClaw gateway 主链验证
  hermes          Hermes backup 备链验证
  windows-bridge  Windows 执行桥恢复留档（仅人工步骤）
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trade-date)
            TRADE_DATE="${2:-}"
            shift 2
            ;;
        --account-id)
            ACCOUNT_ID="${2:-}"
            shift 2
            ;;
        --components)
            COMPONENTS="${2:-}"
            shift 2
            ;;
        --output-root)
            OUTPUT_ROOT="${2:-}"
            shift 2
            ;;
        --run-id)
            RUN_ID="${2:-}"
            shift 2
            ;;
        --execute)
            MODE="execute"
            shift
            ;;
        --dry-run)
            MODE="dry-run"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[recovery-sequence] 未知参数: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

mkdir -p "${OUTPUT_ROOT}"
RUN_ROOT="${OUTPUT_ROOT}/${RUN_ID}_sequence"
mkdir -p "${RUN_ROOT}"
LOG_FILE="${RUN_ROOT}/run.log"
PLAN_FILE="${RUN_ROOT}/plan.txt"

log() {
    local message="$1"
    printf '%s %s\n' "[$(date +%F' '%T)]" "${message}" | tee -a "${LOG_FILE}" >&2
}

append_plan() {
    printf '%s\n' "$1" >> "${PLAN_FILE}"
}

collect_tag() {
    local tag="$1"
    log "[evidence] 采集 ${tag}"
    bash "$SCRIPT_DIR/collect_recovery_evidence.sh" \
        --trade-date "${TRADE_DATE}" \
        --account-id "${ACCOUNT_ID}" \
        --tag "${RUN_ID}_${tag}" \
        --output-root "${OUTPUT_ROOT}" >> "${LOG_FILE}" 2>&1
}

run_or_note() {
    local label="$1"
    shift
    if [[ "${MODE}" == "execute" ]]; then
        log "[execute] ${label}: $*"
        "$@" >> "${LOG_FILE}" 2>&1
        return 0
    fi
    log "[dry-run] ${label}: $*"
    return 0
}

manual_step() {
    local label="$1"
    local detail="$2"
    log "[manual] ${label}: ${detail}"
    append_plan "- ${label}: ${detail}"
}

verify_openclaw_status() {
    if [[ -z "${OPENCLAW_BIN}" ]]; then
        manual_step "OpenClaw" "未找到 openclaw 可执行文件，需人工确认 gateway 状态。"
        return 0
    fi
    run_or_note "OpenClaw gateway status" "${OPENCLAW_BIN}" gateway status
}

verify_hermes_status() {
    if [[ ! -x "${HERMES_BIN}" ]]; then
        manual_step "Hermes" "未找到 Hermes 可执行文件，需人工确认 backup gateway 状态。"
        return 0
    fi
    run_or_note "Hermes gateway status" "${HERMES_BIN}" -p "${HERMES_PROFILE}" gateway status
}

run_step() {
    local component="$1"
    case "${component}" in
        control-plane)
            append_plan "## control-plane"
            append_plan "1. 采集 before_control_plane_restart"
            append_plan "2. ${MODE} 调用 scripts/ashare_service.sh restart"
            append_plan "3. 采集 after_control_plane_restart"
            collect_tag "before_control_plane_restart"
            run_or_note "control-plane restart" bash "$SCRIPT_DIR/ashare_service.sh" restart
            collect_tag "after_control_plane_restart"
            ;;
        scheduler)
            append_plan "## scheduler"
            append_plan "1. 采集 before_scheduler_restart"
            append_plan "2. ${MODE} 调用 scripts/ashare_scheduler_service.sh restart"
            append_plan "3. 采集 after_scheduler_restart"
            collect_tag "before_scheduler_restart"
            run_or_note "scheduler restart" bash "$SCRIPT_DIR/ashare_scheduler_service.sh" restart
            collect_tag "after_scheduler_restart"
            ;;
        feishu)
            append_plan "## feishu"
            append_plan "1. 采集 before_feishu_restart"
            append_plan "2. ${MODE} 调用 scripts/ashare_feishu_longconn_ctl.sh --user restart"
            append_plan "3. 等待 /system/feishu/longconn/status 回到 connected + is_fresh=true"
            append_plan "4. 采集 after_feishu_restart"
            collect_tag "before_feishu_restart"
            run_or_note "feishu longconn restart" bash "$SCRIPT_DIR/ashare_feishu_longconn_ctl.sh" --user restart
            collect_tag "after_feishu_restart"
            ;;
        openclaw)
            append_plan "## openclaw"
            append_plan "1. 采集 before_openclaw_verify"
            append_plan "2. ${MODE} 校验 openclaw gateway status"
            append_plan "3. 采集 after_openclaw_verify"
            collect_tag "before_openclaw_verify"
            verify_openclaw_status
            collect_tag "after_openclaw_verify"
            ;;
        hermes)
            append_plan "## hermes"
            append_plan "1. 采集 before_hermes_verify"
            append_plan "2. ${MODE} 校验 Hermes backup gateway status"
            append_plan "3. 采集 after_hermes_verify"
            collect_tag "before_hermes_verify"
            verify_hermes_status
            collect_tag "after_hermes_verify"
            ;;
        windows-bridge)
            append_plan "## windows-bridge"
            append_plan "1. 采集 before_bridge_recovery"
            append_plan "2. 人工在 Windows 侧重启执行桥或恢复 QMT 链路"
            append_plan "3. 采集 after_bridge_recovery"
            collect_tag "before_bridge_recovery"
            manual_step "Windows 执行桥" "请在 Windows 侧重启 gateway/QMT 后，再复跑本脚本或继续收集 after_bridge_recovery 证据。"
            collect_tag "after_bridge_recovery"
            ;;
        *)
            log "[skip] 未识别组件 ${component}"
            ;;
    esac
}

append_plan "# 服务重启 / 断链恢复压测编排"
append_plan "run_id=${RUN_ID}"
append_plan "mode=${MODE}"
append_plan "trade_date=${TRADE_DATE}"
append_plan "account_id=${ACCOUNT_ID}"
append_plan "components=${COMPONENTS}"

log "[start] run_id=${RUN_ID} mode=${MODE} trade_date=${TRADE_DATE} account_id=${ACCOUNT_ID}"
log "[check] service-recovery-readiness"
bash "$SCRIPT_DIR/check_service_recovery_readiness.sh" "${TRADE_DATE}" "${ACCOUNT_ID}" >> "${LOG_FILE}" 2>&1 || true
log "[check] controlled-apply-readiness"
bash "$SCRIPT_DIR/check_apply_pressure_readiness.sh" "${TRADE_DATE}" "${ACCOUNT_ID}" >> "${LOG_FILE}" 2>&1 || true

IFS=',' read -r -a component_list <<< "${COMPONENTS}"
for raw_component in "${component_list[@]}"; do
    component="$(printf '%s' "${raw_component}" | xargs)"
    if [[ -z "${component}" ]]; then
        continue
    fi
    log "[step] ${component}"
    run_step "${component}"
done

cat > "${RUN_ROOT}/README.txt" <<EOF
服务重启 / 断链恢复压测编排结果
run_id=${RUN_ID}
mode=${MODE}
trade_date=${TRADE_DATE}
account_id=${ACCOUNT_ID}
components=${COMPONENTS}

说明:
- run.log 保存本次编排执行日志
- plan.txt 保存阶段步骤清单
- 证据目录位于 ${OUTPUT_ROOT}/<timestamp>_<tag>/
- 默认 dry-run 不会真正重启 Linux 服务，只做留档和命令提示
EOF

log "[done] run_root=${RUN_ROOT}"
echo "[recovery-sequence] 完成，结果目录: ${RUN_ROOT}"
