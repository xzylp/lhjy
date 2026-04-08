#!/usr/bin/env bash
# ashare-system-v2 Linux control plane 一键启动
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 加载 .env
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

EXECUTION_PLANE="${ASHARE_EXECUTION_PLANE:-windows_gateway}"

mkdir -p logs

echo "[ashare-v2] 启动中..."
echo "[ashare-v2] 模式: ${ASHARE_RUN_MODE:-dry-run} / 执行: ${ASHARE_EXECUTION_MODE:-mock} / 执行平面: ${EXECUTION_PLANE}"
if [ "$EXECUTION_PLANE" = "windows_gateway" ]; then
    echo "[ashare-v2] Linux 侧仅为 control plane，QMT 下单写口位于 Windows Execution Gateway"
fi

# 日志输出到文件
exec .venv/Scripts/python.exe -m ashare_system.run serve "$@" 2>&1 | tee -a logs/startup.log
