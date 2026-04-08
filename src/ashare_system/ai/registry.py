"""AI 模型注册表 + 版本管理"""

from __future__ import annotations

import time
from pathlib import Path

from .contracts import ModelVersion
from ..logging_config import get_logger

logger = get_logger("ai.registry")


class ModelRegistry:
    """AI 模型注册表 — 统一管理模型版本"""

    def __init__(self) -> None:
        self._models: dict[str, list[ModelVersion]] = {}

    def register(self, version: ModelVersion) -> None:
        self._models.setdefault(version.name, []).append(version)
        logger.info("模型注册: %s v%s", version.name, version.version)

    def get_active(self, name: str) -> ModelVersion | None:
        versions = self._models.get(name, [])
        active = [v for v in versions if v.is_active]
        return active[-1] if active else None

    def list_models(self) -> list[str]:
        return list(self._models.keys())

    def deactivate(self, name: str, version: str) -> None:
        for v in self._models.get(name, []):
            if v.version == version:
                v.is_active = False


class ModelStore:
    """模型持久化存储 + 自动重训触发"""

    RETRAIN_DAYS = 30       # 超过30天触发重训
    AUC_DROP_THRESHOLD = 0.05  # AUC下降超过5%触发重训

    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._meta: dict[str, dict] = {}  # {model_name: {path, train_time, train_auc}}

    def save_model(self, name: str, model, auc: float = 0.0) -> Path:
        """保存模型并记录元数据"""
        path = self.store_dir / f"{name}_latest"
        if hasattr(model, "save"):
            model.save(path)
        self._meta[name] = {
            "path": str(path),
            "train_time": time.time(),
            "train_auc": auc,
        }
        logger.info("模型保存: %s (AUC=%.3f)", name, auc)
        return path

    def load_model(self, name: str, model) -> bool:
        """加载已保存的模型"""
        meta = self._meta.get(name)
        if not meta:
            return False
        path = Path(meta["path"])
        if not path.exists():
            return False
        if hasattr(model, "load"):
            model.load(path)
            logger.info("模型加载: %s", name)
            return True
        return False

    def should_retrain(self, name: str, current_auc: float = 0.0) -> bool:
        """判断是否需要重训: 超时 或 AUC下降"""
        meta = self._meta.get(name)
        if not meta:
            return True  # 从未训练
        days_since = (time.time() - meta["train_time"]) / 86400
        if days_since > self.RETRAIN_DAYS:
            logger.info("模型 %s 距上次训练 %.0f 天，触发重训", name, days_since)
            return True
        if current_auc > 0 and meta["train_auc"] - current_auc > self.AUC_DROP_THRESHOLD:
            logger.info("模型 %s AUC下降 %.3f → %.3f，触发重训", name, meta["train_auc"], current_auc)
            return True
        return False


# 全局单例
model_registry = ModelRegistry()
