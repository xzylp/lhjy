"""XGBoost 选股评分模型"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ModelMetrics, PredictResult
from .trainer import BaseTrainer, TrainConfig
from ..logging_config import get_logger

logger = get_logger("ai.xgb_scorer")


@dataclass
class XGBConfig:
    n_estimators: int = 200
    max_depth: int = 6
    learning_rate: float = 0.05
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int = 5
    random_state: int = 42


class XGBScorer(BaseTrainer):
    """XGBoost 选股评分模型 (目标 AUC > 0.84)"""

    def __init__(self, config: XGBConfig | None = None) -> None:
        self.config = config or XGBConfig()
        self._model = None
        self._feature_names: list[str] = []

    def train(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None = None) -> ModelMetrics:
        """训练模型"""
        self._feature_names = list(X.columns)
        cfg = train_config or TrainConfig()
        split_idx = int(len(X) * (1 - cfg.test_size))
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        try:
            from xgboost import XGBClassifier
            self._model = XGBClassifier(
                n_estimators=self.config.n_estimators,
                max_depth=self.config.max_depth,
                learning_rate=self.config.learning_rate,
                subsample=self.config.subsample,
                colsample_bytree=self.config.colsample_bytree,
                min_child_weight=self.config.min_child_weight,
                random_state=self.config.random_state,
                eval_metric="auc",
                verbosity=0,
                early_stopping_rounds=20,
            )
            self._model.fit(
                X_train, y_train,
                eval_set=[(X_test, y_test)],
                verbose=False,
            )
        except ImportError:
            logger.warning("xgboost 未安装，使用 sklearn GradientBoosting 替代")
            from sklearn.ensemble import GradientBoostingClassifier
            self._model = GradientBoostingClassifier(
                n_estimators=self.config.n_estimators,
                max_depth=self.config.max_depth,
                learning_rate=self.config.learning_rate,
                random_state=self.config.random_state,
            )
            self._model.fit(X_train, y_train)

        y_pred = self._model.predict(X_test)
        y_prob = self._model.predict_proba(X_test)[:, 1] if hasattr(self._model, "predict_proba") else None
        metrics = self.calc_metrics(y_test.values, y_pred, y_prob)

        if hasattr(self._model, "feature_importances_"):
            metrics.feature_importance = dict(zip(self._feature_names, self._model.feature_importances_.tolist()))

        logger.info("XGBoost 训练完成: AUC=%.3f, Acc=%.3f", metrics.auc, metrics.accuracy)
        return metrics

    def predict(self, X: pd.DataFrame) -> list[PredictResult]:
        """推理: 输出每只股票的评分"""
        if self._model is None:
            logger.warning("模型未训练")
            return []
        X_aligned = X.reindex(columns=self._feature_names, fill_value=0)
        probs = self._model.predict_proba(X_aligned)[:, 1] if hasattr(self._model, "predict_proba") else self._model.predict(X_aligned).astype(float)
        return [
            PredictResult(symbol=str(idx), score=float(p) * 100, confidence=float(p), model_name="xgb_scorer")
            for idx, p in zip(X.index, probs)
        ]

    def get_feature_importance(self) -> dict[str, float]:
        if self._model is None or not hasattr(self._model, "feature_importances_"):
            return {}
        return dict(zip(self._feature_names, self._model.feature_importances_.tolist()))

    def save(self, path: str | Path) -> None:
        """保存模型到文件"""
        import joblib
        if self._model is None:
            raise ValueError("模型未训练，无法保存")
        joblib.dump({"model": self._model, "feature_names": self._feature_names}, Path(path))
        logger.info("XGBoost 模型已保存: %s", path)

    def load(self, path: str | Path) -> None:
        """从文件加载模型"""
        import joblib
        data = joblib.load(Path(path))
        self._model = data["model"]
        self._feature_names = data["feature_names"]
        logger.info("XGBoost 模型已加载: %s", path)
