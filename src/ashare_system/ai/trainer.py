"""通用训练框架"""

from __future__ import annotations

import pandas as pd
import numpy as np
from dataclasses import dataclass

from .contracts import ModelMetrics
from ..logging_config import get_logger

logger = get_logger("ai.trainer")


@dataclass
class TrainConfig:
    test_size: float = 0.2
    random_state: int = 42
    n_splits: int = 5       # 时序交叉验证折数
    label_days: int = 3     # 预测未来N日收益


class BaseTrainer:
    """模型训练基类"""

    def prepare_labels(self, prices: pd.Series, days: int = 3) -> pd.Series:
        """生成标签: 未来N日收益率 > 0 为正样本"""
        future_return = prices.pct_change(days).shift(-days)
        return (future_return > 0).astype(int)

    def time_split(self, df: pd.DataFrame, n_splits: int = 5) -> list[tuple]:
        """时序交叉验证分割"""
        n = len(df)
        fold_size = n // (n_splits + 1)
        splits = []
        for i in range(n_splits):
            train_end = fold_size * (i + 1)
            test_end = min(train_end + fold_size, n)
            splits.append((df.iloc[:train_end], df.iloc[train_end:test_end]))
        return splits

    def calc_metrics(self, y_true: np.ndarray, y_pred: np.ndarray, y_prob: np.ndarray | None = None) -> ModelMetrics:
        """计算模型指标"""
        from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
        acc = float(accuracy_score(y_true, y_pred))
        prec = float(precision_score(y_true, y_pred, zero_division=0))
        rec = float(recall_score(y_true, y_pred, zero_division=0))
        auc = float(roc_auc_score(y_true, y_prob)) if y_prob is not None else 0.0
        return ModelMetrics(auc=auc, accuracy=acc, precision=prec, recall=rec)
