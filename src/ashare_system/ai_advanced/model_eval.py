"""模型评估 pipeline — AUC/IC"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field

from ..ai.contracts import ModelMetrics
from ..factors.validator import FactorValidator
from ..logging_config import get_logger

logger = get_logger("ai_advanced.model_eval")


@dataclass
class EvalReport:
    model_name: str
    metrics: ModelMetrics
    ic_report: dict[str, float] = field(default_factory=dict)
    passed: bool = False
    suggestions: list[str] = field(default_factory=list)

    AUC_THRESHOLD = 0.80
    IC_THRESHOLD = 0.03


class ModelEvaluator:
    """模型评估 pipeline"""

    def __init__(self) -> None:
        self.factor_validator = FactorValidator()

    def evaluate(self, model_name: str, y_true: np.ndarray, y_pred: np.ndarray, y_prob: np.ndarray | None = None) -> EvalReport:
        """评估分类模型"""
        try:
            from sklearn.metrics import accuracy_score, roc_auc_score
            acc = float(accuracy_score(y_true, y_pred))
            auc = float(roc_auc_score(y_true, y_prob)) if y_prob is not None else 0.0
        except ImportError:
            acc = float(np.mean(y_true == y_pred))
            auc = 0.0

        metrics = ModelMetrics(auc=auc, accuracy=acc)
        passed = auc >= EvalReport.AUC_THRESHOLD
        suggestions = []
        if auc < EvalReport.AUC_THRESHOLD:
            suggestions.append(f"AUC={auc:.3f} 低于阈值 {EvalReport.AUC_THRESHOLD}，建议增加特征或调参")
        if acc < 0.55:
            suggestions.append(f"准确率={acc:.3f} 偏低，建议检查标签质量")

        logger.info("模型评估 [%s]: AUC=%.3f, Acc=%.3f, 通过=%s", model_name, auc, acc, passed)
        return EvalReport(model_name=model_name, metrics=metrics, passed=passed, suggestions=suggestions)

    def evaluate_ic(self, predictions: pd.Series, forward_returns: pd.Series, model_name: str = "") -> dict[str, float]:
        """评估预测的 IC"""
        result = self.factor_validator.validate(predictions, forward_returns, model_name)
        return {"ic": result.ic_mean, "ir": result.ir, "is_valid": float(result.is_valid)}
