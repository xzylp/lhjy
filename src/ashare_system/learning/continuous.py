"""持续学习框架"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..logging_config import get_logger

logger = get_logger("learning.continuous")


@dataclass
class LearningRecord:
    date: str
    model_name: str
    samples_added: int
    metrics_before: dict = field(default_factory=dict)
    metrics_after: dict = field(default_factory=dict)
    improved: bool = False


class ContinuousLearner:
    """持续学习框架 — 模型定期增量训练"""

    def __init__(self) -> None:
        self._records: list[LearningRecord] = []

    def incremental_train(self, model, new_X, new_y, model_name: str = "") -> LearningRecord:
        """增量训练: 用新数据更新模型"""
        today = date.today().isoformat()
        record = LearningRecord(date=today, model_name=model_name, samples_added=len(new_X))

        try:
            if hasattr(model, "train"):
                metrics = model.train(new_X, new_y)
                record.metrics_after = {"auc": metrics.auc, "accuracy": metrics.accuracy}
                record.improved = metrics.auc > self._get_last_auc(model_name)
                logger.info("增量训练 [%s]: %d 样本, AUC=%.3f", model_name, len(new_X), metrics.auc)
        except Exception as e:
            logger.warning("增量训练失败 [%s]: %s", model_name, e)

        self._records.append(record)
        return record

    def _get_last_auc(self, model_name: str) -> float:
        for rec in reversed(self._records):
            if rec.model_name == model_name and rec.metrics_after:
                return rec.metrics_after.get("auc", 0.0)
        return 0.0

    def get_history(self, model_name: str) -> list[LearningRecord]:
        return [r for r in self._records if r.model_name == model_name]

    def feedback_to_strategy(self, strategy_registry) -> dict[str, float]:
        """根据近期模型表现调整策略权重，返回调整后的权重"""
        strategies = strategy_registry.list_active()
        if not strategies:
            return {}
        adjustments: dict[str, float] = {}
        for s in strategies:
            records = self.get_history(s.name)
            if len(records) < 2:
                adjustments[s.name] = s.weight
                continue
            recent = records[-3:]  # 最近3次训练
            avg_auc = sum(r.metrics_after.get("auc", 0.5) for r in recent) / len(recent)
            # AUC > 0.6 加权，< 0.5 减权
            if avg_auc > 0.6:
                adjustments[s.name] = min(s.weight * 1.1, 0.5)
            elif avg_auc < 0.5:
                adjustments[s.name] = max(s.weight * 0.9, 0.1)
            else:
                adjustments[s.name] = s.weight
        # 归一化
        total = sum(adjustments.values()) or 1.0
        for name in adjustments:
            adjustments[name] /= total
        # 应用
        for s in strategies:
            if s.name in adjustments:
                s.weight = adjustments[s.name]
                logger.info("策略权重调整: %s → %.3f", s.name, s.weight)
        return adjustments
