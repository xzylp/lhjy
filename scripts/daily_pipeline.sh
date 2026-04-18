#!/usr/bin/env bash
# ashare-system-v2 盘后日常 pipeline
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/common_env.sh"

PYTHON_BIN="$(resolve_project_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[ashare-v2] 未找到可用 Python 解释器，请先创建 .venv 或设置 ASHARE_PYTHON_BIN" >&2
    exit 1
fi

mkdir -p logs

echo "[ashare-v2] 盘后日常 pipeline 开始..."

echo "[0/5] 清理超过 ${ASHARE_RUNTIME_RETENTION_DAYS:-7} 天的日志与临时文件..."
bash "$SCRIPT_DIR/cleanup_runtime_files.sh"

echo "[1/5] 因子计算..."
bash "$SCRIPT_DIR/compute_factors.sh"

echo "[2/5] 健康检查..."
bash "$SCRIPT_DIR/health_check.sh" || true

echo "[3/5] 生成日终报告..."
"${PYTHON_BIN}" -c "
from ashare_system.report.daily import DailyReporter, DailyReportData
from ashare_system.sentiment.calculator import SentimentCalculator
from ashare_system.sentiment.indicators import SentimentIndicators
from ashare_system.logging_config import setup_logging
from pathlib import Path
from datetime import date

setup_logging(Path('logs'))
calc = SentimentCalculator()
ind = SentimentIndicators(date=date.today().isoformat(), limit_up_count=50, limit_down_count=10, up_down_ratio=2.0, total_amount_billion=7000)
profile = calc.calc_daily(ind)
reporter = DailyReporter()
data = DailyReportData(date=date.today().isoformat(), profile=profile)
content = reporter.generate(data)
print('日终报告已生成')
"

echo "[5/5] 盘后 pipeline 完成"
