"""盘中候选重排器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contracts import IntradayRankResult, MarketProfile, PlaybookContext, RankAction, SectorProfile


class IntradayRanker:
    """根据盘中板块和事件状态给出候选重排/冻结提示。"""

    def rank_candidates(
        self,
        *,
        candidates: list[str] | list[dict[str, Any]] | list[PlaybookContext],
        sector_profiles: list[SectorProfile] | list[dict[str, Any]] | None = None,
        market_profile: MarketProfile | dict[str, Any] | None = None,
        event_context: dict[str, Any] | None = None,
        playbook_contexts: list[PlaybookContext] | list[dict[str, Any]] | None = None,
    ) -> IntradayRankResult:
        generated_at = self._resolve_generated_at(event_context)
        sector_map = self._build_sector_map(sector_profiles or [])
        playbook_map = self._build_playbook_map(playbook_contexts or [])
        market_payload = self._payload(market_profile)
        event_payload = self._payload(event_context)
        candidate_payloads = [
            self._normalize_candidate(item, playbook_map)
            for item in candidates
        ]

        if str(market_payload.get("regime") or "") == "chaos":
            actions = [
                RankAction(
                    symbol=item["symbol"],
                    action="FREEZE_ALL",
                    trigger="market_chaos",
                    reason="市场 regime=chaos，盘中候选统一冻结等待情绪修复。",
                    priority_delta=-999,
                    generated_at=generated_at,
                )
                for item in candidate_payloads
                if item["symbol"]
            ]
            return IntradayRankResult(
                generated_at=generated_at,
                actions=actions,
                freeze_all_active=True,
                summary_lines=["市场进入 chaos，所有候选暂时冻结。"],
            )

        actions: list[RankAction] = []
        for item in candidate_payloads:
            symbol = item["symbol"]
            if not symbol:
                continue
            sector_name = item["sector"]
            sector_payload = sector_map.get(sector_name, {})

            if self._has_negative_event(symbol=symbol, sector_name=sector_name, event_context=event_payload):
                actions.append(
                    RankAction(
                        symbol=symbol,
                        action="FREEZE",
                        trigger="negative_event",
                        reason="event_context 检测到负面事件，当前候选冻结等待事件澄清。",
                        priority_delta=-100,
                        generated_at=generated_at,
                    )
                )
                continue

            life_cycle = str(sector_payload.get("life_cycle") or "")
            zt_count_delta = self._int(sector_payload.get("zt_count_delta"))
            if life_cycle == "retreat":
                actions.append(
                    RankAction(
                        symbol=symbol,
                        action="DOWNGRADE",
                        trigger="sector_retreat",
                        reason=f"所属板块 {sector_name or '未知板块'} 已进入 retreat，候选优先级下调。",
                        priority_delta=-2,
                        generated_at=generated_at,
                    )
                )
                continue
            if life_cycle == "ferment" and zt_count_delta >= 2:
                actions.append(
                    RankAction(
                        symbol=symbol,
                        action="UPGRADE",
                        trigger="sector_ferment_zt_rising",
                        reason=f"所属板块 {sector_name or '未知板块'} 处于 ferment，且 zt_count_delta={zt_count_delta} 明显上升。",
                        priority_delta=2,
                        generated_at=generated_at,
                    )
                )

        return IntradayRankResult(
            generated_at=generated_at,
            actions=actions,
            freeze_all_active=False,
            summary_lines=self._build_summary_lines(actions),
        )

    @staticmethod
    def _payload(item: Any) -> dict[str, Any]:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        return {}

    def _build_sector_map(self, sector_profiles: list[Any]) -> dict[str, dict[str, Any]]:
        return {
            str(payload.get("sector_name") or ""): payload
            for payload in (self._payload(item) for item in sector_profiles)
            if payload.get("sector_name")
        }

    def _build_playbook_map(self, playbook_contexts: list[Any]) -> dict[str, dict[str, Any]]:
        return {
            str(payload.get("symbol") or ""): payload
            for payload in (self._payload(item) for item in playbook_contexts)
            if payload.get("symbol")
        }

    def _normalize_candidate(self, item: Any, playbook_map: dict[str, dict[str, Any]]) -> dict[str, str]:
        payload = self._payload(item)
        if not payload and isinstance(item, str):
            payload = {"symbol": item}
        symbol = str(payload.get("symbol") or "").strip()
        playbook_payload = playbook_map.get(symbol, {})
        sector = (
            str(payload.get("sector") or "").strip()
            or str(payload.get("resolved_sector") or "").strip()
            or str(playbook_payload.get("sector") or "").strip()
        )
        return {"symbol": symbol, "sector": sector}

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _resolve_generated_at(self, event_context: dict[str, Any] | None) -> str:
        payload = self._payload(event_context)
        return str(payload.get("generated_at") or datetime.now().isoformat())

    def _has_negative_event(self, *, symbol: str, sector_name: str, event_context: dict[str, Any]) -> bool:
        highlights = list(event_context.get("highlights") or [])
        by_scope = event_context.get("by_scope") or {}
        related = highlights + list(by_scope.get("symbol") or []) + list(by_scope.get("sector") or []) + list(by_scope.get("market") or [])
        for item in related:
            payload = self._payload(item)
            impact = str(payload.get("impact") or "").strip().lower()
            sentiment = str(payload.get("sentiment") or "").strip().lower()
            severity = str(payload.get("severity") or "").strip().lower()
            tags = [str(tag).strip().lower() for tag in list(payload.get("tags") or [])]
            item_symbol = str(payload.get("symbol") or "").strip()
            if item_symbol and item_symbol != symbol:
                continue
            if sector_name and sector_name.lower() not in " ".join(tags) and payload.get("impact_scope") == "sector" and not item_symbol:
                continue
            if impact in {"negative", "block"}:
                return True
            if sentiment in {"negative", "risk_off", "bearish"}:
                return True
            if severity in {"high", "critical", "block"} and any(tag in {"negative", "risk_off", "bearish", "warning"} for tag in tags):
                return True
        return False

    @staticmethod
    def _build_summary_lines(actions: list[RankAction]) -> list[str]:
        if not actions:
            return ["盘中未触发候选重排动作。"]
        lines: list[str] = []
        for item in actions[:8]:
            lines.append(f"{item.symbol} {item.action} trigger={item.trigger} reason={item.reason}")
        return lines
