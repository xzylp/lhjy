"""Hermes 岗位到模型槽位的路由。"""

from __future__ import annotations

from typing import Any

from ..settings import HermesSettings
from .model_policy import HermesModelSlot, build_model_slots


class HermesModelRouter:
    """按岗位、任务和风险选择合适模型槽位。"""

    def __init__(self, settings: HermesSettings) -> None:
        self.settings = settings
        self._slots = build_model_slots(settings)
        self._slot_map = {item.id: item for item in self._slots}

    def list_slots(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._slots]

    def resolve(
        self,
        *,
        role: str = "main",
        task_kind: str = "chat",
        risk_level: str = "medium",
        prefer_fast: bool = False,
        require_deep_reasoning: bool = False,
    ) -> dict[str, Any]:
        normalized_role = str(role or "main").strip()
        normalized_task = str(task_kind or "chat").strip()
        normalized_risk = str(risk_level or "medium").strip().lower()

        slot = self._pick_slot(
            role=normalized_role,
            task_kind=normalized_task,
            risk_level=normalized_risk,
            prefer_fast=prefer_fast,
            require_deep_reasoning=require_deep_reasoning,
        )
        return {
            "slot": slot.to_dict(),
            "role": normalized_role,
            "task_kind": normalized_task,
            "risk_level": normalized_risk,
            "prefer_fast": prefer_fast,
            "require_deep_reasoning": require_deep_reasoning,
            "routing_policy": self.settings.routing_policy,
            "policy_version": "hermes-model-router-v2",
            "reason": self._explain(slot, normalized_role, normalized_task, normalized_risk, prefer_fast, require_deep_reasoning),
        }

    def _pick_slot(
        self,
        *,
        role: str,
        task_kind: str,
        risk_level: str,
        prefer_fast: bool,
        require_deep_reasoning: bool,
    ) -> HermesModelSlot:
        strong_brain_tasks = {"research", "strategy", "risk", "audit", "execution", "approval", "escalation"}
        if risk_level in self.settings.escalation_risk_levels or task_kind in {"execution", "approval", "escalation"}:
            return self._slot_map["execution-guard"]
        if require_deep_reasoning or role in set(self.settings.deep_roles) or task_kind in strong_brain_tasks:
            return self._slot_map["research-deep-dive"]
        if prefer_fast or role in set(self.settings.fast_roles) or task_kind in {"watch", "receipt", "heartbeat"}:
            return self._slot_map["ops-fastlane"]
        return self._slot_map["workspace-default"]

    @staticmethod
    def _explain(
        slot: HermesModelSlot,
        role: str,
        task_kind: str,
        risk_level: str,
        prefer_fast: bool,
        require_deep_reasoning: bool,
    ) -> str:
        reasons: list[str] = [f"岗位={role}", f"任务={task_kind}", f"风险={risk_level}"]
        if prefer_fast:
            reasons.append("偏好低时延")
        if require_deep_reasoning:
            reasons.append("要求深推理")
        reasons.append(f"provider={slot.provider_name}")
        return f"命中槽位 {slot.id}，原因：" + "，".join(reasons)
