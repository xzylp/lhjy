"""Hermes 模型槽位策略定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..settings import HermesSettings


@dataclass(frozen=True)
class HermesModelSlot:
    id: str
    name: str
    display_model: str
    source: str
    provider_id: str
    provider_name: str
    base_url: str
    credential_configured: bool
    routing_share: str
    status: str
    latency_profile: str
    reasoning_profile: str
    notes: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    risk_levels: tuple[str, ...] = field(default_factory=tuple)
    task_kinds: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "display_model": self.display_model,
            "source": self.source,
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "base_url": self.base_url,
            "credential_configured": self.credential_configured,
            "routing_share": self.routing_share,
            "status": self.status,
            "latency_profile": self.latency_profile,
            "reasoning_profile": self.reasoning_profile,
            "notes": self.notes,
            "roles": list(self.roles),
            "risk_levels": list(self.risk_levels),
            "task_kinds": list(self.task_kinds),
        }


def build_model_slots(settings: HermesSettings) -> list[HermesModelSlot]:
    return [
        HermesModelSlot(
            id="workspace-default",
            name="默认工作台",
            display_model=settings.default_model,
            source="env.hermes.default_model",
            provider_id="minimax",
            provider_name=settings.minimax_provider_name,
            base_url=settings.minimax_base_url,
            credential_configured=bool(settings.minimax_api_key),
            routing_share="25%",
            status="active",
            latency_profile="balanced",
            reasoning_profile="medium",
            notes="默认控制台、主控回复与常规编排使用。偏日常问答与入口分流，不承接高风险深推理。",
            roles=("main", "runtime_scout", "cron_preopen_readiness"),
            risk_levels=("low", "medium"),
            task_kinds=("chat", "control", "summary"),
        ),
        HermesModelSlot(
            id="ops-fastlane",
            name="快速巡检",
            display_model=settings.fast_model,
            source="env.hermes.fast_model",
            provider_id="minimax",
            provider_name=settings.minimax_provider_name,
            base_url=settings.minimax_base_url,
            credential_configured=bool(settings.minimax_api_key),
            routing_share="25%",
            status="ready",
            latency_profile="fast",
            reasoning_profile="low",
            notes="适合秒级巡检、回执、状态查询和轻量澄清问答。",
            roles=tuple(role for role in settings.fast_roles if role not in {"execution_operator"}),
            risk_levels=("low", "medium"),
            task_kinds=("watch", "receipt", "heartbeat"),
        ),
        HermesModelSlot(
            id="research-deep-dive",
            name="深度研究",
            display_model=settings.compat_model or settings.deep_model,
            source="env.hermes.compat_model",
            provider_id="compat-gpt54",
            provider_name=settings.compat_provider_name,
            base_url=settings.compat_base_url,
            credential_configured=bool(settings.compat_api_key and settings.compat_base_url),
            routing_share="25%",
            status="ready",
            latency_profile="slow",
            reasoning_profile="high",
            notes="强推理岗位统一走 gpt-5.4，负责研究、战法组合、风控复核和长文本综合。",
            roles=settings.deep_roles,
            risk_levels=("medium", "high", "critical"),
            task_kinds=("research", "strategy", "risk", "audit"),
        ),
        HermesModelSlot(
            id="execution-guard",
            name="执行闸门",
            display_model=settings.compat_model or settings.execution_guard_model,
            source="env.hermes.compat_model",
            provider_id="compat-gpt54",
            provider_name=settings.compat_provider_name,
            base_url=settings.compat_base_url,
            credential_configured=bool(settings.compat_api_key and settings.compat_base_url),
            routing_share="25%",
            status="ready",
            latency_profile="balanced",
            reasoning_profile="high",
            notes="执行审批、资金动作、关键异常升级统一走 gpt-5.4，避免低成本模型误判。",
            roles=("execution_operator", "risk_gate"),
            risk_levels=("high", "critical"),
            task_kinds=("execution", "approval", "escalation"),
        ),
    ]
