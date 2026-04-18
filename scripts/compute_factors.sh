#!/usr/bin/env bash
# ashare-system-v2 因子批量计算
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

echo "[ashare-v2] 开始因子批量计算..."
echo "[ashare-v2] Python: ${PYTHON_BIN}"
exec "${PYTHON_BIN}" -c "
from ashare_system.factors import registry, FactorEngine
from ashare_system.container import get_market_adapter
from ashare_system.logging_config import setup_logging, get_logger
from pathlib import Path
import pandas as pd

setup_logging(Path('logs'))
logger = get_logger('scripts.compute_factors')

market = get_market_adapter()
universe = market.get_main_board_universe()
logger.info('股票池: %d 只', len(universe))

engine = FactorEngine()
logger.info('注册因子: %d 个', len(registry))

# 获取样本数据并计算因子
bars = market.get_bars(universe[:10], '1d')
if bars:
    df = pd.DataFrame([b.model_dump() for b in bars])
    results = engine.compute_category('technical', df, normalize=True)
    logger.info('技术因子计算完成: %d 个', len(results))
else:
    logger.warning('无行情数据，跳过因子计算')

logger.info('因子计算完成')
" "$@"
