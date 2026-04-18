"""v0.9 讨论流程服务。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field

from ..governance.param_service import ParameterService
from ..logging_config import get_logger
from .candidate_case import CandidateCaseService, CandidateOpinion
from .finalizer import build_finalize_bundle
from .opinion_ingress import adapt_openclaw_opinion_payload
from .round_summarizer import build_trade_date_summary
from .state_machine import DiscussionState, DiscussionStateMachine, PoolState

logger = get_logger("discussion.cycle")

if TYPE_CHECKING:
    from ..learning.score_state import AgentScoreService
    from ..strategy.learned_asset_service import LearnedAssetService


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
    # v1.0: 动态轮次支持
    current_round: int = 0                              # 当前轮次号
    max_rounds: int = 3                                 # 最大讨论轮次
    round_history: list[dict] = Field(default_factory=list)  # 每轮汇总快照


class DiscussionCycleService:
    """围绕 candidate_case 的讨论流程宿主。"""

    def __init__(
        self,
        storage_path: Path,
        candidate_case_service: CandidateCaseService,
        parameter_service: ParameterService | None = None,
        agent_score_service: AgentScoreService | None = None,
        learned_asset_service: LearnedAssetService | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._candidate_case_service = candidate_case_service
        self._parameter_service = parameter_service
        self._agent_score_service = agent_score_service
        self._learned_asset_service = learned_asset_service
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
        summary_snapshot = self.build_summary_snapshot(trade_date)
        execution_pool_case_ids = self._select_execution_pool(trade_date)
        blockers = self._derive_blockers(summary_snapshot, execution_pool_case_ids)
        existing = self.get_cycle(trade_date)
        if existing is not None:
            existing.base_pool_case_ids = [item.case_id for item in cases]
            existing.focus_pool_case_ids = self._select_focus_pool(trade_date)
            existing.execution_pool_case_ids = execution_pool_case_ids
            existing.summary_snapshot = summary_snapshot
            existing.blockers = blockers
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
            execution_pool_case_ids=execution_pool_case_ids,
            started_at=self._now_factory().isoformat(),
            summary_snapshot=summary_snapshot,
            blockers=blockers,
            updated_at=self._now_factory().isoformat(),
        )
        return self._upsert(cycle)

    def start_round(self, trade_date: str, round_number: int) -> DiscussionCycle:
        cycle = self.get_cycle(trade_date) or self.bootstrap_cycle(trade_date)
        now = self._now_factory().isoformat()
        # v1.0: 使用泛化的 start_round(n) 接口
        cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.start_round(round_number)
        cycle.current_round = round_number
        if round_number == 1:
            cycle.round_1_started_at = now
        elif round_number == 2:
            cycle.round_2_started_at = now
            cycle.round_2_target_case_ids = self._select_round_2_targets(trade_date)
        # Round 3+ 不再有专属时间戳字段，统一记录在 round_history 中
        cycle.updated_at = now
        return self._upsert(cycle)

    def refresh_cycle(self, trade_date: str) -> DiscussionCycle:
        cycle = self.get_cycle(trade_date) or self.bootstrap_cycle(trade_date)
        summary = self.build_summary_snapshot(trade_date)
        cycle.summary_snapshot = summary
        cycle.focus_pool_case_ids = self._select_focus_pool(trade_date)
        cycle.execution_pool_case_ids = self._select_execution_pool(trade_date)
        cycle.blockers = self._derive_blockers(summary, cycle.execution_pool_case_ids)
        now = self._now_factory().isoformat()

        if self._should_auto_start_round_1(cycle):
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.start_round(1)
            cycle.current_round = max(int(cycle.current_round or 0), 1)
            cycle.round_1_started_at = cycle.round_1_started_at or now
            logger.info(
                "discussion cycle auto-start round 1: trade_date=%s focus=%d execution=%d",
                trade_date,
                len(cycle.focus_pool_case_ids or []),
                len(cycle.execution_pool_case_ids or []),
            )

        if cycle.discussion_state == "round_1_running" and self._round_1_complete(summary, cycle):
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.complete_round(1)
            cycle.round_1_completed_at = now
            cycle.round_history.append({"round": 1, "completed_at": now, "summary": summary})

        if cycle.discussion_state == "round_2_running" and self._round_2_complete(trade_date, cycle):
            cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.complete_round(2)
            cycle.round_2_completed_at = now
            cycle.round_history.append({"round": 2, "completed_at": now, "summary": summary})

        # v1.0: Round 3+ 完成检测
        if cycle.discussion_state == "round_running" and cycle.current_round >= 3:
            # Round 3+ 复用 Round 2 的完成检查逻辑
            if self._round_2_complete(trade_date, cycle):
                cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.complete_round(cycle.current_round)
                cycle.round_history.append({"round": cycle.current_round, "completed_at": now, "summary": summary})

        # v1.0: 自动续轮判断 → 如果 round_summarized 且还有争议、反证或未闭环质询，且未达上限 → 自动进入下一轮
        if cycle.discussion_state in {"round_1_summarized", "round_summarized", "final_review_ready"}:
            if DiscussionStateMachine.can_continue_discussion(summary, cycle.current_round, cycle.max_rounds):
                next_round = cycle.current_round + 1
                cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.start_round(next_round)
                cycle.current_round = next_round
                logger.info("讨论自动续轮 (存在争议或质询): trade_date=%s round=%d", trade_date, next_round)

        cycle.updated_at = now
        return self._upsert(cycle)

    def finalize_cycle(self, trade_date: str) -> DiscussionCycle:
        cycle = self.refresh_cycle(trade_date)
        if not self.can_finalize(cycle):
            cycle.blockers = list(dict.fromkeys([*cycle.blockers, "discussion_not_ready"]))
            cycle.updated_at = self._now_factory().isoformat()
            return self._upsert(cycle)
        summary = cycle.summary_snapshot or self.build_summary_snapshot(trade_date)
        cycle.execution_pool_case_ids = self._select_execution_pool(trade_date)
        cycle.blockers = self._derive_blockers(summary, cycle.execution_pool_case_ids)
        if self._agent_score_service is not None:
            try:
                agent_weights = self._agent_score_service.export_weights(trade_date)
            except Exception:
                logger.exception("导出 agent weights 失败: trade_date=%s", trade_date)
                agent_weights = {}
            if agent_weights:
                cycle.summary_snapshot = dict(summary)
                cycle.summary_snapshot["agent_weights"] = agent_weights
                cycle.summary_snapshot["agent_weights_exported_at"] = self._now_factory().isoformat()
        blocked = bool(cycle.blockers) or not cycle.execution_pool_case_ids
        cycle.pool_state, cycle.discussion_state = DiscussionStateMachine.finalize(blocked)
        cycle.finalized_at = self._now_factory().isoformat()
        cycle.updated_at = cycle.finalized_at

        # 触发自治链: 自动发现学习产物 (R0.5)
        if self._learned_asset_service is not None:
            try:
                finalize_bundle = build_finalize_bundle(
                    trade_date=trade_date,
                    cases=[
                        item.model_dump() if hasattr(item, "model_dump") else dict(item)
                        for item in self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                    ],
                    include_client_brief=True,
                )
                self._learned_asset_service.auto_discover_from_discussion(
                    trade_date, finalize_bundle.finalize_packet.model_dump()
                )
            except Exception:
                logger.exception("自动发现学习产物失败: trade_date=%s", trade_date)

        return self._upsert(cycle)

    def build_summary_snapshot(self, trade_date: str, use_helper: bool = True) -> dict:
        """最小 helper 接入点。

        当前默认优先走 round_summarizer helper，失败时回退到 CandidateCaseService 现有实现，
        以避免在主链切换初期因 schema 差异放大风险。
        """
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        if not use_helper:
            summary = self._candidate_case_service.build_trade_date_summary(trade_date)
            return self._augment_summary_snapshot(summary, cases)
        try:
            summary = build_trade_date_summary(
                [item.model_dump() for item in cases],
                trade_date=trade_date,
            )
            return self._augment_summary_snapshot(summary, cases)
        except Exception:
            logger.exception("build_summary_snapshot helper failed, fallback to candidate_case service: %s", trade_date)
            summary = self._candidate_case_service.build_trade_date_summary(trade_date)
            return self._augment_summary_snapshot(summary, cases)

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
        agent_weights = {}
        if cycle is not None:
            agent_weights = dict((cycle.summary_snapshot or {}).get("agent_weights") or {})
        if not agent_weights and self._agent_score_service is not None:
            try:
                agent_weights = self._agent_score_service.export_weights(trade_date)
            except Exception:
                logger.exception("构建 finalize bundle 时导出 agent weights 失败: trade_date=%s", trade_date)
        bundle = build_finalize_bundle(
            trade_date=trade_date,
            cases=[item.model_dump() for item in cases],
            cycle=cycle.model_dump() if cycle is not None else None,
            execution_precheck=execution_precheck,
            execution_dispatch=execution_dispatch,
            include_client_brief=include_client_brief,
            agent_weights=agent_weights or None,
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

    def adapt_compose_result_to_strategy_opinions(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str = "ashare-strategy",
        case_id_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """把 compose 输出适配成 ashare-strategy 的 discussion opinions。"""
        compose_payload = dict(payload or {}) if isinstance(payload, dict) else {}
        proposal_packet = dict(compose_payload.get("proposal_packet") or {})
        if not proposal_packet:
            proposal_packet = {
                "selected_symbols": list(compose_payload.get("selected_symbols") or []),
                "watchlist_symbols": list(compose_payload.get("watchlist_symbols") or []),
                "discussion_focus": list(compose_payload.get("discussion_focus") or []),
            }
        candidates = [dict(item) for item in list(compose_payload.get("candidates") or []) if isinstance(item, dict)]
        candidate_map = {
            str(item.get("symbol") or "").strip(): item
            for item in candidates
            if str(item.get("symbol") or "").strip()
        }
        resolved_case_id_map = self._build_case_id_map(trade_date)
        if case_id_map:
            resolved_case_id_map.update(case_id_map)
        cycle = self.get_cycle(trade_date)
        resolved_round = expected_round or max(int(getattr(cycle, "current_round", 0) or 0), 1)
        selected_symbols = [
            str(item).strip()
            for item in list(proposal_packet.get("selected_symbols") or [])
            if str(item).strip()
        ]
        watchlist_symbols = [
            str(item).strip()
            for item in list(proposal_packet.get("watchlist_symbols") or [])
            if str(item).strip()
        ]
        ordered_symbols = list(dict.fromkeys([*selected_symbols, *watchlist_symbols]))
        case_lookup = {
            case.case_id: case
            for case in self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        }
        intent = dict(compose_payload.get("intent") or {})
        explanations = dict(compose_payload.get("explanations") or {})
        market_summary = dict(compose_payload.get("market_summary") or {})
        evaluation_trace = dict(compose_payload.get("evaluation_trace") or {})
        if not evaluation_trace and compose_payload.get("trace_id"):
            evaluation_trace = {"trace_id": compose_payload.get("trace_id")}
        runtime_job = dict(compose_payload.get("runtime_job") or {})
        recorded_at = self._now_factory().isoformat()

        writeback_items: list[tuple[str, CandidateOpinion]] = []
        missing_symbols: list[str] = []
        for symbol in ordered_symbols:
            case_id = resolved_case_id_map.get(symbol)
            if not case_id:
                missing_symbols.append(symbol)
                continue
            candidate = candidate_map.get(symbol, {})
            stance = "support" if symbol in selected_symbols else "watch"
            opinion = CandidateOpinion(
                round=resolved_round,
                agent_id=expected_agent_id,
                stance=stance,
                confidence=self._compose_confidence_for_candidate(candidate, stance=stance),
                reasons=self._build_compose_reason_lines(
                    candidate,
                    explanations=explanations,
                    market_summary=market_summary,
                ),
                evidence_refs=self._build_compose_evidence_refs(
                    evaluation_trace=evaluation_trace,
                    runtime_job=runtime_job,
                    candidate=candidate,
                ),
                thesis=self._build_compose_thesis(
                    symbol=symbol,
                    candidate=candidate,
                    intent=intent,
                    stance=stance,
                ),
                key_evidence=self._build_compose_key_evidence(
                    candidate,
                    explanations=explanations,
                ),
                evidence_gaps=self._build_compose_evidence_gaps(
                    candidate,
                    discussion_focus=proposal_packet.get("discussion_focus"),
                ),
                questions_to_others=self._build_compose_questions_to_others(
                    candidate,
                    discussion_focus=proposal_packet.get("discussion_focus"),
                ),
                recorded_at=recorded_at,
            )
            writeback_items.append((case_id, opinion))

        preview = self._serialize_writeback_candidates(writeback_items, case_lookup)
        summary_lines = [
            f"compose 候选={len(candidates)} selected={len(selected_symbols)} watchlist={len(watchlist_symbols)}",
            f"strategy writeback={len(writeback_items)} missing_symbols={len(missing_symbols)}",
        ]
        if evaluation_trace.get("trace_id"):
            summary_lines.append(f"compose trace={evaluation_trace.get('trace_id')}")
        if runtime_job.get("pipeline_job_id"):
            summary_lines.append(f"runtime job={runtime_job.get('pipeline_job_id')}")
        return {
            "ok": bool(writeback_items),
            "trade_date": trade_date,
            "round": resolved_round,
            "agent_id": expected_agent_id,
            "case_id_map": resolved_case_id_map,
            "selected_symbols": selected_symbols,
            "watchlist_symbols": watchlist_symbols,
            "covered_case_ids": [case_id for case_id, _ in writeback_items],
            "missing_symbols": missing_symbols,
            "summary_lines": summary_lines,
            "writeback_items": writeback_items,
            "preview_items": preview,
            "trace_id": str(evaluation_trace.get("trace_id") or "").strip() or None,
            "pipeline_job_id": str(runtime_job.get("pipeline_job_id") or "").strip() or None,
        }

    def write_compose_strategy_opinions(
        self,
        payload: object,
        *,
        trade_date: str,
        expected_round: int | None = None,
        expected_agent_id: str = "ashare-strategy",
        case_id_map: dict[str, str] | None = None,
        auto_rebuild: bool = True,
        refresh_summary: bool = True,
    ) -> dict[str, Any]:
        """把 compose 输出正式写回 strategy 讨论主线。"""
        result = self.adapt_compose_result_to_strategy_opinions(
            payload,
            trade_date=trade_date,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
            case_id_map=case_id_map,
        )
        response = {
            "ok": result["ok"],
            "trade_date": trade_date,
            "round": result["round"],
            "agent_id": result["agent_id"],
            "case_id_map": result["case_id_map"],
            "selected_symbols": result["selected_symbols"],
            "watchlist_symbols": result["watchlist_symbols"],
            "covered_case_ids": result["covered_case_ids"],
            "missing_symbols": result["missing_symbols"],
            "summary_lines": result["summary_lines"],
            "preview_items": result["preview_items"],
            "trace_id": result.get("trace_id"),
            "pipeline_job_id": result.get("pipeline_job_id"),
            "written_count": 0,
            "written_case_ids": [],
            "rebuilt_case_ids": [],
            "refreshed_summary_snapshot": {},
            "touched_case_summaries": [],
            "items": [],
            "count": 0,
        }
        if not result["ok"]:
            return response
        writeback_items = list(result.get("writeback_items") or [])
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
            "contradiction_summary_lines": packet.get("contradiction_summary_lines", []),
            "case_contradictions": packet.get("case_contradictions", []),
            "must_answer_questions": packet.get("must_answer_questions", []),
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
            "contradiction_summary_lines": packet.get("contradiction_summary_lines", []),
            "case_contradictions": packet.get("case_contradictions", []),
            "must_answer_questions": packet.get("must_answer_questions", []),
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
        selected_case_ids = [item.get("case_id") for item in (summary_snapshot.get("selected", []) or []) if item.get("case_id")]
        focus_case_ids = list(cycle.focus_pool_case_ids) if cycle is not None else []
        round_2_target_case_ids = list(cycle.round_2_target_case_ids) if cycle is not None else []
        contradiction_summary_lines = self._build_contradiction_summary_lines(cases)
        case_contradictions = self._build_case_contradictions(cases, preferred_case_ids=round_2_target_case_ids or focus_case_ids)
        must_answer_questions = self._build_must_answer_questions(
            cases,
            preferred_case_ids=round_2_target_case_ids or focus_case_ids or selected_case_ids,
        )
        replay_summary_lines = self._build_replay_packet_summary_lines(
            preview=preview,
            summary_snapshot=summary_snapshot,
            writeback_candidates=writeback_candidates,
            must_answer_questions=must_answer_questions,
        )
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
                "contradiction_summary_lines": contradiction_summary_lines,
                "case_contradictions": case_contradictions,
                "must_answer_questions": must_answer_questions,
            },
            "summary_snapshot": summary_snapshot,
            "contradiction_summary_lines": contradiction_summary_lines,
            "case_contradictions": case_contradictions,
            "must_answer_questions": must_answer_questions,
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
                "contradiction_summary_lines": contradiction_summary_lines,
                "case_contradictions": case_contradictions,
                "must_answer_questions": must_answer_questions,
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
                "contradiction_summary_lines": contradiction_summary_lines,
                "case_contradictions": case_contradictions,
                "must_answer_questions": must_answer_questions,
                "summary_lines": [
                    *list(preview["summary_lines"]),
                    *[f"矛盾检测：{item}" for item in contradiction_summary_lines[:3]],
                    *[f"必须回应：{item}" for item in must_answer_questions[:3]],
                    "该 proposal packet 仅供 replay / 自我进化研究，不直接触发 live。",
                ],
            },
        }

    def _build_case_id_map(self, trade_date: str) -> dict[str, str]:
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        return {item.symbol: item.case_id for item in cases}

    @staticmethod
    def _compose_confidence_for_candidate(candidate: dict[str, Any], *, stance: str) -> str:
        rank = int(candidate.get("rank", 99) or 99)
        composite_score = float(candidate.get("composite_score", 0.0) or 0.0)
        evidence_count = len([item for item in list(candidate.get("evidence") or []) if str(item).strip()])
        if stance == "support" and (rank <= 3 or composite_score >= 8.0 or evidence_count >= 3):
            return "high"
        if evidence_count <= 1 and composite_score <= 0.0:
            return "low"
        return "medium"

    @staticmethod
    def _build_compose_reason_lines(
        candidate: dict[str, Any],
        *,
        explanations: dict[str, Any],
        market_summary: dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        symbol = str(candidate.get("symbol") or "").strip()
        rank = candidate.get("rank")
        if symbol and rank:
            reasons.append(f"{symbol} 在 compose 排名中位于前列，当前 rank={rank}")
        for item in list(candidate.get("evidence") or [])[:3]:
            text = str(item).strip()
            if text:
                reasons.append(text)
        market_regime = str(market_summary.get("market_regime") or "").strip()
        if market_regime:
            reasons.append(f"当前市场阶段判断为 {market_regime}")
        for item in list(explanations.get("market_driver_summary") or [])[:2]:
            text = str(item).strip()
            if text:
                reasons.append(text)
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _build_compose_key_evidence(
        candidate: dict[str, Any],
        *,
        explanations: dict[str, Any],
    ) -> list[str]:
        key_evidence: list[str] = []
        for item in list(candidate.get("evidence") or [])[:4]:
            text = str(item).strip()
            if text:
                key_evidence.append(text)
        driver_tags = [
            str(item).strip()
            for item in list((candidate.get("market_drivers") or {}).get("tags") or [])
            if str(item).strip()
        ]
        if driver_tags:
            key_evidence.append("市场驱动标签: " + ",".join(driver_tags[:3]))
        for item in list(explanations.get("weight_summary") or [])[:2]:
            text = str(item).strip()
            if text:
                key_evidence.append(text)
        return list(dict.fromkeys(key_evidence))

    @staticmethod
    def _build_compose_evidence_gaps(candidate: dict[str, Any], *, discussion_focus: object) -> list[str]:
        evidence_gaps: list[str] = []
        for item in list(candidate.get("counter_evidence") or [])[:3]:
            text = str(item).strip()
            if text:
                evidence_gaps.append(text)
        for item in list(discussion_focus or [])[:2]:
            text = str(item).strip()
            if text:
                evidence_gaps.append(f"待讨论: {text}")
        evidence_gaps.append("需经 research/risk/audit 继续质询后再决定是否进入最终执行池")
        return list(dict.fromkeys(evidence_gaps))

    @staticmethod
    def _build_compose_questions_to_others(candidate: dict[str, Any], *, discussion_focus: object) -> list[str]:
        questions: list[str] = []
        for item in list(discussion_focus or [])[:3]:
            text = str(item).strip()
            if text:
                questions.append(text)
        risk_flags = [str(item).strip() for item in list(candidate.get("risk_flags") or []) if str(item).strip()]
        if risk_flags:
            questions.append("风控侧需确认: " + "；".join(risk_flags[:2]))
        return list(dict.fromkeys(questions))

    @staticmethod
    def _build_compose_evidence_refs(
        *,
        evaluation_trace: dict[str, Any],
        runtime_job: dict[str, Any],
        candidate: dict[str, Any],
    ) -> list[str]:
        refs: list[str] = []
        trace_id = str(evaluation_trace.get("trace_id") or "").strip()
        if trace_id:
            refs.append(f"compose_trace:{trace_id}")
        pipeline_job_id = str(runtime_job.get("pipeline_job_id") or "").strip()
        if pipeline_job_id:
            refs.append(f"runtime_job:{pipeline_job_id}")
        symbol = str(candidate.get("symbol") or "").strip()
        if symbol:
            refs.append(f"candidate:{symbol}")
        return refs

    @staticmethod
    def _build_compose_thesis(
        *,
        symbol: str,
        candidate: dict[str, Any],
        intent: dict[str, Any],
        stance: str,
    ) -> str:
        market_hypothesis = str(intent.get("market_hypothesis") or "").strip()
        objective = str(intent.get("objective") or "").strip()
        name = str(candidate.get("name") or "").strip()
        symbol_label = f"{symbol} {name}".strip()
        lead = market_hypothesis or objective or "当前由 strategy compose 形成候选提案"
        if stance == "support":
            return f"{lead}，策略侧主张把 {symbol_label} 列为优先讨论标的。".strip()
        return f"{lead}，策略侧建议把 {symbol_label} 作为观察候选继续跟踪。".strip()

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
                    "bull_case": dict(getattr(case, "bull_case", {}) or {}),
                    "bear_case": dict(getattr(case, "bear_case", {}) or {}),
                    "uncertainty": dict(getattr(case, "uncertainty", {}) or {}),
                    "contradictions": [dict(item) for item in getattr(case, "contradictions", []) or []],
                    "contradiction_summary_lines": list(getattr(case, "contradiction_summary_lines", []) or []),
                    "must_answer_questions": list(getattr(case, "must_answer_questions", []) or []),
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
        must_answer_questions: list[str],
    ) -> list[str]:
        lines = [
            f"preview 解析 {preview.get('raw_count', 0)} 条，候选 writeback {len(writeback_candidates)} 条。",
            f"当日 summary selected_count={summary_snapshot.get('selected_count', 0)}。",
        ]
        lines.extend(f"矛盾检测：{item}" for item in (summary_snapshot.get("contradiction_summary_lines", []) or [])[:3])
        if preview.get("missing_case_ids"):
            lines.append(
                "缺失 case 覆盖: " + "；".join(str(item) for item in preview.get("missing_case_ids", [])[:5])
            )
        if preview.get("substantive_round_2_case_ids"):
            lines.append(
                "涉及实质性 Round2 case: "
                + "；".join(str(item) for item in preview.get("substantive_round_2_case_ids", [])[:5])
            )
        lines.extend(f"必须回应：{item}" for item in must_answer_questions[:3])
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
                    "bull_case": dict(item.bull_case),
                    "bear_case": dict(item.bear_case),
                    "uncertainty": dict(item.uncertainty),
                    "contradictions": [dict(entry) for entry in item.contradictions],
                    "contradiction_summary_lines": list(item.contradiction_summary_lines),
                    "must_answer_questions": list(item.must_answer_questions),
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

    @staticmethod
    def _attach_case_evidence(item: dict, case: object | None) -> dict:
        updated = dict(item)
        if case is None:
            updated.setdefault("bull_case", {})
            updated.setdefault("bear_case", {})
            updated.setdefault("uncertainty", {})
            updated.setdefault("contradictions", [])
            updated.setdefault("contradiction_summary_lines", [])
            updated.setdefault("must_answer_questions", [])
            return updated
        updated["bull_case"] = dict(getattr(case, "bull_case", {}) or {})
        updated["bear_case"] = dict(getattr(case, "bear_case", {}) or {})
        updated["uncertainty"] = dict(getattr(case, "uncertainty", {}) or {})
        updated["contradictions"] = [dict(item) for item in getattr(case, "contradictions", []) or []]
        updated["contradiction_summary_lines"] = list(getattr(case, "contradiction_summary_lines", []) or [])
        updated["must_answer_questions"] = list(getattr(case, "must_answer_questions", []) or [])
        return updated

    @classmethod
    def _augment_summary_snapshot(cls, summary: dict, cases: list) -> dict:
        case_lookup = {item.case_id: item for item in cases}
        updated = dict(summary)
        for group in ("candidate_pool", "selected", "watchlist", "rejected"):
            updated[group] = [
                cls._attach_case_evidence(item, case_lookup.get(item.get("case_id")))
                for item in (summary.get(group, []) or [])
            ]
        updated["contradicted_case_ids"] = [item.case_id for item in cases if getattr(item, "contradictions", [])]
        updated["case_contradictions"] = cls._build_case_contradictions(cases)
        updated["contradiction_summary_lines"] = cls._build_contradiction_summary_lines(cases)
        updated["controversy_summary_lines"] = cls._dedupe_texts(
            list(updated.get("contradiction_summary_lines", [])) + list(updated.get("controversy_summary_lines", []))
        )[:8]
        updated["round_2_guidance"] = cls._dedupe_texts(
            cls._build_contradiction_questions(cases) + list(updated.get("round_2_guidance", []))
        )[:8]
        updated["must_answer_questions"] = cls._build_must_answer_questions(cases)
        return updated

    @staticmethod
    def _build_must_answer_questions(cases: list, preferred_case_ids: list[str] | None = None) -> list[str]:
        case_map = {item.case_id: item for item in cases}
        ordered_cases = [case_map[case_id] for case_id in preferred_case_ids or [] if case_id in case_map]
        if not ordered_cases:
            ordered_cases = list(cases)
        questions: list[str] = []
        for item in ordered_cases:
            questions.extend(list(getattr(item, "must_answer_questions", []) or []))
            if len(questions) >= 9:
                break
        return DiscussionCycleService._dedupe_texts(questions)[:9]

    @staticmethod
    def _build_contradiction_questions(cases: list, preferred_case_ids: list[str] | None = None) -> list[str]:
        case_map = {item.case_id: item for item in cases}
        ordered_cases = [case_map[case_id] for case_id in preferred_case_ids or [] if case_id in case_map]
        if not ordered_cases:
            ordered_cases = list(cases)
        questions: list[str] = []
        for item in ordered_cases:
            for contradiction in getattr(item, "contradictions", []) or []:
                if contradiction.get("must_resolve_before_round_2"):
                    questions.append(str(contradiction.get("question") or "").strip())
            if len(questions) >= 8:
                break
        return DiscussionCycleService._dedupe_texts(questions)[:8]

    @staticmethod
    def _build_contradiction_summary_lines(cases: list, preferred_case_ids: list[str] | None = None) -> list[str]:
        case_map = {item.case_id: item for item in cases}
        ordered_cases = [case_map[case_id] for case_id in preferred_case_ids or [] if case_id in case_map]
        if not ordered_cases:
            ordered_cases = list(cases)
        lines: list[str] = []
        for item in ordered_cases:
            lines.extend(list(getattr(item, "contradiction_summary_lines", []) or []))
            if len(lines) >= 8:
                break
        return DiscussionCycleService._dedupe_texts(lines)[:8]

    @staticmethod
    def _build_case_contradictions(cases: list, preferred_case_ids: list[str] | None = None) -> list[dict]:
        case_map = {item.case_id: item for item in cases}
        ordered_cases = [case_map[case_id] for case_id in preferred_case_ids or [] if case_id in case_map]
        if not ordered_cases:
            ordered_cases = list(cases)
        payloads: list[dict] = []
        for item in ordered_cases:
            for contradiction in getattr(item, "contradictions", []) or []:
                payloads.append(
                    {
                        "case_id": item.case_id,
                        "symbol": item.symbol,
                        "name": item.name,
                        **dict(contradiction),
                    }
                )
                if len(payloads) >= 20:
                    return payloads
        return payloads

    @staticmethod
    def _dedupe_texts(items: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

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
    def _derive_blockers(summary: dict, execution_pool_case_ids: list[str] | None = None) -> list[str]:
        blockers: list[str] = []
        selected_items = summary.get("selected", []) or []
        executable_selected = [
            item
            for item in selected_items
            if item.get("risk_gate") != "reject" and item.get("audit_gate") != "hold"
        ]
        if executable_selected:
            return blockers
        if execution_pool_case_ids:
            return blockers
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

    @staticmethod
    def _should_auto_start_round_1(cycle: DiscussionCycle) -> bool:
        if cycle.discussion_state != "idle":
            return False
        if cycle.round_1_started_at:
            return False
        if not (cycle.focus_pool_case_ids or cycle.base_pool_case_ids or cycle.execution_pool_case_ids):
            return False
        return True

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
