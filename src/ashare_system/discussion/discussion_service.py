"""v0.9 讨论流程服务。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from ..governance.param_service import ParameterService
from ..logging_config import get_logger
from .candidate_case import CandidateCaseService
from .finalizer import build_finalize_bundle
from .opinion_ingress import adapt_openclaw_opinion_payload
from .round_summarizer import build_trade_date_summary
from .state_machine import DiscussionState, DiscussionStateMachine, PoolState

logger = get_logger("discussion.cycle")


class DiscussionCycle(BaseModel):
    cycle_id: str
    trade_date: str
    pool_state: PoolState
    discussion_state: DiscussionState
    base_pool_case_ids: list[str] = Field(default_factory=list)
    focus_pool_case_ids: list[str] = Field(default_factory=list)
    round_2_target_case_ids: list[str] = Field(default_factory=list)
    execution_pool_case_ids: list[str] = Field(default_factory=list)
    started_at: str
    round_1_started_at: str | None = None
    round_1_completed_at: str | None = None
    round_2_started_at: str | None = None
    round_2_completed_at: str | None = None
    finalized_at: str | None = None
    blockers: list[str] = Field(default_factory=list)
    summary_snapshot: dict = Field(default_factory=dict)
    updated_at: str


class DiscussionCycleService:
    """围绕 candidate_case 的讨论流程宿主。"""

    def __init__(
        self,
        storage_path: Path,
        candidate_case_service: CandidateCaseService,
        parameter_service: ParameterService | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._candidate_case_service = candidate_case_service
        self._parameter_service = parameter_service
        self._now_factory = now_factory or datetime.now

    def list_cycles(self) -> list[DiscussionCycle]:
        items = self._read_cycles()
        items.sort(key=lambda item: item.trade_date, reverse=True)
        return items

    def get_cycle(self, trade_date: str) -> DiscussionCycle | None:
        for item in self._read_cycles():
            if item.trade_date == trade_date:
                return item
        return None

    def bootstrap_cycle(self, trade_date: str) -> DiscussionCycle:
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        if not cases:
            raise ValueError(f"no candidate cases found for trade_date={trade_date}")
        existing = self.get_cycle(trade_date)
        if existing is not None:
            existing.base_pool_case_ids = [item.case_id for item in cases]
            existing.focus_pool_case_ids = self._select_focus_pool(trade_date)
            existing.summary_snapshot = self.build_summary_snapshot(trade_date)
            existing.updated_at = self._now_factory().isoformat()
            return self._upsert(existing)
        pool_state, discussion_state = DiscussionStateMachine.bootstrap()
        focus_capacity = int(self._parameter_service.get_param_value("focus_pool_capacity")) if self._parameter_service else 15
        cycle = DiscussionCycle(
            cycle_id=f"cycle-{trade_date.replace('-', '')}",
            trade_date=trade_date,
            pool_state=pool_state,
            discussion_state=discussion_state,
            base_pool_case_ids=[item.case_id for item in cases],
            focus_pool_case_ids=[item.case_id for item in cases[:focus_capacity]],
            started_at=self._now_factory().isoformat(),
            summary_snapshot=self.build_summary_snapshot(trade_date),
            updated_at=self._now_factory().isoformat(),
        )
        return self._upsert(cycle)

    def start_round(self, trade_date: str, round_number: int) -> DiscussionCycle:
        cycle = self.get_cycle(trade_date) or self.bootstrap_cycle(trade_date)
        now = self._now_factory().isoformat()
        if round_number == 1:
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.start_round_1()
            cycle.round_1_started_at = now
        elif round_number == 2:
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.start_round_2()
            cycle.round_2_started_at = now
            cycle.round_2_target_case_ids = self._select_round_2_targets(trade_date)
        else:
            raise ValueError("round_number must be 1 or 2")
        cycle.updated_at = now
        return self._upsert(cycle)

    def refresh_cycle(self, trade_date: str) -> DiscussionCycle:
        cycle = self.get_cycle(trade_date) or self.bootstrap_cycle(trade_date)
        summary = self.build_summary_snapshot(trade_date)
        cycle.summary_snapshot = summary
        cycle.focus_pool_case_ids = self._select_focus_pool(trade_date)
        cycle.blockers = self._derive_blockers(summary)
        cycle.execution_pool_case_ids = self._select_execution_pool(trade_date)
        now = self._now_factory().isoformat()

        if cycle.discussion_state == "round_1_running" and self._round_1_complete(summary, cycle):
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.complete_round_1()
            cycle.round_1_completed_at = now

        if cycle.discussion_state == "round_2_running" and self._round_2_complete(trade_date, cycle):
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.complete_round_2()
            cycle.round_2_completed_at = now

        cycle.updated_at = now
        return self._upsert(cycle)

    def finalize_cycle(self, trade_date: str) -> DiscussionCycle:
        cycle = self.refresh_cycle(trade_date)
        if not self.can_finalize(cycle):
            cycle.blockers = list(dict.fromkeys([*cycle.blockers, "discussion_not_ready"]))
            cycle.updated_at = self._now_factory().isoformat()
            return self._upsert(cycle)
        summary = cycle.summary_snapshot or self.build_summary_snapshot(trade_date)
        cycle.blockers = self._derive_blockers(summary)
        cycle.execution_pool_case_ids = self._select_execution_pool(trade_date)
        blocked = bool(cycle.blockers) or not cycle.execution_pool_case_ids
        cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.finalize(blocked)
        cycle.finalized_at = self._now_factory().isoformat()
        cycle.updated_at = cycle.finalized_at
        return self._upsert(cycle)

    def build_summary_snapshot(self, trade_date: str, use_helper: bool = True) -> dict:
        """最小 helper 接入点。

        当前默认优先走 round_summarizer helper，失败时回退到 CandidateCaseService 现有实现，
        以避免在主链切换初期因 schema 差异放大风险。
        """

        if not use_helper:
            return self._candidate_case_service.build_trade_date_summary(trade_date)
        try:
            cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
            return build_trade_date_summary(
                [item.model_dump() for item in cases],
                trade_date=trade_date,
            )
        except Exception:
            logger.exception("build_summary_snapshot helper failed, fallback to candidate_case service: %s", trade_date)
            return self._candidate_case_service.build_trade_date_summary(trade_date)

    def build_finalize_bundle(
        self,
        trade_date: str,
        *,
        execution_precheck: dict | None = None,
        execution_dispatch: dict | None = None,
        include_client_brief: bool = True,
    ) -> dict:
        """最小 helper 接入点，供主链后续替换 finalize 拼包逻辑。"""

        cycle = self.get_cycle(trade_date)
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        bundle = build_finalize_bundle(
            trade_date=trade_date,
            cases=[item.model_dump() for item in cases],
            cycle=cycle.model_dump() if cycle is not None else None,
            execution_precheck=execution_precheck,
            execution_dispatch=execution_dispatch,
            include_client_brief=include_client_brief,
        )
        return bundle.model_dump()

    def adapt_openclaw_opinion_payload(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str | None = None,
        expected_case_ids: list[str] | None = None,
        case_id_map: dict[str, str] | None = None,
        default_case_id: str | None = None,
    ) -> dict:
        """按 trade_date 自动补齐 case 映射，返回可直接写回主链的 opinion 适配结果。"""
        resolved_case_id_map = self._build_case_id_map(trade_date)
        if case_id_map:
            resolved_case_id_map.update(case_id_map)
        resolved_default_case_id = default_case_id
        if not resolved_default_case_id and len(resolved_case_id_map) == 1:
            resolved_default_case_id = next(iter(resolved_case_id_map.values()))
        result = adapt_openclaw_opinion_payload(
            payload,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
            expected_case_ids=expected_case_ids,
            case_id_map=resolved_case_id_map,
            default_case_id=resolved_default_case_id,
        )
        return {
            "trade_date": trade_date,
            "case_id_map": resolved_case_id_map,
            "default_case_id": resolved_default_case_id,
            **result,
        }

    def write_openclaw_opinions(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str | None = None,
        expected_case_ids: list[str] | None = None,
        case_id_map: dict[str, str] | None = None,
        default_case_id: str | None = None,
        auto_rebuild: bool = True,
        refresh_summary: bool = True,
    ) -> dict:
        """适配 OpenClaw opinion payload 并按现有 candidate_case 写回接口落盘。"""
        result = self.adapt_openclaw_opinion_payload(
            payload,
            trade_date=trade_date,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
            expected_case_ids=expected_case_ids,
            case_id_map=case_id_map,
            default_case_id=default_case_id,
        )
        response = {
            "ok": result["ok"],
            "trade_date": trade_date,
            "case_id_map": result["case_id_map"],
            "default_case_id": result["default_case_id"],
            "raw_count": result["raw_count"],
            "normalized_payloads": result["normalized_payloads"],
            "normalized_items": result["normalized_items"],
            "issues": result["issues"],
            "summary_lines": result["summary_lines"],
            "covered_case_ids": result["covered_case_ids"],
            "missing_case_ids": result["missing_case_ids"],
            "duplicate_keys": result["duplicate_keys"],
            "substantive_round_2_case_ids": result["substantive_round_2_case_ids"],
            "writeback_items": result.get("writeback_items", []),
            "written_count": 0,
            "written_case_ids": [],
            "rebuilt_case_ids": [],
            "refresh_summary": refresh_summary,
            "refreshed_summary_snapshot": {},
            "touched_case_summaries": [],
            "items": [],
            "count": 0,
        }
        if not result["ok"]:
            return response
        writeback_items = result.get("writeback_items", [])
        updated = self._candidate_case_service.record_opinions_batch(writeback_items)
        written_case_ids = list(dict.fromkeys(case_id for case_id, _ in writeback_items))
        rebuilt_case_ids: list[str] = []
        if auto_rebuild:
            rebuilt_case_ids = written_case_ids
            updated = [self._candidate_case_service.rebuild_case(case_id) for case_id in rebuilt_case_ids]
        response["written_count"] = len(writeback_items)
        response["written_case_ids"] = written_case_ids
        response["rebuilt_case_ids"] = rebuilt_case_ids
        response["items"] = [item.model_dump() for item in updated]
        response["count"] = len(updated)
        response["touched_case_summaries"] = self._build_touched_case_summaries(updated)
        if refresh_summary:
            refreshed_summary = self.build_summary_snapshot(trade_date)
            response["refreshed_summary_snapshot"] = refreshed_summary
            cycle = self.get_cycle(trade_date)
            if cycle is not None:
                cycle.summary_snapshot = refreshed_summary
                cycle.updated_at = self._now_factory().isoformat()
                self._upsert(cycle)
        return response

    def build_openclaw_replay_packet(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str | None = None,
        expected_case_ids: list[str] | None = None,
        case_id_map: dict[str, str] | None = None,
        default_case_id: str | None = None,
    ) -> dict:
        """构建盘后 replay helper packet（研究用途，不触发 live）。"""
        packet = self.build_openclaw_replay_proposal_packet(
            payload,
            trade_date=trade_date,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
            expected_case_ids=expected_case_ids,
            case_id_map=case_id_map,
            default_case_id=default_case_id,
        )
        metadata = self._build_archive_ready_metadata(
            packet_type="openclaw_replay_packet",
            trade_date=packet["trade_date"],
            generated_at=packet["generated_at"],
            research_track="post_close_replay",
            preview=packet["preview"],
            summary_snapshot=packet["summary_snapshot"],
            cycle=packet["replay_packet"].get("cycle"),
        )
        archive_manifest = self._build_archive_manifest(
            packet_type="openclaw_replay_packet",
            packet_id=metadata["packet_id"],
            trade_date=packet["trade_date"],
            generated_at=packet["generated_at"],
            research_track=metadata["research_track"],
            archive_tags=metadata["archive_tags"],
        )
        latest_descriptor = self._build_latest_descriptor(
            packet_type="openclaw_replay_packet",
            packet_id=metadata["packet_id"],
            research_track=metadata["research_track"],
            source_refs=metadata["source_refs"],
            archive_tags=metadata["archive_tags"],
            archive_manifest=archive_manifest,
        )
        contract_sample = self._build_contract_sample(
            packet_type="openclaw_replay_packet",
            packet_id=metadata["packet_id"],
            trade_date=packet["trade_date"],
            source_refs=metadata["source_refs"],
            archive_tags=metadata["archive_tags"],
            archive_manifest=archive_manifest,
            latest_descriptor=latest_descriptor,
        )
        return {
            "packet_type": "openclaw_replay_packet",
            "trade_date": packet["trade_date"],
            "generated_at": packet["generated_at"],
            "source": packet["source"],
            "offline_only": True,
            "live_trigger": False,
            **metadata,
            "archive_manifest": archive_manifest,
            "latest_descriptor": latest_descriptor,
            "contract_sample": contract_sample,
            "semantics_note": "仅用于盘后 replay，不直接触发 live。",
            "preview": packet["preview"],
            "summary_snapshot": packet["summary_snapshot"],
            "writeback_preview": packet["writeback_preview"],
            "replay_packet": packet["replay_packet"],
        }

    def build_openclaw_archive_manifest(self, packet: dict) -> dict:
        """基于已生成 packet 重建 archive manifest。"""

        packet_type = str(packet.get("packet_type") or "")
        packet_id = str(packet.get("packet_id") or "")
        trade_date = str(packet.get("trade_date") or "")
        generated_at = str(packet.get("generated_at") or "")
        research_track = str(packet.get("research_track") or "")
        archive_tags = list(packet.get("archive_tags", []))
        if not packet_type or not packet_id or not trade_date or not generated_at or not research_track:
            raise ValueError("packet missing archive manifest fields")
        return self._build_archive_manifest(
            packet_type=packet_type,
            packet_id=packet_id,
            trade_date=trade_date,
            generated_at=generated_at,
            research_track=research_track,
            archive_tags=archive_tags,
        )

    def build_openclaw_latest_descriptor(self, packet: dict) -> dict:
        """基于已生成 packet 重建 latest descriptor。"""

        packet_type = str(packet.get("packet_type") or "")
        packet_id = str(packet.get("packet_id") or "")
        research_track = str(packet.get("research_track") or "")
        source_refs = list(packet.get("source_refs", []))
        archive_tags = list(packet.get("archive_tags", []))
        if not packet_type or not packet_id or not research_track:
            raise ValueError("packet missing latest descriptor fields")
        archive_manifest = packet.get("archive_manifest")
        if not isinstance(archive_manifest, dict):
            archive_manifest = self.build_openclaw_archive_manifest(packet)
        return self._build_latest_descriptor(
            packet_type=packet_type,
            packet_id=packet_id,
            research_track=research_track,
            source_refs=source_refs,
            archive_tags=archive_tags,
            archive_manifest=archive_manifest,
        )

    def build_openclaw_contract_sample(self, packet: dict) -> dict:
        """基于已生成 packet 重建统一 contract sample。"""

        packet_type = str(packet.get("packet_type") or "")
        packet_id = str(packet.get("packet_id") or "")
        trade_date = str(packet.get("trade_date") or "")
        source_refs = list(packet.get("source_refs", []))
        archive_tags = list(packet.get("archive_tags", []))
        if not packet_type or not packet_id or not trade_date:
            raise ValueError("packet missing contract sample fields")
        archive_manifest = packet.get("archive_manifest")
        if not isinstance(archive_manifest, dict):
            archive_manifest = self.build_openclaw_archive_manifest(packet)
        latest_descriptor = packet.get("latest_descriptor")
        if not isinstance(latest_descriptor, dict):
            latest_descriptor = self.build_openclaw_latest_descriptor(packet)
        return self._build_contract_sample(
            packet_type=packet_type,
            packet_id=packet_id,
            trade_date=trade_date,
            source_refs=source_refs,
            archive_tags=archive_tags,
            archive_manifest=archive_manifest,
            latest_descriptor=latest_descriptor,
        )

    def build_openclaw_proposal_packet(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str | None = None,
        expected_case_ids: list[str] | None = None,
        case_id_map: dict[str, str] | None = None,
        default_case_id: str | None = None,
    ) -> dict:
        """构建 proposal helper packet（自我进化研究用途，不触发 live）。"""
        packet = self.build_openclaw_replay_proposal_packet(
            payload,
            trade_date=trade_date,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
            expected_case_ids=expected_case_ids,
            case_id_map=case_id_map,
            default_case_id=default_case_id,
        )
        metadata = self._build_archive_ready_metadata(
            packet_type="openclaw_proposal_packet",
            trade_date=packet["trade_date"],
            generated_at=packet["generated_at"],
            research_track="self_evolution_research",
            preview=packet["preview"],
            summary_snapshot=packet["summary_snapshot"],
            cycle=packet["replay_packet"].get("cycle"),
        )
        archive_manifest = self._build_archive_manifest(
            packet_type="openclaw_proposal_packet",
            packet_id=metadata["packet_id"],
            trade_date=packet["trade_date"],
            generated_at=packet["generated_at"],
            research_track=metadata["research_track"],
            archive_tags=metadata["archive_tags"],
        )
        latest_descriptor = self._build_latest_descriptor(
            packet_type="openclaw_proposal_packet",
            packet_id=metadata["packet_id"],
            research_track=metadata["research_track"],
            source_refs=metadata["source_refs"],
            archive_tags=metadata["archive_tags"],
            archive_manifest=archive_manifest,
        )
        contract_sample = self._build_contract_sample(
            packet_type="openclaw_proposal_packet",
            packet_id=metadata["packet_id"],
            trade_date=packet["trade_date"],
            source_refs=metadata["source_refs"],
            archive_tags=metadata["archive_tags"],
            archive_manifest=archive_manifest,
            latest_descriptor=latest_descriptor,
        )
        return {
            "packet_type": "openclaw_proposal_packet",
            "trade_date": packet["trade_date"],
            "generated_at": packet["generated_at"],
            "source": packet["source"],
            "offline_only": True,
            "live_trigger": False,
            **metadata,
            "archive_manifest": archive_manifest,
            "latest_descriptor": latest_descriptor,
            "contract_sample": contract_sample,
            "semantics_note": "仅用于 replay / 自我进化 proposal 研究，不直接触发 live。",
            "preview": packet["preview"],
            "summary_snapshot": packet["summary_snapshot"],
            "writeback_preview": packet["writeback_preview"],
            "proposal_packet": packet["proposal_packet"],
        }

    def build_openclaw_replay_proposal_packet(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str | None = None,
        expected_case_ids: list[str] | None = None,
        case_id_map: dict[str, str] | None = None,
        default_case_id: str | None = None,
    ) -> dict:
        """为盘后 replay / 自我进化研究构建纯离线 packet。"""

        preview = self.adapt_openclaw_opinion_payload(
            payload,
            trade_date=trade_date,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
            expected_case_ids=expected_case_ids,
            case_id_map=case_id_map,
            default_case_id=default_case_id,
        )
        summary_snapshot = self.build_summary_snapshot(trade_date)
        cycle = self.get_cycle(trade_date)
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        case_lookup = {item.case_id: item for item in cases}
        writeback_candidates = self._serialize_writeback_candidates(preview.get("writeback_items", []), case_lookup)
        replay_summary_lines = self._build_replay_packet_summary_lines(
            preview=preview,
            summary_snapshot=summary_snapshot,
            writeback_candidates=writeback_candidates,
        )
        selected_case_ids = [item.get("case_id") for item in (summary_snapshot.get("selected", []) or []) if item.get("case_id")]
        focus_case_ids = list(cycle.focus_pool_case_ids) if cycle is not None else []
        round_2_target_case_ids = list(cycle.round_2_target_case_ids) if cycle is not None else []
        generated_at = self._now_factory().isoformat()
        metadata = self._build_archive_ready_metadata(
            packet_type="openclaw_replay_proposal",
            trade_date=trade_date,
            generated_at=generated_at,
            research_track="replay_proposal_dual_track",
            preview=preview,
            summary_snapshot=summary_snapshot,
            cycle=cycle.model_dump() if cycle is not None else None,
        )
        archive_manifest = self._build_archive_manifest(
            packet_type="openclaw_replay_proposal",
            packet_id=metadata["packet_id"],
            trade_date=trade_date,
            generated_at=generated_at,
            research_track=metadata["research_track"],
            archive_tags=metadata["archive_tags"],
        )
        latest_descriptor = self._build_latest_descriptor(
            packet_type="openclaw_replay_proposal",
            packet_id=metadata["packet_id"],
            research_track=metadata["research_track"],
            source_refs=metadata["source_refs"],
            archive_tags=metadata["archive_tags"],
            archive_manifest=archive_manifest,
        )
        contract_sample = self._build_contract_sample(
            packet_type="openclaw_replay_proposal",
            packet_id=metadata["packet_id"],
            trade_date=trade_date,
            source_refs=metadata["source_refs"],
            archive_tags=metadata["archive_tags"],
            archive_manifest=archive_manifest,
            latest_descriptor=latest_descriptor,
        )

        return {
            "packet_type": "openclaw_replay_proposal",
            "trade_date": trade_date,
            "generated_at": generated_at,
            "source": "discussion_service",
            "offline_only": True,
            "live_trigger": False,
            **metadata,
            "archive_manifest": archive_manifest,
            "latest_descriptor": latest_descriptor,
            "contract_sample": contract_sample,
            "preview": {
                "ok": preview["ok"],
                "trade_date": preview["trade_date"],
                "case_id_map": preview["case_id_map"],
                "default_case_id": preview["default_case_id"],
                "raw_count": preview["raw_count"],
                "normalized_payloads": preview["normalized_payloads"],
                "normalized_items": preview["normalized_items"],
                "issues": preview["issues"],
                "summary_lines": preview["summary_lines"],
                "covered_case_ids": preview["covered_case_ids"],
                "missing_case_ids": preview["missing_case_ids"],
                "duplicate_keys": preview["duplicate_keys"],
                "substantive_round_2_case_ids": preview["substantive_round_2_case_ids"],
            },
            "summary_snapshot": summary_snapshot,
            "writeback_preview": {
                "count": len(writeback_candidates),
                "case_ids": [item["case_id"] for item in writeback_candidates],
                "items": writeback_candidates,
                "summary_lines": [
                    f"writeback 候选 {len(writeback_candidates)} 条。",
                    "该预览仅供 replay/proposal 研究，不直接触发 live 写回。",
                ],
            },
            "replay_packet": {
                "mode": "post_close_replay",
                "offline_only": True,
                "selected_case_ids": selected_case_ids,
                "focus_pool_case_ids": focus_case_ids,
                "round_2_target_case_ids": round_2_target_case_ids,
                "cycle": cycle.model_dump() if cycle is not None else None,
                "summary_lines": replay_summary_lines,
            },
            "proposal_packet": {
                "mode": "self_evolution_research",
                "offline_only": True,
                "live_trigger": False,
                "writeback_count": len(writeback_candidates),
                "writeback_candidates": writeback_candidates,
                "covered_case_ids": preview["covered_case_ids"],
                "missing_case_ids": preview["missing_case_ids"],
                "duplicate_keys": preview["duplicate_keys"],
                "substantive_round_2_case_ids": preview["substantive_round_2_case_ids"],
                "summary_lines": [
                    *list(preview["summary_lines"]),
                    "该 proposal packet 仅供 replay / 自我进化研究，不直接触发 live。",
                ],
            },
        }

    def _build_case_id_map(self, trade_date: str) -> dict[str, str]:
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        return {item.symbol: item.case_id for item in cases}

    @staticmethod
    def _serialize_writeback_candidates(writeback_items: list, case_lookup: dict[str, object]) -> list[dict]:
        serialized: list[dict] = []
        for case_id, opinion in writeback_items:
            case = case_lookup.get(case_id)
            serialized.append(
                {
                    "case_id": case_id,
                    "symbol": case.symbol if case is not None else "",
                    "name": case.name if case is not None else "",
                    "round": opinion.round,
                    "agent_id": opinion.agent_id,
                    "stance": opinion.stance,
                    "confidence": opinion.confidence,
                    "reasons": list(opinion.reasons),
                    "evidence_refs": list(opinion.evidence_refs),
                    "thesis": opinion.thesis,
                    "key_evidence": list(opinion.key_evidence),
                    "evidence_gaps": list(opinion.evidence_gaps),
                    "questions_to_others": list(opinion.questions_to_others),
                    "remaining_disputes": list(opinion.remaining_disputes),
                }
            )
        return serialized

    @staticmethod
    def _build_archive_ready_metadata(
        *,
        packet_type: str,
        trade_date: str,
        generated_at: str,
        research_track: str,
        preview: dict,
        summary_snapshot: dict,
        cycle: dict | None,
    ) -> dict:
        compact_generated_at = (
            generated_at.replace("-", "").replace(":", "").replace("T", "").replace(".", "").replace("+", "")
        )
        packet_id = f"{packet_type}-{trade_date.replace('-', '')}-{compact_generated_at}"
        source_refs = [
            {
                "kind": "discussion_cycle",
                "trade_date": trade_date,
                "cycle_id": (cycle or {}).get("cycle_id", ""),
                "available": bool(cycle),
            },
            {
                "kind": "summary_snapshot",
                "trade_date": trade_date,
                "case_count": int(summary_snapshot.get("case_count", 0) or 0),
                "selected_count": int(summary_snapshot.get("selected_count", 0) or 0),
            },
            {
                "kind": "openclaw_preview",
                "trade_date": trade_date,
                "raw_count": int(preview.get("raw_count", 0) or 0),
                "covered_case_ids": list(preview.get("covered_case_ids", [])),
                "missing_case_ids": list(preview.get("missing_case_ids", [])),
            },
        ]
        archive_tags = [
            "openclaw",
            "discussion_helper",
            "offline_only",
            f"trade_date:{trade_date}",
            f"packet_type:{packet_type}",
            f"research_track:{research_track}",
        ]
        if preview.get("missing_case_ids"):
            archive_tags.append("has_missing_case_coverage")
        if preview.get("substantive_round_2_case_ids"):
            archive_tags.append("has_round_2_substantive_cases")
        if int(summary_snapshot.get("selected_count", 0) or 0) > 0:
            archive_tags.append("has_selected_cases")
        return {
            "packet_id": packet_id,
            "source_refs": source_refs,
            "archive_tags": archive_tags,
            "research_track": research_track,
        }

    @staticmethod
    def _build_archive_manifest(
        *,
        packet_type: str,
        packet_id: str,
        trade_date: str,
        generated_at: str,
        research_track: str,
        archive_tags: list[str],
    ) -> dict:
        trade_date_compact = trade_date.replace("-", "")
        generated_at_compact = (
            generated_at.replace("-", "").replace(":", "").replace("T", "").replace(".", "").replace("+", "")
        )
        archive_group = f"openclaw/discussion/{trade_date_compact}"
        artifact_name = f"{packet_id}.json"
        archive_path = f"{archive_group}/{research_track}/{artifact_name}"
        latest_aliases = [
            f"{archive_group}/{research_track}/latest.json",
            f"{archive_group}/{research_track}/latest-{packet_type}.json",
        ]
        recommended_tracks = [
            {
                "research_track": "post_close_replay",
                "archive_group": archive_group,
                "archive_path_prefix": f"{archive_group}/post_close_replay/",
                "artifact_name_pattern": (
                    f"openclaw_replay_packet-{trade_date_compact}-{generated_at_compact}.json"
                ),
                "latest_alias": f"{archive_group}/post_close_replay/latest.json",
            },
            {
                "research_track": "self_evolution_research",
                "archive_group": archive_group,
                "archive_path_prefix": f"{archive_group}/self_evolution_research/",
                "artifact_name_pattern": (
                    f"openclaw_proposal_packet-{trade_date_compact}-{generated_at_compact}.json"
                ),
                "latest_alias": f"{archive_group}/self_evolution_research/latest.json",
            },
        ]
        return {
            "version": "v1",
            "packet_type": packet_type,
            "packet_id": packet_id,
            "trade_date": trade_date,
            "research_track": research_track,
            "artifact_name": artifact_name,
            "archive_group": archive_group,
            "archive_path": archive_path,
            "latest_aliases": latest_aliases,
            "recommended_tracks": recommended_tracks,
            "archive_tags": list(archive_tags),
            "offline_only": True,
            "live_trigger": False,
        }

    @staticmethod
    def _build_latest_descriptor(
        *,
        packet_type: str,
        packet_id: str,
        research_track: str,
        source_refs: list[dict],
        archive_tags: list[str],
        archive_manifest: dict,
    ) -> dict:
        return {
            "version": "v1",
            "packet_type": packet_type,
            "packet_id": packet_id,
            "research_track": research_track,
            "artifact_name": str(archive_manifest.get("artifact_name") or ""),
            "archive_path": str(archive_manifest.get("archive_path") or ""),
            "latest_aliases": list(archive_manifest.get("latest_aliases", [])),
            "source_refs": list(source_refs),
            "archive_tags": list(archive_tags),
            "offline_only": True,
            "live_trigger": False,
        }

    @staticmethod
    def _build_contract_sample(
        *,
        packet_type: str,
        packet_id: str,
        trade_date: str,
        source_refs: list[dict],
        archive_tags: list[str],
        archive_manifest: dict,
        latest_descriptor: dict,
    ) -> dict:
        trade_date_compact = trade_date.replace("-", "")
        packet_id_parts = packet_id.split("-", 2)
        packet_id_suffix = packet_id_parts[2] if len(packet_id_parts) == 3 else "YYYYMMDDHHMMSS"
        archive_group = str(archive_manifest.get("archive_group") or f"openclaw/discussion/{trade_date_compact}")
        replay_packet_id = f"openclaw_replay_packet-{trade_date_compact}-{packet_id_suffix}"
        proposal_packet_id = f"openclaw_proposal_packet-{trade_date_compact}-{packet_id_suffix}"
        replay_archive_path = f"{archive_group}/post_close_replay/{replay_packet_id}.json"
        proposal_archive_path = f"{archive_group}/self_evolution_research/{proposal_packet_id}.json"
        replay_tags = sorted({*archive_tags, "packet_type:openclaw_replay_packet", "research_track:post_close_replay"})
        proposal_tags = sorted(
            {*archive_tags, "packet_type:openclaw_proposal_packet", "research_track:self_evolution_research"}
        )
        return {
            "version": "v1",
            "packet_type": packet_type,
            "packet_id": packet_id,
            "replay_descriptor_minimal": {
                "packet_type": "openclaw_replay_packet",
                "packet_id": replay_packet_id,
                "research_track": "post_close_replay",
                "artifact_name": f"{replay_packet_id}.json",
                "archive_path": replay_archive_path,
                "latest_aliases": [
                    f"{archive_group}/post_close_replay/latest.json",
                    f"{archive_group}/post_close_replay/latest-openclaw_replay_packet.json",
                ],
                "source_refs": list(source_refs),
                "archive_tags": replay_tags,
            },
            "proposal_descriptor_minimal": {
                "packet_type": "openclaw_proposal_packet",
                "packet_id": proposal_packet_id,
                "research_track": "self_evolution_research",
                "artifact_name": f"{proposal_packet_id}.json",
                "archive_path": proposal_archive_path,
                "latest_aliases": [
                    f"{archive_group}/self_evolution_research/latest.json",
                    f"{archive_group}/self_evolution_research/latest-openclaw_proposal_packet.json",
                ],
                "source_refs": list(source_refs),
                "archive_tags": proposal_tags,
            },
            "manifest_latest_mapping": [
                {"manifest_field": "artifact_name", "descriptor_field": "artifact_name", "relation": "equal"},
                {"manifest_field": "archive_path", "descriptor_field": "archive_path", "relation": "equal"},
                {"manifest_field": "latest_aliases", "descriptor_field": "latest_aliases", "relation": "equal"},
                {"manifest_field": "archive_tags", "descriptor_field": "archive_tags", "relation": "equal"},
            ],
            "linux_openclaw_samples": {
                "latest_pull": {
                    "alias": list(latest_descriptor.get("latest_aliases", []))[:1],
                    "expect_descriptor_fields": [
                        "packet_type",
                        "packet_id",
                        "research_track",
                        "artifact_name",
                        "archive_path",
                        "latest_aliases",
                    ],
                },
                "review_reference": {
                    "packet_id": latest_descriptor.get("packet_id", ""),
                    "source_refs": list(latest_descriptor.get("source_refs", [])),
                    "archive_tags": list(latest_descriptor.get("archive_tags", [])),
                },
                "archive_reference": {
                    "archive_group": archive_manifest.get("archive_group", ""),
                    "archive_path": latest_descriptor.get("archive_path", ""),
                    "artifact_name": latest_descriptor.get("artifact_name", ""),
                },
            },
            "offline_only": True,
            "live_trigger": False,
        }

    @staticmethod
    def _build_replay_packet_summary_lines(
        *,
        preview: dict,
        summary_snapshot: dict,
        writeback_candidates: list[dict],
    ) -> list[str]:
        lines = [
            f"preview 解析 {preview.get('raw_count', 0)} 条，候选 writeback {len(writeback_candidates)} 条。",
            f"当日 summary selected_count={summary_snapshot.get('selected_count', 0)}。",
        ]
        if preview.get("missing_case_ids"):
            lines.append(
                "缺失 case 覆盖: " + "；".join(str(item) for item in preview.get("missing_case_ids", [])[:5])
            )
        if preview.get("substantive_round_2_case_ids"):
            lines.append(
                "涉及实质性 Round2 case: "
                + "；".join(str(item) for item in preview.get("substantive_round_2_case_ids", [])[:5])
            )
        lines.append("该 replay packet 仅供盘后 replay，不直接触发 live。")
        return lines

    @staticmethod
    def _build_touched_case_summaries(items: list) -> list[dict]:
        summaries: list[dict] = []
        for item in items:
            opinions = list(item.opinions or [])
            latest_opinion = opinions[-1] if opinions else None
            summaries.append(
                {
                    "case_id": item.case_id,
                    "symbol": item.symbol,
                    "name": item.name,
                    "final_status": item.final_status,
                    "risk_gate": item.risk_gate,
                    "audit_gate": item.audit_gate,
                    "selected_reason": item.selected_reason,
                    "rejected_reason": item.rejected_reason,
                    "opinion_count": len(opinions),
                    "latest_opinion": (
                        {
                            "round": latest_opinion.round,
                            "agent_id": latest_opinion.agent_id,
                            "stance": latest_opinion.stance,
                            "confidence": latest_opinion.confidence,
                        }
                        if latest_opinion
                        else None
                    ),
                }
            )
        return summaries

    def _select_focus_pool(self, trade_date: str) -> list[str]:
        focus_capacity = int(self._parameter_service.get_param_value("focus_pool_capacity")) if self._parameter_service else 15
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        return [item.case_id for item in cases[:focus_capacity]]

    def _select_round_2_targets(self, trade_date: str) -> list[str]:
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        target_ids: list[str] = []
        for item in cases:
            if item.round_1_summary.questions_for_round_2 or item.audit_gate == "hold" or item.risk_gate == "limit":
                target_ids.append(item.case_id)
        return target_ids

    def _select_execution_pool(self, trade_date: str) -> list[str]:
        cap = int(self._parameter_service.get_param_value("execution_pool_capacity")) if self._parameter_service else 3
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        selected = [
            item.case_id
            for item in cases
            if item.final_status == "selected" and item.risk_gate != "reject" and item.audit_gate != "hold"
        ]
        return selected[:cap]

    @staticmethod
    def _derive_blockers(summary: dict) -> list[str]:
        blockers: list[str] = []
        selected_items = summary.get("selected", []) or []
        executable_selected = [
            item
            for item in selected_items
            if item.get("risk_gate") != "reject" and item.get("audit_gate") != "hold"
        ]
        if not selected_items:
            blockers.append("selected_count_zero")
        elif not executable_selected:
            blockers.append("selected_gate_blocked")
        return blockers

    @staticmethod
    def _round_1_complete(summary: dict, cycle: DiscussionCycle) -> bool:
        case_count = len(cycle.focus_pool_case_ids) or summary.get("case_count", 0)
        return summary.get("round_coverage", {}).get("round_1_ready", 0) >= min(case_count, summary.get("case_count", 0))

    def _round_2_complete(self, trade_date: str, cycle: DiscussionCycle) -> bool:
        if not cycle.round_2_target_case_ids:
            return True
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        case_map = {item.case_id: item for item in cases}
        for case_id in cycle.round_2_target_case_ids:
            item = case_map.get(case_id)
            if item is None:
                continue
            if not self._candidate_case_service.round_2_has_substantive_response(item):
                return False
        return True

    @staticmethod
    def can_finalize(cycle: DiscussionCycle) -> bool:
        return DiscussionStateMachine.can_finalize_from_summary(cycle.discussion_state, cycle.summary_snapshot)

    def _upsert(self, cycle: DiscussionCycle) -> DiscussionCycle:
        items = self._read_cycles()
        replaced = False
        for index, item in enumerate(items):
            if item.trade_date != cycle.trade_date:
                continue
            items[index] = cycle
            replaced = True
            break
        if not replaced:
            items.append(cycle)
        self._write_cycles(items)
        logger.info("discussion cycle updated: %s -> %s/%s", cycle.trade_date, cycle.pool_state, cycle.discussion_state)
        return cycle

    def _read_cycles(self) -> list[DiscussionCycle]:
        if not self._storage_path.exists():
            return []
        payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        return [DiscussionCycle(**item) for item in payload.get("cycles", [])]

    def _write_cycles(self, items: list[DiscussionCycle]) -> None:
        payload = {"cycles": [item.model_dump() for item in items]}
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
