#!/usr/bin/env bash
# ashare-system-v2 Linux control plane 一键启动
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"

# 加载 .env
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

EXECUTION_PLANE="${ASHARE_EXECUTION_PLANE:-windows_gateway}"
PYTHON_BIN="$(resolve_project_python || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[ashare-v2] 未找到可用 Python 解释器，请先创建 .venv 或设置 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

mkdir -p logs

echo "[ashare-v2] 清理超过 ${ASHARE_RUNTIME_RETENTION_DAYS:-7} 天的日志与临时文件..."
bash "$SCRIPT_DIR/cleanup_runtime_files.sh" || true
rotate_log_file_if_needed "${PROJECT_DIR}/logs/startup.log" "${ASHARE_ROTATE_STARTUP_LOG_MB:-50}"

echo "[ashare-v2] 启动中..."
echo "[ashare-v2] 模式: ${ASHARE_RUN_MODE:-dry-run} / 执行: ${ASHARE_EXECUTION_MODE:-mock} / 执行平面: ${EXECUTION_PLANE}"
echo "[ashare-v2] Python: ${PYTHON_BIN}"
if [ "$EXECUTION_PLANE" = "windows_gateway" ]; then
    echo "[ashare-v2] Linux 侧仅为 control plane，QMT 下单写口位于 Windows Execution Gateway"
fi

# 日志输出到文件
exec "${PYTHON_BIN}" -m ashare_system.run serve "$@" 2>&1 | tee -a logs/startup.log
