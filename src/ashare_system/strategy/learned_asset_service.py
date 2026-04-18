"""学习产物状态流转服务。"""

from __future__ import annotations

from typing import Any

from ..logging_config import get_logger
from ..infra.audit_store import AuditStore, StateStore
from ..discussion.finalizer import build_finalize_bundle
from .atomic_repository import StrategyAssetStatus, StrategyRepository, StrategyRepositoryEntry

logger = get_logger("strategy.learned_asset_service")


class LearnedAssetService:
    """管理 Agent 学习产物的状态升级链。"""

    _ADVICE_QUEUE_KEY = "learned_asset_advice_queue"
    _ALLOWED_TRANSITIONS: dict[StrategyAssetStatus, set[StrategyAssetStatus]] = {
        "draft": {"review_required", "rejected", "archived"},
        "review_required": {"active", "rejected", "archived", "experimental"},
        "experimental": {"active", "rejected", "archived"},
        "learned": {"review_required", "experimental", "archived"},
        "active": {"archived", "experimental"},
        "rejected": {"archived"},
        "archived": set(),
    }

    def __init__(
        self,
        repository: StrategyRepository,
        state_store: StateStore | None = None,
        audit_store: AuditStore | None = None,
        candidate_case_service: Any | None = None,
    ) -> None:
        self._repository = repository
        self._state_store = state_store
        self._audit_store = audit_store
        self._candidate_case_service = candidate_case_service

    def transition(
        self,
        *,
        asset_id: str,
        version: str,
        target_status: StrategyAssetStatus,
        operator: str,
        note: str = "",
        evaluation_summary: dict[str, Any] | None = None,
        approval_context: dict[str, Any] | None = None,
    ) -> StrategyRepositoryEntry:
        entry = self._repository.get(asset_id, version)
        if entry is None:
            raise KeyError(f"未找到学习产物: {asset_id}:{version}")
        current_status = entry.status
        allowed = self._ALLOWED_TRANSITIONS.get(current_status, set())
        if target_status not in allowed:
            raise ValueError(f"不允许状态流转: {current_status} -> {target_status}")
        approval_context = dict(approval_context or {})
        self._validate_transition(target_status=target_status, approval_context=approval_context)
        merged_evaluation = dict(entry.evaluation_summary or {})
        if evaluation_summary:
            merged_evaluation.update(evaluation_summary)
        merged_evaluation.update(
            {
                "last_operator": operator,
                "last_note": note,
                "target_status": target_status,
                "approval_context": approval_context,
            }
        )
        updated = entry.model_copy(
            update={
                "status": target_status,
                "evaluation_summary": merged_evaluation,
            }
        )
        self._repository.set_status(asset_id, version, target_status)
        self._repository.update_entry(updated)
        discussion_binding = self._build_discussion_binding(approval_context)
        approval_record = {
            "asset_id": asset_id,
            "version": version,
            "from_status": current_status,
            "to_status": target_status,
            "operator": operator,
            "note": note,
            "approval_context": approval_context,
            "discussion_binding": discussion_binding,
            "evaluation_summary": merged_evaluation,
            "recorded_at": updated.updated_at,
        }
        self._append_history(approval_record)
        logger.info("学习产物流转: %s:%s %s -> %s", asset_id, version, current_status, target_status)
        return self._repository.get(asset_id, version) or updated

    def _validate_transition(self, *, target_status: StrategyAssetStatus, approval_context: dict[str, Any]) -> None:
        if target_status != "active":
            return
        required_flags = {
            "discussion_passed": "讨论结论",
            "risk_passed": "风控结论",
            "audit_passed": "审计结论",
        }
        missing = [label for key, label in required_flags.items() if not bool(approval_context.get(key))]
        if missing:
            raise ValueError(f"升级为 active 前缺少审批上下文: {','.join(missing)}")
        required_refs = {
            "discussion_case_id": "discussion_case_id",
            "risk_gate": "risk_gate",
            "audit_gate": "audit_gate",
        }
        missing_refs = [label for key, label in required_refs.items() if not str(approval_context.get(key) or "").strip()]
        if missing_refs:
            raise ValueError(f"升级为 active 前缺少正式引用: {','.join(missing_refs)}")
        if str(approval_context.get("risk_gate") or "").strip() not in {"allow", "clear", "pass"}:
            raise ValueError("升级为 active 前 risk_gate 必须为 allow/clear/pass")
        if str(approval_context.get("audit_gate") or "").strip() not in {"clear", "pass"}:
            raise ValueError("升级为 active 前 audit_gate 必须为 clear/pass")
        self._validate_case_reference(approval_context)

    def _validate_case_reference(self, approval_context: dict[str, Any]) -> None:
        if self._candidate_case_service is None:
            return
        case_id = str(approval_context.get("discussion_case_id") or "").strip()
        if not case_id:
            return
        case = self._candidate_case_service.get_case(case_id)
        if case is None:
            raise ValueError(f"discussion_case_id 不存在: {case_id}")
        actual_final_status = str(getattr(case, "final_status", "") or "").strip()
        if actual_final_status not in {"selected", "watchlist"}:
            raise ValueError(f"discussion_case_id 状态不可用于转正: {case_id} final_status={actual_final_status or 'unknown'}")
        expected_risk_gate = str(approval_context.get("risk_gate") or "").strip()
        expected_audit_gate = str(approval_context.get("audit_gate") or "").strip()
        actual_risk_gate = str(getattr(case, "risk_gate", "") or "").strip()
        actual_audit_gate = str(getattr(case, "audit_gate", "") or "").strip()
        if expected_risk_gate and actual_risk_gate != expected_risk_gate:
            raise ValueError(
                f"risk_gate 引用不匹配: case={case_id} actual={actual_risk_gate} expected={expected_risk_gate}"
            )
        if expected_audit_gate and actual_audit_gate != expected_audit_gate:
            raise ValueError(
                f"audit_gate 引用不匹配: case={case_id} actual={actual_audit_gate} expected={expected_audit_gate}"
            )

    def recent_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        if self._state_store is None:
            return []
        history = list(self._state_store.get("learned_asset_approvals", []) or [])
        return list(reversed(history[-limit:]))

    def stage_advice(
        self,
        *,
        asset_id: str,
        version: str,
        trace_id: str,
        operator: str,
        advice: dict[str, Any],
        source: str = "runtime_reconcile_outcome",
        note: str = "",
    ) -> dict[str, Any]:
        entry = self._repository.get(asset_id, version)
        if entry is None:
            raise KeyError(f"未找到学习产物: {asset_id}:{version}")
        recommended_action = str(advice.get("recommended_action") or "").strip()
        if not recommended_action:
            raise ValueError("advice.recommended_action 缺失")
        queue_item = {
            "queue_id": f"ladv-{asset_id}-{version}-{len(self.list_advice_queue(limit=500)) + 1}",
            "asset_id": asset_id,
            "version": version,
            "current_status": str(entry.status),
            "recommended_action": recommended_action,
            "suggested_target_status": self._suggest_target_status(recommended_action, current_status=str(entry.status)),
            "governance_route": self._suggest_governance_route(recommended_action),
            "operator": operator,
            "trace_id": trace_id,
            "source": source,
            "note": note,
            "status": "pending",
            "advice": dict(advice or {}),
            "recorded_at": entry.updated_at,
        }
        if self._state_store is not None:
            queue = list(self._state_store.get(self._ADVICE_QUEUE_KEY, []) or [])
            queue.append(queue_item)
            self._state_store.set(self._ADVICE_QUEUE_KEY, queue[-300:])
        if self._audit_store is not None:
            self._audit_store.append(
                category="learned_asset_advice",
                message=f"学习产物建议入队: {asset_id}:{version} -> {recommended_action}",
                payload=queue_item,
            )
        return queue_item

    def list_advice_queue(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        if self._state_store is None:
            return []
        items = list(self._state_store.get(self._ADVICE_QUEUE_KEY, []) or [])
        if status is not None:
            items = [item for item in items if str(item.get("status") or "").strip() == status]
        return list(reversed(items[-limit:]))

    def resolve_advice(
        self,
        *,
        queue_id: str,
        resolution_status: str,
        operator: str,
        note: str = "",
        apply_transition: bool = False,
        target_status: StrategyAssetStatus | None = None,
        evaluation_summary: dict[str, Any] | None = None,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._state_store is None:
            raise RuntimeError("state_store 不可用，无法处理 advice 队列")
        normalized_queue_id = str(queue_id or "").strip()
        normalized_resolution_status = str(resolution_status or "").strip()
        normalized_operator = str(operator or "").strip() or "system"
        if not normalized_queue_id:
            raise ValueError("queue_id 必填")
        allowed_resolution_statuses = {"accepted", "rejected", "applied", "dismissed"}
        if normalized_resolution_status not in allowed_resolution_statuses:
            raise ValueError(f"不支持的 resolution_status: {normalized_resolution_status}")

        queue = list(self._state_store.get(self._ADVICE_QUEUE_KEY, []) or [])
        item_index = next(
            (index for index, item in enumerate(queue) if str(item.get("queue_id") or "").strip() == normalized_queue_id),
            None,
        )
        if item_index is None:
            raise KeyError(f"未找到 advice 队列项: {normalized_queue_id}")
        current_item = dict(queue[item_index] or {})
        current_status = str(current_item.get("status") or "").strip()
        if current_status != "pending":
            raise ValueError(f"advice 队列项不可重复处理: {normalized_queue_id} status={current_status or 'unknown'}")

        if apply_transition and target_status is None:
            raise ValueError("apply_transition=true 时 target_status 必填")

        transition_item = None
        if apply_transition:
            transition_item = self.transition(
                asset_id=str(current_item.get("asset_id") or "").strip(),
                version=str(current_item.get("version") or "v1").strip(),
                target_status=target_status,  # type: ignore[arg-type]
                operator=normalized_operator,
                note=note or f"由 advice 队列 {normalized_queue_id} 显式落地",
                evaluation_summary=evaluation_summary,
                approval_context=approval_context,
            )

        updated_item = {
            **current_item,
            "status": normalized_resolution_status,
            "resolution_note": note,
            "resolved_by": normalized_operator,
            "resolved_at": transition_item.updated_at if transition_item is not None else current_item.get("recorded_at"),
            "apply_transition": apply_transition,
            "applied_target_status": str(target_status or "").strip() or None,
            "transition_applied": transition_item is not None,
            "transition_result": transition_item.model_dump() if transition_item is not None else None,
            "approval_context": dict(approval_context or {}),
            "evaluation_summary": dict(evaluation_summary or {}),
        }
        queue[item_index] = updated_item
        self._state_store.set(self._ADVICE_QUEUE_KEY, queue[-300:])
        if self._audit_store is not None:
            self._audit_store.append(
                category="learned_asset_advice_resolution",
                message=(
                    f"学习产物建议处理: {updated_item['asset_id']}:{updated_item['version']} "
                    f"status={normalized_resolution_status} apply_transition={apply_transition}"
                ),
                payload=updated_item,
            )
        return updated_item

    def build_panel(self, limit: int = 20) -> dict[str, Any]:
        items = [
            entry.model_dump()
            for entry in self._repository.list_entries()
            if entry.type in {"learned_combo", "template", "playbook"}
            and entry.status in {"draft", "review_required", "experimental", "active", "rejected"}
        ]
        approvals = self.recent_approvals(limit=limit)
        waiting_review = [item for item in items if item.get("status") == "review_required"]
        active_items = [item for item in items if item.get("status") == "active"]
        linked_case_count = sum(
            1 for item in approvals if (item.get("discussion_binding") or {}).get("case_id")
        )
        return {
            "summary": {
                "total": len(items),
                "waiting_review_count": len(waiting_review),
                "active_count": len(active_items),
                "recent_approval_count": len(approvals),
                "linked_case_count": linked_case_count,
                "advice_queue_count": len(self.list_advice_queue(limit=500)),
            },
            "items": items[:limit],
            "recent_approvals": approvals,
            "advice_queue": self.list_advice_queue(limit=limit),
        }

    @staticmethod
    def _suggest_target_status(recommended_action: str, *, current_status: str) -> str:
        if recommended_action == "promote_active":
            return "active"
        if recommended_action == "review_required":
            return "review_required"
        if recommended_action == "keep_review":
            return "review_required"
        if recommended_action == "maintain_active":
            return current_status
        return current_status

    @staticmethod
    def _suggest_governance_route(recommended_action: str) -> str:
        if recommended_action == "promote_active":
            return "promotion_candidate"
        if recommended_action == "review_required":
            return "rollback_review"
        if recommended_action == "keep_review":
            return "review_queue"
        if recommended_action == "maintain_active":
            return "maintain_review"
        return "observe_only"

    def _build_discussion_binding(self, approval_context: dict[str, Any]) -> dict[str, Any]:
        if self._candidate_case_service is None:
            return {}
        case_id = str(approval_context.get("discussion_case_id") or "").strip()
        if not case_id:
            return {}
        case = self._candidate_case_service.get_case(case_id)
        if case is None:
            return {}
        trade_date = str(getattr(case, "trade_date", "") or "").strip()
        summary = {}
        if trade_date:
            try:
                summary = self._candidate_case_service.build_trade_date_summary(trade_date)
            except Exception:
                summary = {}
        reason_board = {}
        vote_detail = {}
        if trade_date:
            try:
                reason_board = self._candidate_case_service.build_reason_board(trade_date)
            except Exception:
                reason_board = {}
            try:
                vote_detail = self._candidate_case_service.build_case_vote_detail(case_id) or {}
            except Exception:
                vote_detail = {}
        reply_pack = {}
        if trade_date:
            try:
                reply_pack = self._candidate_case_service.build_reply_pack(trade_date)
            except Exception:
                reply_pack = {}
        final_brief = {}
        if trade_date and hasattr(self._candidate_case_service, "build_final_brief"):
            try:
                final_brief = self._candidate_case_service.build_final_brief(trade_date)
            except Exception:
                final_brief = {}
        finalize_packet = {}
        if trade_date:
            try:
                if hasattr(self._candidate_case_service, "build_finalize_bundle"):
                    finalize_bundle = self._candidate_case_service.build_finalize_bundle(trade_date)
                    finalize_packet = dict((finalize_bundle or {}).get("finalize_packet") or {})
                elif hasattr(self._candidate_case_service, "list_cases"):
                    finalize_bundle = build_finalize_bundle(
                        trade_date=trade_date,
                        cases=[
                            item.model_dump() if hasattr(item, "model_dump") else dict(item)
                            for item in self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                        ],
                        include_client_brief=False,
                    ).model_dump()
                    finalize_packet = dict((finalize_bundle or {}).get("finalize_packet") or {})
            except Exception:
                finalize_packet = {}
        return {
            "case_id": case_id,
            "trade_date": trade_date,
            "symbol": str(getattr(case, "symbol", "") or "").strip(),
            "final_status": str(getattr(case, "final_status", "") or "").strip(),
            "risk_gate": str(getattr(case, "risk_gate", "") or "").strip(),
            "audit_gate": str(getattr(case, "audit_gate", "") or "").strip(),
            "trade_date_summary": {
                "selected_count": summary.get("selected_count"),
                "watchlist_count": summary.get("watchlist_count"),
                "rejected_count": summary.get("rejected_count"),
            },
            "reason_board_summary": {
                "selected_count": reason_board.get("selected_count"),
                "watchlist_count": reason_board.get("watchlist_count"),
                "rejected_count": reason_board.get("rejected_count"),
            },
            "vote_detail": {
                "headline_line": vote_detail.get("headline_line"),
                "agent_vote_count": len(vote_detail.get("agent_votes", []) or []),
            },
            "reply_pack_summary": {
                "selected_count": reply_pack.get("selected_count"),
                "watchlist_count": reply_pack.get("watchlist_count"),
                "rejected_count": reply_pack.get("rejected_count"),
                "overview_line": ((reply_pack.get("overview_lines") or [""])[0] if reply_pack else ""),
            },
            "final_brief_summary": {
                "status": final_brief.get("status"),
                "selected_count": final_brief.get("selected_count"),
                "watchlist_count": final_brief.get("watchlist_count"),
                "rejected_count": final_brief.get("rejected_count"),
                "blockers": final_brief.get("blockers", []),
                "headline_line": ((final_brief.get("lines") or [""])[0] if final_brief else ""),
            },
            "finalize_packet_summary": {
                "status": finalize_packet.get("status"),
                "blocked": finalize_packet.get("blocked"),
                "selected_case_count": len(finalize_packet.get("selected_case_ids", []) or []),
                "execution_intent_count": ((finalize_packet.get("execution_intents") or {}).get("intent_count")),
                "summary_line": ((finalize_packet.get("summary_lines") or [""])[0] if finalize_packet else ""),
            },
        }

    def _append_history(self, record: dict[str, Any]) -> None:
        if self._state_store is not None:
            history = list(self._state_store.get("learned_asset_approvals", []) or [])
            history.append(record)
            self._state_store.set("learned_asset_approvals", history[-200:])
        if self._audit_store is not None:
            self._audit_store.append(
                category="learned_asset_transition",
                message=(
                    f"学习产物流转: {record['asset_id']}:{record['version']} "
                    f"{record['from_status']} -> {record['to_status']}"
                ),
                payload=record,
            )

    def auto_discover_from_discussion(self, trade_date: str, finalize_packet: dict[str, Any]) -> list[str]:
        """从讨论终包中自动发现并注册 draft 学习产物。"""
        if not finalize_packet or not finalize_packet.get("selected_case_ids"):
            return []
        
        discovered_ids = []
        execution_intents = finalize_packet.get("execution_intents", {})
        intents = list(execution_intents.get("intents") or [])
        
        # 如果有选出的票且有明确执行意图，尝试提取战法与因子组合
        if intents:
            # 提取所有意图共有的 playbook 或核心参数
            playbooks = list({it.get("playbook") for it in intents if it.get("playbook")})
            if playbooks:
                asset_id = f"auto-combo-{trade_date}-{playbooks[0]}"
                if self._repository.get(asset_id) is None:
                    content = {
                        "playbook_weights": {pb: 1.0/len(playbooks) for pb in playbooks},
                        "score_bonus": 0.5,
                        "context_note": f"自动从 {trade_date} 讨论中提取，涉及战法: {','.join(playbooks)}",
                    }
                    entry = self._repository.submit_learned_entry(
                        asset_id=asset_id,
                        name=f"自动发现组合-{trade_date}",
                        asset_type="learned_combo",
                        author="system:auto_discovery",
                        source=f"discussion:{trade_date}",
                        content=content,
                        tags=["auto_discovered", "discussion_driven", trade_date],
                        risk_notes=["自动发现产物，需经过实盘观察"],
                    )
                    discovered_ids.append(entry.id)
                    logger.info("自动发现学习产物(讨论): %s", entry.id)
        
        return discovered_ids

    def auto_discover_from_attribution(self, trade_date: str, report: Any) -> list[str]:
        """从归因报告中自动发现并注册 draft 学习产物。"""
        if not report or not getattr(report, "available", False):
            return []
        
        discovered_ids = []
        # 寻找表现优异的战法 (R0.5 逻辑: 胜率 > 70% 且样本数 >= 2)
        for bucket in getattr(report, "by_playbook", []):
            if bucket.trade_count >= 2 and bucket.win_rate >= 0.7:
                asset_id = f"auto-boost-{trade_date}-{bucket.key}"
                if self._repository.get(asset_id) is None:
                    content = {
                        "playbook_weights": {bucket.key: 0.2}, # 给予 20% 的权重增益
                        "score_bonus": 1.0,
                        "context_note": f"自动从 {trade_date} 归因中提取，{bucket.key} 表现优异(胜率{bucket.win_rate:.0%})",
                    }
                    entry = self._repository.submit_learned_entry(
                        asset_id=asset_id,
                        name=f"自动优选加成-{bucket.key}-{trade_date}",
                        asset_type="learned_combo",
                        author="system:auto_discovery",
                        source=f"attribution:{trade_date}",
                        content=content,
                        tags=["auto_discovered", "attribution_driven", bucket.key],
                    )
                    discovered_ids.append(entry.id)
                    logger.info("自动发现学习产物(归因): %s", entry.id)
        
        return discovered_ids

    def auto_promote_assets(self, operator: str = "system:auto_promotion") -> list[str]:
        """自动扫描并流转符合条件的资产。"""
        promoted = []
        entries = self._repository.list_entries()
        
        # R0.10: 性能驱动的自动流转
        evaluations = []
        if self._state_store is not None:
            evaluations = list(self._state_store.get("compose_evaluations", []) or [])
            
        for entry in entries:
            # 1. draft -> review_required: 如果已有关联讨论 case (source 包含 discussion)
            if entry.status == "draft" and "discussion" in entry.source:
                try:
                    self.transition(
                        asset_id=entry.id,
                        version=entry.version,
                        target_status="review_required",
                        operator=operator,
                        note="自动流转: 讨论驱动的草案进入待审状态",
                    )
                    promoted.append(f"{entry.id}:review_required")
                except Exception:
                    continue
            
            # 2. review_required -> experimental: 如果回测证明优异 (R0.10)
            if entry.status == "review_required":
                # 查找该资产的回测记录
                asset_evals = [
                    e.get("outcome", {}).get("posterior_metrics", {}).get("backtest_metrics", {})
                    for e in evaluations if entry.id in set(e.get("active_learned_asset_ids", []) or [])
                ]
                # 过滤空回测
                valid_bt = [m for m in asset_evals if m.get("win_rate") is not None]
                if valid_bt and valid_bt[0].get("win_rate", 0) > 0.6:
                    try:
                        self.transition(
                            asset_id=entry.id,
                            version=entry.version,
                            target_status="experimental",
                            operator=operator,
                            note=f"自动流转: 回测胜率({valid_bt[0]['win_rate']:.0%})优异进入实验分区",
                        )
                        promoted.append(f"{entry.id}:experimental")
                    except Exception:
                        continue
            
            # 3. 兜底: review_required -> experimental (归因驱动)
            if entry.status == "review_required" and "attribution" in entry.source:
                try:
                    self.transition(
                        asset_id=entry.id,
                        version=entry.version,
                        target_status="experimental",
                        operator=operator,
                        note="自动流转: 归因驱动产物进入实验分区",
                    )
                    promoted.append(f"{entry.id}:experimental")
                except Exception:
                    continue
                    
        return promoted
