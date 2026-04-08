#!/usr/bin/env bash
# ashare-system-v2 AI 模型训练
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

mkdir -p logs

echo "[ashare-v2] 开始模型训练..."
exec .venv/Scripts/python.exe -c "
from ashare_system.ai.xgb_scorer import XGBScorer
from ashare_system.ai.registry import model_registry
from ashare_system.ai.contracts import ModelVersion
from ashare_system.logging_config import setup_logging, get_logger
from pathlib import Path
import pandas as pd
import numpy as np

setup_logging(Path('logs'))
logger = get_logger('scripts.train_models')

# 生成模拟训练数据
np.random.seed(42)
n = 500
X = pd.DataFrame(np.random.randn(n, 20), columns=[f'factor_{i}' for i in range(20)])
y = pd.Series((X.iloc[:, 0] + X.iloc[:, 1] + np.random.randn(n) * 0.5 > 0).astype(int))

logger.info('训练数据: %d 样本, %d 特征', n, X.shape[1])

scorer = XGBScorer()
metrics = scorer.train(X, y)
logger.info('XGBoost 训练完成: AUC=%.3f, Acc=%.3f', metrics.auc, metrics.accuracy)

version = ModelVersion(name='xgb_scorer', version='1.0', metrics=metrics)
model_registry.register(version)
logger.info('模型已注册: xgb_scorer v1.0')
print(f'训练完成: AUC={metrics.auc:.3f}, Accuracy={metrics.accuracy:.3f}')
" "$@"
