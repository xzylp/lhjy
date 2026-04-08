#!/usr/bin/env bash
# ashare-system-v2 盘后日常 pipeline
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

mkdir -p logs

echo "[ashare-v2] 盘后日常 pipeline 开始..."

echo "[1/4] 因子计算..."
bash "$SCRIPT_DIR/compute_factors.sh"

echo "[2/4] 健康检查..."
bash "$SCRIPT_DIR/health_check.sh" || true

echo "[3/4] 生成日终报告..."
.venv/Scripts/python.exe -c "
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

echo "[4/4] 盘后 pipeline 完成"
