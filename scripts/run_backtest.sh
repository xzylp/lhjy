#!/usr/bin/env bash
# ashare-system-v2 执行回测
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

mkdir -p logs

echo "[ashare-v2] 开始回测..."
exec .venv/Scripts/python.exe -c "
from ashare_system.backtest.engine import BacktestEngine, BacktestConfig
from ashare_system.backtest.metrics import MetricsCalculator
from ashare_system.logging_config import setup_logging, get_logger
from pathlib import Path
import pandas as pd
import numpy as np

setup_logging(Path('logs'))
logger = get_logger('scripts.run_backtest')

config = BacktestConfig(initial_cash=1_000_000)
engine = BacktestEngine(config)

# 生成模拟信号和价格数据用于演示
np.random.seed(42)
dates = pd.date_range('2025-01-01', periods=60, freq='B')
symbols = ['600519.SH', '000001.SZ']

signals = pd.DataFrame(index=dates, columns=symbols, data='HOLD')
signals.iloc[5]['600519.SH'] = 'BUY'
signals.iloc[20]['600519.SH'] = 'SELL'

price_data = {}
for sym in symbols:
    close = 10.0 + np.cumsum(np.random.randn(60) * 0.2)
    price_data[sym] = pd.DataFrame({'close': close, 'open': close, 'high': close*1.01, 'low': close*0.99, 'volume': 100000}, index=dates.astype(str))

result = engine.run(signals, price_data)
m = result.metrics
logger.info('回测结果: 总收益=%.1f%%, 夏普=%.2f, 最大回撤=%.1f%%, 胜率=%.1f%%',
    m.total_return*100, m.sharpe_ratio, m.max_drawdown*100, m.win_rate*100)
print(f'总收益: {m.total_return:+.1%}')
print(f'夏普比率: {m.sharpe_ratio:.2f}')
print(f'最大回撤: {m.max_drawdown:.1%}')
print(f'胜率: {m.win_rate:.1%}')
" "$@"
