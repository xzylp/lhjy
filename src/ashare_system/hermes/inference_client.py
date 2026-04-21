"""Hermes 通用推理调用器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..settings import HermesSettings
from .model_policy import HermesModelSlot
from .model_router import HermesModelRouter


def _extract_message_text(payload: dict[str, Any]) -> str:
    choices = list(payload.get("choices") or [])
    if not choices:
        return ""
    message = dict((choices[0] or {}).get("message") or {})
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type in {"text", "output_text"}:
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


@dataclass(frozen=True)
class HermesInferenceResult:
    ok: bool
    text: str
    selected_slot: dict[str, Any]
    effective_slot: dict[str, Any]
    routing_reason: str
    fallback_reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "selected_slot": self.selected_slot,
            "effective_slot": self.effective_slot,
            "routing_reason": self.routing_reason,
            "fallback_reason": self.fallback_reason,
            "error": self.error,
        }


class HermesInferenceClient:
    """调用 OpenAI 兼容接口执行 Hermes 自由问答。"""

    def __init__(self, settings: HermesSettings, router: HermesModelRouter | None = None, timeout_sec: float = 12.0) -> None:
        self.settings = settings
        self.router = router or HermesModelRouter(settings)
        self.timeout_sec = timeout_sec
        self._slot_map: dict[str, HermesModelSlot] = {
            slot.id: slot
            for slot in self.router._slots  # noqa: SLF001 - Hermes 内部组件共享同一份槽位定义
        }

    def complete(
        self,
        *,
        question: str,
        role: str = "main",
        task_kind: str = "chat",
        risk_level: str = "medium",
        prefer_fast: bool = False,
        require_deep_reasoning: bool = False,
        system_prompt: str = "",
        context_lines: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 500,
    ) -> HermesInferenceResult:
        resolved = self.router.resolve(
            role=role,
            task_kind=task_kind,
            risk_level=risk_level,
            prefer_fast=prefer_fast,
            require_deep_reasoning=require_deep_reasoning,
        )
        selected_slot = self._slot_map[resolved["slot"]["id"]]
        effective_slot, fallback_reason = self._resolve_effective_slot(
            selected_slot,
            require_deep_reasoning=require_deep_reasoning,
            risk_level=risk_level,
        )
        if effective_slot is None:
            return HermesInferenceResult(
                ok=False,
                text="",
                selected_slot=selected_slot.to_dict(),
                effective_slot={},
                routing_reason=str(resolved.get("reason") or ""),
                fallback_reason=fallback_reason,
                error="no_provider_configured",
            )

        prompt_parts = [str(system_prompt or "").strip()]
        context_values = [str(item or "").strip() for item in list(context_lines or []) if str(item or "").strip()]
        if context_values:
            prompt_parts.append("上下文：\n" + "\n".join(f"- {item}" for item in context_values))
        prompt_parts.append("要求：只用中文作答；不要捏造交易事实；如果上下文不足就明确说不足。")
        composed_system_prompt = "\n\n".join(part for part in prompt_parts if part)

        try:
            payload = self._post_chat_completion(
                slot=effective_slot,
                messages=[
                    {"role": "system", "content": composed_system_prompt},
                    {"role": "user", "content": str(question or "").strip()},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = _extract_message_text(payload)
            if not text:
                raise ValueError("empty_response_text")
            return HermesInferenceResult(
                ok=True,
                text=text,
                selected_slot=selected_slot.to_dict(),
                effective_slot=effective_slot.to_dict(),
                routing_reason=str(resolved.get("reason") or ""),
                fallback_reason=fallback_reason,
            )
        except Exception as exc:
            return HermesInferenceResult(
                ok=False,
                text="",
                selected_slot=selected_slot.to_dict(),
                effective_slot=effective_slot.to_dict(),
                routing_reason=str(resolved.get("reason") or ""),
                fallback_reason=fallback_reason,
                error=str(exc),
            )

    def _resolve_effective_slot(
        self,
        selected_slot: HermesModelSlot,
        *,
        require_deep_reasoning: bool,
        risk_level: str,
    ) -> tuple[HermesModelSlot | None, str]:
        if self._slot_available(selected_slot):
            return selected_slot, ""

        candidate_ids: list[str] = []
        if require_deep_reasoning or risk_level in {"high", "critical"}:
            candidate_ids.extend(["execution-guard", "research-deep-dive"])
        else:
            candidate_ids.extend(["workspace-default", "ops-fastlane", "research-deep-dive", "execution-guard"])

        for slot_id in candidate_ids:
            slot = self._slot_map.get(slot_id)
            if slot is None or slot.id == selected_slot.id:
                continue
            if self._slot_available(slot):
                return slot, f"selected_slot_unavailable:{selected_slot.id}->{slot.id}"

        return None, f"selected_slot_unavailable:{selected_slot.id}"

    @staticmethod
    def _slot_available(slot: HermesModelSlot) -> bool:
        return bool(slot.base_url and slot.credential_configured and slot.display_model)

    def _post_chat_completion(
        self,
        *,
        slot: HermesModelSlot,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        base_url = str(slot.base_url or "").rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._resolve_api_key(slot)}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": slot.display_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid_chat_completion_payload")
        return data

    def _resolve_api_key(self, slot: HermesModelSlot) -> str:
        if slot.provider_id == "minimax":
            return str(self.settings.minimax_api_key or "").strip()
        return str(self.settings.compat_api_key or "").strip()
