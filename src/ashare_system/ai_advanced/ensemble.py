"""多模型 Stacking 集成"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field

from ..ai.contracts import ModelMetrics, PredictResult
from ..ai.trainer import BaseTrainer
from ..logging_config import get_logger

logger = get_logger("ai_advanced.ensemble")


@dataclass
class EnsembleConfig:
    method: str = "stacking"   # "stacking" | "voting" | "weighted"
    weights: dict[str, float] = field(default_factory=dict)
    n_folds: int = 5


class EnsembleModel(BaseTrainer):
    """多模型 Stacking 集成"""

    def __init__(self, config: EnsembleConfig | None = None) -> None:
        self.config = config or EnsembleConfig()
        self._models: dict[str, object] = {}
        self._meta_model = None

    def add_model(self, name: str, model) -> None:
        self._models[name] = model
        logger.info("集成模型添加: %s", name)

    def predict_weighted(self, X: pd.DataFrame) -> list[PredictResult]:
        """加权平均集成"""
        if not self._models:
            return []
        weights = self.config.weights
        total_weight = sum(weights.get(name, 1.0) for name in self._models)
        combined = np.zeros(len(X))

        for name, model in self._models.items():
            w = weights.get(name, 1.0) / total_weight
            if hasattr(model, "predict"):
                preds = model.predict(X)
                if isinstance(preds, list):
                    scores = np.array([p.score / 100 for p in preds])
                else:
                    scores = np.array(preds, dtype=float)
                combined += scores * w

        return [
            PredictResult(symbol=str(idx), score=float(s) * 100, confidence=float(s), model_name="ensemble")
            for idx, s in zip(X.index, combined)
        ]

    def train_stacking(self, X: pd.DataFrame, y: pd.Series) -> ModelMetrics:
        """Stacking 训练: 使用 K-fold OOF 预测作为元特征，避免数据泄漏"""
        if not self._models:
            return ModelMetrics()

        n = len(X)
        k = self.config.n_folds
        indices = np.arange(n)
        fold_size = n // k

        meta_features = pd.DataFrame(index=X.index)

        for name, model in self._models.items():
            if not hasattr(model, "train") or not hasattr(model, "predict"):
                continue
            oof_preds = np.full(n, np.nan)
            for fold in range(k):
                val_start = fold * fold_size
                val_end = val_start + fold_size if fold < k - 1 else n
                val_idx = indices[val_start:val_end]
                train_idx = np.concatenate([indices[:val_start], indices[val_end:]])

                X_tr = X.iloc[train_idx]
                y_tr = y.iloc[train_idx]
                X_val = X.iloc[val_idx]

                model.train(X_tr, y_tr)
                preds = model.predict(X_val)
                if isinstance(preds, list) and preds:
                    oof_preds[val_idx] = [p.score / 100 for p in preds]

            meta_features[name] = oof_preds

        meta_features = meta_features.dropna()
        y_meta = y.loc[meta_features.index]

        if meta_features.empty:
            return ModelMetrics()

        try:
            from sklearn.linear_model import LogisticRegression
            self._meta_model = LogisticRegression()
            self._meta_model.fit(meta_features, y_meta)
            y_pred = self._meta_model.predict(meta_features)
            return self.calc_metrics(y_meta.values, y_pred)
        except ImportError:
            logger.warning("sklearn 未安装，跳过 Stacking 元模型训练")
            return ModelMetrics()
