"""团队注册表自动覆写器。

每日盘后由 score_state 的 weight 输出驱动，
自动更新 team_registry.final.json 中的 agent_weights 节点。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..logging_config import get_logger

logger = get_logger("learning.registry_updater")

REQUIRED_REGISTRY_KEYS = {"name", "version", "teams", "agent_roles"}
VALID_AGENT_IDS = {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}


class RegistryUpdater:
    """team_registry.final.json 的 agent_weights 自动覆写器。"""

    def __init__(
        self,
        registry_path: Path,
        history_path: Path | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry_path = registry_path
        self._history_path = history_path or (registry_path.parent / "learning" / "registry_update_history.json")
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def update_agent_weights(self, weights: dict[str, float]) -> bool:
        """更新注册表中的 agent_weights 节点。

        Args:
            weights: {agent_id: weight_value}，如 {"ashare-risk": 0.6, ...}

        Returns:
            True 如果更新成功，False 如果校验失败

        安全约束：
        - 只更新 agent_weights 节点，不碰其他内容
        - 写入前做 JSON schema 校验
        - 所有 weight 值必须在 [0.0, 1.5] 范围内
        - agent_id 必须在白名单中
        """
        if not self._registry_path.exists():
            logger.error("注册表文件不存在: %s", self._registry_path)
            return False

        try:
            registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("注册表文件解析失败: %s", self._registry_path)
            return False

        # 校验注册表基本结构
        if not self._validate_registry_structure(registry):
            return False

        # 校验 weights
        if not self._validate_weights(weights):
            return False

        # 读取旧值
        old_weights = dict(registry.get("agent_weights", {}))

        # 更新
        registry["agent_weights"] = {
            agent_id: round(weights.get(agent_id, 1.0), 4)
            for agent_id in VALID_AGENT_IDS
        }

        # 写回
        self._registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # 记录历史
        self._append_history(old_weights, registry["agent_weights"])
        logger.info("注册表 agent_weights 已更新: %s", registry["agent_weights"])
        return True

    def update_from_scores(
        self,
        score_states: list[dict[str, Any]],
    ) -> bool:
        """从 AgentScoreState 列表中提取 weight_value 并更新注册表。

        Args:
            score_states: AgentScoreState.model_dump() 列表

        TODO:
            1. 与 scheduler.py 盘后任务集成
            2. 从 AgentScoreService 获取当日所有 score_states
        """
        weights: dict[str, float] = {}
        for state in score_states:
            agent_id = str(state.get("agent_id", ""))
            weight_value = float(state.get("weight_value", 1.0))
            if agent_id in VALID_AGENT_IDS:
                weights[agent_id] = weight_value
        if not weights:
            logger.warning("未找到有效的 agent score states，跳过注册表更新。")
            return False
        return self.update_agent_weights(weights)

    def read_current_weights(self) -> dict[str, float]:
        """读取注册表当前的 agent_weights。"""
        if not self._registry_path.exists():
            return {}
        try:
            registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in dict(registry.get("agent_weights", {})).items()}
        except Exception:
            return {}

    @staticmethod
    def _validate_registry_structure(registry: dict) -> bool:
        """校验注册表基本结构。"""
        for key in REQUIRED_REGISTRY_KEYS:
            if key not in registry:
                logger.error("注册表缺少必要字段: %s", key)
                return False
        return True

    @staticmethod
    def _validate_weights(weights: dict[str, float]) -> bool:
        """校验 weight 值的合法性。"""
        for agent_id, value in weights.items():
            if agent_id not in VALID_AGENT_IDS:
                logger.error("无效的 agent_id: %s", agent_id)
                return False
            if not (0.0 <= value <= 1.5):
                logger.error("weight 值越界: %s = %f (范围 0.0~1.5)", agent_id, value)
                return False
        return True

    def _append_history(self, old_weights: dict, new_weights: dict) -> None:
        """记录变更历史。"""
        history: list[dict] = []
        if self._history_path.exists():
            try:
                history = json.loads(self._history_path.read_text(encoding="utf-8"))
            except Exception:
                history = []
        history.append({
            "timestamp": self._now_factory().isoformat(),
            "old_weights": old_weights,
            "new_weights": new_weights,
        })
        history = history[-100:]
        self._history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
