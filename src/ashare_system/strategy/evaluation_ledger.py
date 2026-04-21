"""Compose 评估账本服务。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import sqrt
from typing import TYPE_CHECKING, Any, Callable

from ..infra.audit_store import AuditStore, StateStore
from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..discussion.candidate_case import CandidateCaseService
    from ..learning.attribution import TradeAttributionService
    from ..learning.registry_updater import RegistryUpdater
    from ..learning.score_state import AgentScoreService
    from .learned_asset_service import LearnedAssetService
    from .nightly_sandbox import NightlySandbox

logger = get_logger("strategy.evaluation_ledger")


class EvaluationLedgerService:
    """记录 runtime compose 的调用、提案与后续反馈。"""

    _STATE_KEY = "compose_evaluations"
    _MAX_HISTORY = 300

    def __init__(
        self,
        state_store: StateStore | None = None,
        audit_store: AuditStore | None = None,
        meeting_state_store: StateStore | None = None,
        candidate_case_service: CandidateCaseService | None = None,
        trade_attribution_service: TradeAttributionService | None = None,
        agent_score_service: AgentScoreService | None = None,
        nightly_sandbox: NightlySandbox | None = None,
        learned_asset_service: LearnedAssetService | None = None,
        registry_updater: RegistryUpdater | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._state_store = state_store
        self._audit_store = audit_store
        self._meeting_state_store = meeting_state_store
        self._candidate_case_service = candidate_case_service
        self._trade_attribution_service = trade_attribution_service
        self._agent_score_service = agent_score_service
        self._nightly_sandbox = nightly_sandbox
        self._learned_asset_service = learned_asset_service
        self._registry_updater = registry_updater
        self._now_factory = now_factory or datetime.now

    def record_compose_evaluation(
        self,
        *,
        trace_id: str,
        request_payload: dict[str, Any],
        runtime_job: dict[str, Any],
        market_summary: dict[str, Any],
        candidates: list[dict[str, Any]],
        filtered_out: list[dict[str, Any]],
        proposal_packet: dict[str, Any],
        applied_constraints: dict[str, Any],
        repository: dict[str, Any],
        generated_at: str,
        autonomy_trace: dict[str, Any] | None = None,
        retry_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "trace_id": trace_id,
            "request_id": str(request_payload.get("request_id") or "").strip(),
            "account_id": str(request_payload.get("account_id") or "").strip(),
            "generated_at": generated_at,
            "recorded_at": generated_at,
            "agent": dict(request_payload.get("agent") or {}),
            "intent": dict(request_payload.get("intent") or {}),
            "universe": dict(request_payload.get("universe") or {}),
            "strategy": dict(request_payload.get("strategy") or {}),
            "constraints": dict(request_payload.get("constraints") or {}),
            "market_context": dict(request_payload.get("market_context") or {}),
            "learned_asset_options": dict(request_payload.get("learned_asset_options") or {}),
            "runtime_job": dict(runtime_job or {}),
            "market_summary": dict(market_summary or {}),
            "applied_constraints": dict(applied_constraints or {}),
            "candidate_count": len(candidates),
            "filtered_count": len(filtered_out),
            "candidates": [self._slim_candidate_record(item) for item in list(candidates or [])],
            "filtered_out": [self._slim_filtered_record(item) for item in list(filtered_out or [])],
            "autonomy_trace": dict(autonomy_trace or {}),
            "retry_plan": dict(retry_plan or {}),
            "selected_symbols": list(proposal_packet.get("selected_symbols") or []),
            "watchlist_symbols": list(proposal_packet.get("watchlist_symbols") or []),
            "discussion_focus": list(proposal_packet.get("discussion_focus") or []),
            "repository_summary": dict((repository or {}).get("repository_summary") or {}),
            "used_asset_keys": [
                f"{str(item.get('id') or '').strip()}:{str(item.get('version') or '').strip()}"
                for item in list((repository or {}).get("used_assets") or [])
                if str(item.get("id") or "").strip() and str(item.get("version") or "").strip()
            ],
            "used_playbook_keys": [
                f"{str(item.get('id') or '').strip()}:{str(item.get('version') or '').strip()}"
                for item in list((repository or {}).get("used_assets") or [])
                if str(item.get("type") or "").strip() == "playbook"
                and str(item.get("id") or "").strip()
                and str(item.get("version") or "").strip()
            ],
            "used_factor_keys": [
                f"{str(item.get('id') or '').strip()}:{str(item.get('version') or '').strip()}"
                for item in list((repository or {}).get("used_assets") or [])
                if str(item.get("type") or "").strip() == "factor"
                and str(item.get("id") or "").strip()
                and str(item.get("version") or "").strip()
            ],
            "used_asset_ids": [
                str(item.get("id") or "")
                for item in list((repository or {}).get("used_assets") or [])
                if str(item.get("id") or "").strip()
            ],
            "learned_asset_ids": [
                str(item.get("id") or "")
                for item in list((repository or {}).get("learned_assets") or [])
                if str(item.get("id") or "").strip()
            ],
            "active_learned_asset_ids": [
                str(item.get("id") or "")
                for item in list((repository or {}).get("active_learned_assets") or [])
                if str(item.get("id") or "").strip()
            ],
            "auto_selected_learned_asset_ids": [
                str(item.get("id") or "")
                for item in list((repository or {}).get("auto_selected_learned_assets") or [])
                if str(item.get("id") or "").strip()
            ],
            "adoption": {
                "status": "pending",
                "adopted_symbols": [],
                "watchlist_symbols": [],
                "rejected_symbols": [],
                "discussion_case_ids": [],
                "trade_date": "",
                "selected_count": 0,
                "watchlist_count": 0,
                "rejected_count": 0,
                "resolved_case_count": 0,
                "missing_case_ids": [],
                "case_statuses": [],
                "risk_gate_counts": {},
                "audit_gate_counts": {},
                "note": "",
                "updated_by": "",
                "updated_at": "",
            },
            "outcome": {
                "status": "pending",
                "posterior_metrics": {},
                "note": "",
                "updated_by": "",
                "updated_at": "",
            },
        }
        self._upsert_record(record)
        self._append_audit(
            message=f"记录 compose 评估账本: {trace_id}",
            payload={
                "trace_id": trace_id,
                "request_id": record["request_id"],
                "agent_id": (record["agent"] or {}).get("agent_id"),
                "candidate_count": record["candidate_count"],
                "filtered_count": record["filtered_count"],
                "selected_symbols": record["selected_symbols"],
            },
        )
        return record

    def update_feedback(
        self,
        *,
        trace_id: str,
        adoption: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = self._read_records()
        updated: dict[str, Any] | None = None
        for index, item in enumerate(records):
            if str(item.get("trace_id") or "") != trace_id:
                continue
            next_item = dict(item)
            if adoption:
                merged_adoption = dict(item.get("adoption") or {})
                merged_adoption.update(adoption)
                next_item["adoption"] = merged_adoption
            if outcome:
                merged_outcome = dict(item.get("outcome") or {})
                merged_outcome.update(outcome)
                next_item["outcome"] = merged_outcome
            records[index] = next_item
            updated = next_item
            break
        if updated is None:
            raise KeyError(f"未找到 compose 评估记录: {trace_id}")
        self._write_records(records)
        self._append_audit(
            message=f"更新 compose 评估反馈: {trace_id}",
            payload={
                "trace_id": trace_id,
                "adoption": adoption or {},
                "outcome": outcome or {},
            },
        )
        return updated

    def get_record(self, trace_id: str) -> dict[str, Any]:
        return self._find_record(trace_id)

    def reconcile_adoption(self, *, trace_id: str, updated_by: str = "system:auto_reconcile") -> dict[str, Any]:
        if self._candidate_case_service is None:
            raise RuntimeError("candidate_case_service 未接入，无法回看 discussion case")

        record = self._find_record(trace_id)
        runtime_job = dict(record.get("runtime_job") or {})
        case_ids = self._normalize_case_ids(runtime_job.get("case_ids"))
        updated_at = self._now_factory().isoformat()
        if not case_ids:
            return self.update_feedback(
                trace_id=trace_id,
                adoption={
                    "status": "no_cases",
                    "adopted_symbols": [],
                    "watchlist_symbols": [],
                    "rejected_symbols": [],
                    "discussion_case_ids": [],
                    "trade_date": "",
                    "selected_count": 0,
                    "watchlist_count": 0,
                    "rejected_count": 0,
                    "resolved_case_count": 0,
                    "missing_case_ids": [],
                    "case_statuses": [],
                    "risk_gate_counts": {},
                    "audit_gate_counts": {},
                    "note": "runtime_job 未提供可回看的 discussion case_ids",
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                },
            )

        resolved_cases: list[Any] = []
        missing_case_ids: list[str] = []
        for case_id in case_ids:
            case = self._candidate_case_service.get_case(case_id)
            if case is None:
                missing_case_ids.append(case_id)
                continue
            resolved_cases.append(case)

        selected_symbols = [str(case.symbol) for case in resolved_cases if str(case.final_status) == "selected"]
        watchlist_symbols = [str(case.symbol) for case in resolved_cases if str(case.final_status) == "watchlist"]
        rejected_symbols = [str(case.symbol) for case in resolved_cases if str(case.final_status) == "rejected"]
        trade_dates = sorted({str(case.trade_date) for case in resolved_cases if str(case.trade_date).strip()})
        case_statuses = [
            {
                "case_id": str(case.case_id),
                "symbol": str(case.symbol),
                "trade_date": str(case.trade_date),
                "final_status": str(case.final_status),
                "risk_gate": str(case.risk_gate),
                "audit_gate": str(case.audit_gate),
            }
            for case in resolved_cases
        ]
        adoption_payload = {
            "status": self._resolve_adoption_status(
                selected_count=len(selected_symbols),
                watchlist_count=len(watchlist_symbols),
                rejected_count=len(rejected_symbols),
                resolved_case_count=len(resolved_cases),
                missing_case_ids=missing_case_ids,
            ),
            "adopted_symbols": selected_symbols,
            "watchlist_symbols": watchlist_symbols,
            "rejected_symbols": rejected_symbols,
            "discussion_case_ids": case_ids,
            "trade_date": trade_dates[0] if len(trade_dates) == 1 else "",
            "selected_count": len(selected_symbols),
            "watchlist_count": len(watchlist_symbols),
            "rejected_count": len(rejected_symbols),
            "resolved_case_count": len(resolved_cases),
            "missing_case_ids": missing_case_ids,
            "case_statuses": case_statuses,
            "risk_gate_counts": self._count_attr_values(resolved_cases, "risk_gate"),
            "audit_gate_counts": self._count_attr_values(resolved_cases, "audit_gate"),
            "note": self._build_reconcile_note(
                case_count=len(case_ids),
                resolved_case_count=len(resolved_cases),
                selected_count=len(selected_symbols),
                watchlist_count=len(watchlist_symbols),
                rejected_count=len(rejected_symbols),
                missing_case_ids=missing_case_ids,
            ),
            "updated_by": updated_by,
            "updated_at": updated_at,
        }
        updated = self.update_feedback(trace_id=trace_id, adoption=adoption_payload)
        self._append_audit(
            message=f"回看 compose 采纳状态: {trace_id}",
            payload={
                "trace_id": trace_id,
                "discussion_case_ids": case_ids,
                "adoption_status": adoption_payload["status"],
                "selected_count": adoption_payload["selected_count"],
                "watchlist_count": adoption_payload["watchlist_count"],
                "rejected_count": adoption_payload["rejected_count"],
                "missing_case_ids": missing_case_ids,
            },
        )
        return updated

    def reconcile_outcome(
        self,
        *,
        trace_id: str,
        account_id: str | None = None,
        sync_registry_weights: bool = False,
        updated_by: str = "system:auto_outcome",
    ) -> dict[str, Any]:
        if self._meeting_state_store is None:
            raise RuntimeError("meeting_state_store 未接入，无法回看执行后验")

        record = self._find_record(trace_id)
        tracked_symbols = self._resolve_target_symbols(record)
        resolved_account_id = str(account_id or record.get("account_id") or "").strip()
        updated_at = self._now_factory().isoformat()
        latest = dict(self._meeting_state_store.get("latest_execution_reconciliation", {}) or {})
        if not latest:
            return self.update_feedback(
                trace_id=trace_id,
                outcome={
                    "status": "no_reconciliation",
                    "posterior_metrics": {
                        "tracked_symbol_count": len(tracked_symbols),
                        "matched_symbol_count": 0,
                        "filled_symbol_count": 0,
                        "matched_order_count": 0,
                        "filled_order_count": 0,
                        "filled_quantity": 0,
                        "filled_value": 0.0,
                    },
                    "account_id": resolved_account_id,
                    "reconciled_at": "",
                    "reconciliation_status": "missing",
                    "target_symbols": tracked_symbols,
                    "matched_symbols": [],
                    "filled_symbols": [],
                    "pending_symbols": tracked_symbols,
                    "items": [],
                    "attribution_items": [],
                    "note": "当前尚无 latest_execution_reconciliation 记录",
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                },
            )

        reconciliation_account_id = str(latest.get("account_id") or "").strip()
        if resolved_account_id and reconciliation_account_id and reconciliation_account_id != resolved_account_id:
            return self.update_feedback(
                trace_id=trace_id,
                outcome={
                    "status": "account_mismatch",
                    "posterior_metrics": {
                        "tracked_symbol_count": len(tracked_symbols),
                        "matched_symbol_count": 0,
                        "filled_symbol_count": 0,
                        "matched_order_count": 0,
                        "filled_order_count": 0,
                        "filled_quantity": 0,
                        "filled_value": 0.0,
                    },
                    "account_id": resolved_account_id,
                    "reconciled_at": str(latest.get("reconciled_at") or ""),
                    "reconciliation_status": str(latest.get("status") or "unknown"),
                    "target_symbols": tracked_symbols,
                    "matched_symbols": [],
                    "filled_symbols": [],
                    "pending_symbols": tracked_symbols,
                    "items": [],
                    "attribution_items": [],
                    "note": f"latest_execution_reconciliation 属于账户 {reconciliation_account_id}，与请求账户不一致",
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                },
            )

        tracked_symbol_set = set(tracked_symbols)
        reconciliation_items = [
            dict(item)
            for item in list(latest.get("items") or [])
            if not tracked_symbol_set or str(item.get("symbol") or "") in tracked_symbol_set
        ]
        matched_symbols = list(
            dict.fromkeys(str(item.get("symbol") or "") for item in reconciliation_items if str(item.get("symbol") or "").strip())
        )
        filled_items = [
            item for item in reconciliation_items if int(item.get("filled_quantity", 0) or 0) > 0
        ]
        filled_symbols = list(
            dict.fromkeys(str(item.get("symbol") or "") for item in filled_items if str(item.get("symbol") or "").strip())
        )
        pending_symbols = [symbol for symbol in tracked_symbols if symbol not in set(filled_symbols)]
        attribution_items = [
            dict(item)
            for item in list(((latest.get("attribution") or {}).get("items") or []))
            if not tracked_symbol_set or str(item.get("symbol") or "") in tracked_symbol_set
        ]
        posterior_metrics = {
            "tracked_symbol_count": len(tracked_symbols),
            "matched_symbol_count": len(matched_symbols),
            "filled_symbol_count": len(filled_symbols),
            "matched_order_count": len(reconciliation_items),
            "filled_order_count": len(filled_items),
            "filled_quantity": sum(int(item.get("filled_quantity", 0) or 0) for item in filled_items),
            "filled_value": round(sum(float(item.get("filled_value", 0.0) or 0.0) for item in filled_items), 4),
            "orphan_trade_count": int(latest.get("orphan_trade_count", 0) or 0),
            "position_count": int(latest.get("position_count", 0) or 0),
            "trade_count": int(latest.get("trade_count", 0) or 0),
        }
        if attribution_items:
            returns = [float(item.get("next_day_close_pct", 0.0) or 0.0) for item in attribution_items]
            posterior_metrics.update(
                {
                    "attribution_count": len(attribution_items),
                    "avg_next_day_close_pct": round(sum(returns) / len(returns), 6),
                    "positive_count": sum(1 for value in returns if value > 0),
                    "negative_count": sum(1 for value in returns if value < 0),
                    "flat_count": sum(1 for value in returns if value == 0),
                }
            )
        trade_date = self._resolve_trade_date(record, latest)
        score_date = str(latest.get("reconciled_at") or "")[:10] if str(latest.get("reconciled_at") or "") else ""
        agent_score_settlement = self._build_agent_score_settlement(
            record=record,
            attribution_items=attribution_items,
            trade_date=trade_date,
            score_date=score_date or trade_date,
        )
        learning_bridge = self._build_learning_bridge(
            record=record,
            tracked_symbols=tracked_symbols,
            trade_date=trade_date,
            score_date=score_date,
            sync_registry_weights=sync_registry_weights,
        )
        learning_summary = dict(learning_bridge.get("summary") or {})
        if learning_summary:
            posterior_metrics.update(
                {
                    "bridge_trade_count": int(learning_summary.get("trade_count", 0) or 0),
                    "bridge_targeted_trade_count": int(learning_summary.get("targeted_trade_count", 0) or 0),
                    "bridge_targeted_win_rate": float(learning_summary.get("targeted_win_rate", 0.0) or 0.0),
                    "bridge_parameter_hint_count": int(learning_summary.get("parameter_hint_count", 0) or 0),
                    "bridge_priority_hit_count": int(learning_summary.get("nightly_priority_hit_count", 0) or 0),
                    "bridge_active_learned_asset_count": int(learning_summary.get("active_learned_asset_count", 0) or 0),
                    "bridge_registry_synced": bool(learning_summary.get("registry_synced", False)),
                }
            )

        outcome_payload = {
            "status": self._resolve_outcome_status(
                reconciliation_status=str(latest.get("status") or "unknown"),
                tracked_symbol_count=len(tracked_symbols),
                filled_symbol_count=len(filled_symbols),
                matched_symbol_count=len(matched_symbols),
            ),
            "posterior_metrics": posterior_metrics,
            "account_id": resolved_account_id or reconciliation_account_id,
            "reconciled_at": str(latest.get("reconciled_at") or ""),
            "reconciliation_status": str(latest.get("status") or "unknown"),
            "target_symbols": tracked_symbols,
            "matched_symbols": matched_symbols,
            "filled_symbols": filled_symbols,
            "pending_symbols": pending_symbols,
            "items": reconciliation_items,
            "attribution_items": attribution_items,
            "learning_bridge": learning_bridge,
            "agent_score_settlement": agent_score_settlement,
            "note": self._build_outcome_note(
                tracked_symbol_count=len(tracked_symbols),
                matched_symbol_count=len(matched_symbols),
                filled_symbol_count=len(filled_symbols),
                attribution_count=len(attribution_items),
                reconciliation_status=str(latest.get("status") or "unknown"),
            ),
            "updated_by": updated_by,
            "updated_at": updated_at,
        }
        updated = self.update_feedback(trace_id=trace_id, outcome=outcome_payload)
        self._append_audit(
            message=f"回看 compose 后验结果: {trace_id}",
            payload={
                "trace_id": trace_id,
                "account_id": outcome_payload["account_id"],
                "outcome_status": outcome_payload["status"],
                "tracked_symbols": tracked_symbols,
                "matched_symbols": matched_symbols,
                "filled_symbols": filled_symbols,
            },
        )
        return updated

    def _build_agent_score_settlement(
        self,
        *,
        record: dict[str, Any],
        attribution_items: list[dict[str, Any]],
        trade_date: str,
        score_date: str,
    ) -> dict[str, Any]:
        if self._agent_score_service is None or self._candidate_case_service is None:
            return {"available": False, "applied": False, "reason": "score_or_case_service_missing"}
        if not attribution_items:
            return {"available": False, "applied": False, "reason": "missing_attribution_items"}
        if not score_date:
            return {"available": False, "applied": False, "reason": "missing_score_date"}

        outcome_state = dict(record.get("outcome") or {})
        previous_settlement = dict(outcome_state.get("agent_score_settlement") or {})
        settlement_key = f"{record.get('trace_id')}:{score_date}:{len(attribution_items)}"
        if previous_settlement.get("settlement_key") == settlement_key and previous_settlement.get("applied"):
            return {
                "available": True,
                "applied": False,
                "reason": "already_applied",
                "settlement_key": settlement_key,
                "score_date": score_date,
            }

        case_ids = self._normalize_case_ids((record.get("runtime_job") or {}).get("case_ids"))
        cases: list[Any] = []
        for case_id in case_ids:
            case = self._candidate_case_service.get_case(case_id)
            if case is not None:
                cases.append(case)
        if not cases and trade_date:
            tracked_symbols = {str(item.get("symbol") or "").strip() for item in attribution_items if str(item.get("symbol") or "").strip()}
            cases = [
                case
                for case in self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                if str(case.symbol) in tracked_symbols
            ]
        if not cases:
            return {"available": False, "applied": False, "reason": "missing_cases", "settlement_key": settlement_key}

        from ..learning.settlement import AgentScoreSettlementService, SettlementSymbolOutcome

        outcome_map = {
            str(item.get("symbol") or "").strip(): SettlementSymbolOutcome(
                symbol=str(item.get("symbol") or "").strip(),
                next_day_close_pct=float(item.get("next_day_close_pct", 0.0) or 0.0),
                note=str(item.get("note") or ""),
            )
            for item in attribution_items
            if str(item.get("symbol") or "").strip()
        }
        settlement_results = AgentScoreSettlementService().settle(
            cases,
            outcome_map,
            min_sample_count=1,
        )
        if not settlement_results:
            return {"available": False, "applied": False, "reason": "empty_settlement_results", "settlement_key": settlement_key}

        persisted_states = self._agent_score_service.run_daily_settlement(
            settlement_results=[
                {
                    "agent_id": item.agent_id,
                    "result_score_delta": item.result_score_delta,
                    "cases_evaluated": item.cases_evaluated,
                    "confidence_tier": item.confidence_tier,
                    "settlement_key": settlement_key,
                }
                for item in settlement_results
            ],
            trade_date=score_date,
        )
        return {
            "available": True,
            "applied": True,
            "score_date": score_date,
            "settlement_key": settlement_key,
            "agent_count": len(settlement_results),
            "state_count": len(persisted_states),
            "results": [
                {
                    "agent_id": item.agent_id,
                    "result_score_delta": item.result_score_delta,
                    "cases_evaluated": item.cases_evaluated,
                    "confidence_tier": item.confidence_tier,
                    "sample_count": item.sample_count,
                    "insufficient_sample": item.insufficient_sample,
                }
                for item in settlement_results
            ],
        }

    def reconcile_backtest(
        self,
        *,
        trace_id: str,
        price_data: dict[str, Any], # {symbol: pd.DataFrame}
        config_overrides: dict[str, Any] | None = None,
        updated_by: str = "system:auto_backtest",
    ) -> dict[str, Any]:
        """对 compose 记录运行自动离线回测，并将指标填入 outcome。"""
        from ..backtest.engine import BacktestEngine, BacktestConfig
        import pandas as pd

        record = self._find_record(trace_id)
        symbols = self._resolve_target_symbols(record)
        if not symbols:
            return record

        # 构建信号 DataFrame：只对主选票发出买入信号，watchlist 不强制入场。
        generated_at = pd.Timestamp(record["generated_at"]).strftime("%Y-%m-%d")
        selected_symbols = [str(item) for item in list(record.get("selected_symbols") or []) if str(item).strip()]
        target_symbols = selected_symbols or symbols[: min(len(symbols), 5)]
        signals = pd.DataFrame(index=[generated_at], columns=target_symbols)
        for symbol in target_symbols:
            signals.loc[generated_at, symbol] = "BUY"
            
        # 运行回测
        bt_config = BacktestConfig(**(config_overrides or {}))
        engine = BacktestEngine(bt_config)
        result = engine.run(signals, price_data)
        
        metrics_dict = {
            "total_return": round(result.metrics.total_return, 6),
            "sharpe_ratio": round(result.metrics.sharpe_ratio, 4),
            "max_drawdown": round(result.metrics.max_drawdown, 6),
            "win_rate": round(result.metrics.win_rate, 4),
            "trade_count": result.metrics.trade_count,
        }
        
        outcome_patch = {
            "status": "backtest_completed",
            "posterior_metrics": {
                **record.get("outcome", {}).get("posterior_metrics", {}),
                "backtest_metrics": metrics_dict,
                "backtest_trades": [{k: v for k, v in t.items() if k != "pnl"} for t in result.trades[:10]],
            },
            "reconciled_at": self._now_factory().isoformat(),
            "updated_by": updated_by,
            "updated_at": self._now_factory().isoformat(),
        }
        
        return self.update_feedback(trace_id=trace_id, outcome=outcome_patch)

    def reconcile_factor_performance(
        self,
        *,
        trace_id: str,
        price_data: dict[str, Any],
        updated_by: str = "system:factor_evaluator",
    ) -> dict[str, Any]:
        """评估本次 compose 中各因子的预测效力 (Rank IC)。"""
        import math
        import pandas as pd

        record = self._find_record(trace_id)
        candidates = list(record.get("candidates") or [])
        if not candidates:
            return record

        # 1. 提取因子值矩阵
        symbols = [c["symbol"] for c in candidates]
        factor_names = set()
        for c in candidates:
            factor_names.update((c.get("factor_scores") or {}).keys())
        
        if not factor_names:
            return record

        # 2. 计算下期收益率 (T+1)
        returns_map = {}
        generated_date = pd.Timestamp(record["generated_at"]).strftime("%Y-%m-%d")
        for s in symbols:
            df = price_data.get(s)
            if df is not None and generated_date in df.index:
                try:
                    idx = df.index.get_loc(generated_date)
                    if idx + 1 < len(df):
                        # 下一日收益率
                        ret = (df["close"].iloc[idx + 1] - df["close"].iloc[idx]) / df["close"].iloc[idx]
                        returns_map[s] = ret
                except Exception:
                    continue
        
        if not returns_map:
            return record

        # 3. 计算每个因子的 Rank IC
        factor_performance = {}
        returns_series = pd.Series(returns_map)
        
        for f_name in factor_names:
            f_values = pd.Series({c["symbol"]: (c.get("factor_scores") or {}).get(f_name, 0.0) for c in candidates})
            f_values = f_values.reindex(returns_series.index)
            
            sample_count = int(len(f_values.dropna()))
            if sample_count > 2:
                rank_ic = f_values.rank().corr(returns_series.rank())
                if rank_ic is not None and not math.isnan(float(rank_ic)):
                    t_stat = abs(float(rank_ic)) * math.sqrt(max(sample_count - 2, 1)) / math.sqrt(max(1.0 - float(rank_ic) ** 2, 1e-9))
                    p_value = max(min(math.erfc(t_stat / math.sqrt(2.0)), 1.0), 0.0)
                    factor_performance[f_name] = {
                        "rank_ic": round(float(rank_ic), 4),
                        "p_value": round(p_value, 4),
                        "sample_count": sample_count,
                        "significant": bool(p_value <= 0.1),
                    }

        outcome_patch = {
            "posterior_metrics": {
                **record.get("outcome", {}).get("posterior_metrics", {}),
                "factor_rank_ic": factor_performance,
            },
            "updated_at": self._now_factory().isoformat(),
            "updated_by": updated_by,
        }
        
        return self.update_feedback(trace_id=trace_id, outcome=outcome_patch)

    def list_records(self, limit: int = 20, agent_id: str | None = None) -> list[dict[str, Any]]:
        records = list(reversed(self._read_records()))
        if agent_id:
            records = [item for item in records if str((item.get("agent") or {}).get("agent_id") or "") == agent_id]
        return records[: max(limit, 1)]

    def build_panel(self, limit: int = 20) -> dict[str, Any]:
        records = self._read_records()
        items = list(reversed(records))[: max(limit, 1)]
        adopted_count = sum(1 for item in items if str((item.get("adoption") or {}).get("status") or "") == "adopted")
        settled_count = sum(1 for item in items if str((item.get("outcome") or {}).get("status") or "") == "settled")
        factor_ledger = self._build_factor_ledger(records, limit=max(limit, 1))
        playbook_ledger = self._build_playbook_ledger(records, limit=max(limit, 1))
        compose_combo_ledger = self._build_compose_combo_ledger(records, limit=max(limit, 1))
        unsupported_factor_ledger_count = sum(
            1
            for item in factor_ledger
            if str(item.get("monitor_mode") or "") == "outcome_attribution_only"
        )
        return {
            "summary": {
                "total": len(records),
                "recent_count": len(items),
                "adopted_count": adopted_count,
                "settled_count": settled_count,
                "factor_ledger_count": len(factor_ledger),
                "unsupported_factor_ledger_count": unsupported_factor_ledger_count,
                "playbook_ledger_count": len(playbook_ledger),
                "compose_combo_ledger_count": len(compose_combo_ledger),
            },
            "factor_ledger": factor_ledger,
            "playbook_ledger": playbook_ledger,
            "compose_combo_ledger": compose_combo_ledger,
            "items": items,
        }

    def estimate_composite_multiplier(self, lookback: int = 30) -> dict[str, Any]:
        records = self.list_records(limit=max(lookback, 1))
        candidate_samples = self._build_composite_multiplier_samples(records)
        if len(candidate_samples) < 20:
            return {
                "available": False,
                "sample_size": len(candidate_samples),
                "resolved_multiplier": 4.0,
                "source": "conservative_fallback",
                "reason": "候选级已结算样本不足 20 条，暂不做历史回归校准",
            }

        selection_values = [float(item["selection_score"]) for item in candidate_samples]
        adjustment_values = [float(item["composite_adjustment"]) for item in candidate_samples]
        return_values = [float(item["realized_return"]) for item in candidate_samples]
        selection_slope = self._regression_slope(selection_values, return_values)
        adjustment_slope = self._regression_slope(adjustment_values, return_values)
        if abs(selection_slope) <= 1e-9 or self._variance(adjustment_values) <= 1e-9:
            return {
                "available": False,
                "sample_size": len(candidate_samples),
                "resolved_multiplier": 4.0,
                "source": "conservative_fallback",
                "reason": "样本中的 selection_score 或 composite_adjustment 变化不足，无法做稳定校准",
            }

        raw_multiplier = adjustment_slope / selection_slope
        ic = self._pearson(adjustment_values, return_values)
        selection_ic = self._pearson(selection_values, return_values)
        matched_win_rate = self._mean([1.0 if value > 0 else 0.0 for value in return_values])
        resolved_multiplier = max(1.5, min(8.0, raw_multiplier))
        return {
            "available": True,
            "sample_size": len(candidate_samples),
            "resolved_multiplier": round(resolved_multiplier, 4),
            "source": "candidate_level_return_regression",
            "raw_multiplier": round(raw_multiplier, 6),
            "selection_slope": round(selection_slope, 8),
            "adjustment_slope": round(adjustment_slope, 8),
            "adjustment_ic": round(ic, 6),
            "selection_ic": round(selection_ic, 6),
            "avg_realized_return": round(self._mean(return_values), 6),
            "matched_win_rate": round(matched_win_rate, 4),
        }

    def _build_factor_ledger(self, records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        ledgers: dict[str, dict[str, Any]] = {}
        recent_trade_dates = self._recent_trade_dates(records)
        for record in records:
            factor_specs = self._normalize_factor_specs(record)
            used_keys = list(dict.fromkeys(list(record.get("used_factor_keys") or []) or [item["key"] for item in factor_specs]))
            if not used_keys:
                continue
            factor_meta = {item["key"]: item for item in factor_specs}
            phase = self._resolve_market_phase_label(record)
            return_metric = self._extract_return_metric(record)
            risk_metric = self._extract_risk_metric(record)
            trade_date = self._resolve_trade_date_label(record)
            success_score = self._extract_effective_score(record, return_metric=return_metric)
            hit_symbols = self._resolve_hit_symbols(record)
            adoption_status = str((record.get("adoption") or {}).get("status") or "pending")
            outcome_status = str((record.get("outcome") or {}).get("status") or "pending")

            for key in used_keys:
                meta = factor_meta.get(key, self._fallback_factor_meta(key))
                ledger = ledgers.setdefault(
                    key,
                    {
                        "key": key,
                        "factor_id": meta["id"],
                        "version": meta["version"],
                        "group": meta["group"],
                        "name": meta["name"],
                        "monitor_mode_breakdown": Counter(),
                        "monitor_status_breakdown": Counter(),
                        "usage_count": 0,
                        "hit_symbols": set(),
                        "market_phase_breakdown": Counter(),
                        "adoption_breakdown": Counter(),
                        "outcome_breakdown": Counter(),
                        "weighted_returns": [],
                        "weighted_risks": [],
                        "weight_samples": [],
                        "return_samples": [],
                        "settled_returns": [],
                        "positive_outcomes": [],
                        "filled_flags": [],
                        "effectiveness_events": [],
                        "last_used_at": "",
                    },
                )
                weight = float(meta.get("weight", 0.0) or 0.0)
                if weight <= 0:
                    weight = 1.0 / max(len(used_keys), 1)
                ledger["usage_count"] += 1
                ledger["hit_symbols"].update(hit_symbols)
                ledger["monitor_mode_breakdown"][str(meta.get("monitor_mode") or "unknown")] += 1
                ledger["monitor_status_breakdown"][str(meta.get("monitor_status") or "unknown")] += 1
                ledger["market_phase_breakdown"][phase] += 1
                ledger["adoption_breakdown"][adoption_status] += 1
                ledger["outcome_breakdown"][outcome_status] += 1
                ledger["weighted_returns"].append(return_metric * weight)
                ledger["weighted_risks"].append(risk_metric * abs(weight))
                ledger["weight_samples"].append(weight)
                ledger["return_samples"].append(return_metric)
                if outcome_status == "settled":
                    ledger["settled_returns"].append(return_metric)
                    ledger["positive_outcomes"].append(1.0 if return_metric >= 0 else 0.0)
                    ledger["filled_flags"].append(
                        1.0
                        if float(
                            ((record.get("outcome") or {}).get("posterior_metrics") or {}).get("filled_symbol_count", 0) or 0
                        )
                        > 0
                        else 0.0
                    )
                ledger["effectiveness_events"].append({"trade_date": trade_date, "score": success_score})
                ledger["last_used_at"] = str(record.get("recorded_at") or record.get("generated_at") or ledger["last_used_at"])

        payload = []
        for ledger in ledgers.values():
            dominant_monitor_mode = "unknown"
            if ledger["monitor_mode_breakdown"]:
                dominant_monitor_mode = ledger["monitor_mode_breakdown"].most_common(1)[0][0]
            payload.append(
                {
                    "key": ledger["key"],
                    "factor_id": ledger["factor_id"],
                    "version": ledger["version"],
                    "group": ledger["group"],
                    "name": ledger["name"],
                    "monitor_mode": dominant_monitor_mode,
                    "monitor_mode_breakdown": dict(sorted(ledger["monitor_mode_breakdown"].items())),
                    "monitor_status_breakdown": dict(sorted(ledger["monitor_status_breakdown"].items())),
                    "usage_count": ledger["usage_count"],
                    "hit_symbols": sorted(ledger["hit_symbols"]),
                    "hit_symbol_count": len(ledger["hit_symbols"]),
                    "market_phase_breakdown": dict(sorted(ledger["market_phase_breakdown"].items())),
                    "return_contribution": round(sum(ledger["weighted_returns"]), 6),
                    "risk_contribution": round(sum(ledger["weighted_risks"]), 6),
                    "effectiveness": self._build_effectiveness_summary(ledger["effectiveness_events"], recent_trade_dates),
                    "correlation": round(self._pearson(ledger["weight_samples"], ledger["return_samples"]), 4),
                    "post_outcome_attribution": {
                        "settled_count": len(ledger["settled_returns"]),
                        "avg_return": round(self._mean(ledger["settled_returns"]), 6),
                        "positive_rate": round(self._mean(ledger["positive_outcomes"]), 6),
                        "filled_rate": round(self._mean(ledger["filled_flags"]), 6),
                    },
                    "adoption_breakdown": dict(sorted(ledger["adoption_breakdown"].items())),
                    "outcome_breakdown": dict(sorted(ledger["outcome_breakdown"].items())),
                    "last_used_at": ledger["last_used_at"],
                }
            )
        payload.sort(key=lambda item: (-int(item["usage_count"]), str(item["key"])))
        return payload[:limit]

    def _build_playbook_ledger(self, records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        ledgers: dict[str, dict[str, Any]] = {}
        recent_trade_dates = self._recent_trade_dates(records)
        for record in records:
            playbook_specs = self._normalize_playbook_specs(record)
            used_keys = list(dict.fromkeys(list(record.get("used_playbook_keys") or []) or [item["key"] for item in playbook_specs]))
            if not used_keys:
                continue
            playbook_meta = {item["key"]: item for item in playbook_specs}
            phase = self._resolve_market_phase_label(record)
            return_metric = self._extract_return_metric(record)
            risk_metric = self._extract_risk_metric(record)
            trade_date = self._resolve_trade_date_label(record)
            success_score = self._extract_effective_score(record, return_metric=return_metric)
            selected_count = int((record.get("adoption") or {}).get("selected_count", 0) or 0)
            watchlist_count = int((record.get("adoption") or {}).get("watchlist_count", 0) or 0)
            filled_count = int(((record.get("outcome") or {}).get("posterior_metrics") or {}).get("filled_symbol_count", 0) or 0)
            candidate_count = int(record.get("candidate_count", 0) or 0)
            failure_mode = self._extract_failure_mode(record)

            for key in used_keys:
                meta = playbook_meta.get(key, self._fallback_playbook_meta(key))
                ledger = ledgers.setdefault(
                    key,
                    {
                        "key": key,
                        "playbook_id": meta["id"],
                        "version": meta["version"],
                        "name": meta["name"],
                        "market_phases": meta["market_phases"],
                        "usage_count": 0,
                        "market_phase_breakdown": Counter(),
                        "selection_rates": [],
                        "preview_pass_flags": [],
                        "live_trigger_flags": [],
                        "returns": [],
                        "drawdowns": [],
                        "failure_modes": Counter(),
                        "effectiveness_events": [],
                        "last_used_at": "",
                    },
                )
                ledger["usage_count"] += 1
                ledger["market_phase_breakdown"][phase] += 1
                ledger["selection_rates"].append(selected_count / max(candidate_count, 1))
                ledger["preview_pass_flags"].append(1.0 if selected_count > 0 or watchlist_count > 0 else 0.0)
                ledger["live_trigger_flags"].append(1.0 if filled_count > 0 else 0.0)
                ledger["returns"].append(return_metric)
                ledger["drawdowns"].append(risk_metric)
                if failure_mode:
                    ledger["failure_modes"][failure_mode] += 1
                ledger["effectiveness_events"].append({"trade_date": trade_date, "score": success_score})
                ledger["last_used_at"] = str(record.get("recorded_at") or record.get("generated_at") or ledger["last_used_at"])

        payload = []
        for ledger in ledgers.values():
            payload.append(
                {
                    "key": ledger["key"],
                    "playbook_id": ledger["playbook_id"],
                    "version": ledger["version"],
                    "name": ledger["name"],
                    "market_phases": list(ledger["market_phases"]),
                    "usage_count": ledger["usage_count"],
                    "selection_rate": round(self._mean(ledger["selection_rates"]), 6),
                    "preview_pass_rate": round(self._mean(ledger["preview_pass_flags"]), 6),
                    "live_trigger_rate": round(self._mean(ledger["live_trigger_flags"]), 6),
                    "avg_return": round(self._mean(ledger["returns"]), 6),
                    "avg_drawdown": round(self._mean(ledger["drawdowns"]), 6),
                    "market_phase_breakdown": dict(sorted(ledger["market_phase_breakdown"].items())),
                    "recent_failure_modes": [item[0] for item in ledger["failure_modes"].most_common(3)],
                    "effectiveness": self._build_effectiveness_summary(ledger["effectiveness_events"], recent_trade_dates),
                    "last_used_at": ledger["last_used_at"],
                }
            )
        payload.sort(key=lambda item: (-int(item["usage_count"]), str(item["key"])))
        return payload[:limit]

    def _build_compose_combo_ledger(self, records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        ledgers: dict[str, dict[str, Any]] = {}
        for record in records:
            factor_specs = self._normalize_factor_specs(record)
            playbook_specs = self._normalize_playbook_specs(record)
            combo_key = self._build_combo_key(factor_specs, playbook_specs)
            if not combo_key:
                continue
            phase = self._resolve_market_phase_label(record)
            return_metric = self._extract_return_metric(record)
            risk_metric = self._extract_risk_metric(record)
            filled_count = int(((record.get("outcome") or {}).get("posterior_metrics") or {}).get("filled_symbol_count", 0) or 0)
            ledger = ledgers.setdefault(
                combo_key,
                {
                    "combo_key": combo_key,
                    "playbooks": [{"id": item["id"], "version": item["version"], "weight": item["weight"]} for item in playbook_specs],
                    "factors": [{"id": item["id"], "version": item["version"], "group": item["group"], "weight": item["weight"]} for item in factor_specs],
                    "use_count": 0,
                    "adopted_count": 0,
                    "settled_count": 0,
                    "filled_count": 0,
                    "returns": [],
                    "drawdowns": [],
                    "market_phase_breakdown": Counter(),
                    "selected_symbols": set(),
                    "last_used_at": "",
                },
            )
            ledger["use_count"] += 1
            if str((record.get("adoption") or {}).get("status") or "") == "adopted":
                ledger["adopted_count"] += 1
            if str((record.get("outcome") or {}).get("status") or "") == "settled":
                ledger["settled_count"] += 1
            ledger["filled_count"] += filled_count
            ledger["returns"].append(return_metric)
            ledger["drawdowns"].append(risk_metric)
            ledger["market_phase_breakdown"][phase] += 1
            ledger["selected_symbols"].update(self._resolve_hit_symbols(record))
            ledger["last_used_at"] = str(record.get("recorded_at") or record.get("generated_at") or ledger["last_used_at"])

        payload = []
        for ledger in ledgers.values():
            payload.append(
                {
                    "combo_key": ledger["combo_key"],
                    "playbooks": ledger["playbooks"],
                    "factors": ledger["factors"],
                    "use_count": ledger["use_count"],
                    "adopted_count": ledger["adopted_count"],
                    "settled_count": ledger["settled_count"],
                    "filled_count": ledger["filled_count"],
                    "avg_return": round(self._mean(ledger["returns"]), 6),
                    "avg_drawdown": round(self._mean(ledger["drawdowns"]), 6),
                    "market_phase_breakdown": dict(sorted(ledger["market_phase_breakdown"].items())),
                    "selected_symbols": sorted(ledger["selected_symbols"]),
                    "last_used_at": ledger["last_used_at"],
                }
            )
        payload.sort(key=lambda item: (-int(item["use_count"]), str(item["combo_key"])))
        return payload[:limit]

    def _normalize_factor_specs(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        strategy = dict(record.get("strategy") or {})
        factor_policy = dict((record.get("market_context") or {}).get("factor_policy") or {})
        factor_effectiveness_trace = dict(factor_policy.get("factor_effectiveness_trace") or {})
        payload = []
        for raw in list(strategy.get("factors") or []):
            item = dict(raw or {})
            factor_id = str(item.get("id") or "").strip()
            if not factor_id:
                continue
            version = str(item.get("version") or "v1").strip() or "v1"
            trace = dict(factor_effectiveness_trace.get(factor_id) or {})
            monitor_status = str(trace.get("status") or "unknown").strip() or "unknown"
            monitor_mode = str(trace.get("monitor_mode") or "").strip()
            if not monitor_mode:
                if monitor_status == "unsupported_for_monitor":
                    monitor_mode = "outcome_attribution_only"
                elif monitor_status in {"effective", "ineffective"}:
                    monitor_mode = "cross_sectional_rank_ic"
                elif monitor_status == "unavailable":
                    monitor_mode = "cross_sectional_rank_ic_unavailable"
                else:
                    monitor_mode = "unknown"
            payload.append(
                {
                    "id": factor_id,
                    "version": version,
                    "key": f"{factor_id}:{version}",
                    "group": str(item.get("group") or "").strip(),
                    "name": str(item.get("name") or factor_id),
                    "weight": float(item.get("weight", 0.0) or 0.0),
                    "monitor_status": monitor_status,
                    "monitor_mode": monitor_mode,
                    "mean_rank_ic": float(trace.get("mean_rank_ic", 0.0) or 0.0),
                    "p_value": float(trace.get("p_value", 1.0) or 1.0),
                }
            )
        return payload

    def _normalize_playbook_specs(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        strategy = dict(record.get("strategy") or {})
        payload = []
        for raw in list(strategy.get("playbooks") or []):
            item = dict(raw or {})
            playbook_id = str(item.get("id") or "").strip()
            if not playbook_id:
                continue
            version = str(item.get("version") or "v1").strip() or "v1"
            payload.append(
                {
                    "id": playbook_id,
                    "version": version,
                    "key": f"{playbook_id}:{version}",
                    "name": str(item.get("name") or playbook_id),
                    "weight": float(item.get("weight", 0.0) or 0.0),
                    "market_phases": list(item.get("market_phases") or []),
                }
            )
        return payload

    def _fallback_factor_meta(self, key: str) -> dict[str, Any]:
        factor_id, _, version = key.partition(":")
        return {
            "id": factor_id,
            "version": version or "v1",
            "group": "unknown",
            "name": factor_id or key,
            "weight": 0.0,
            "monitor_status": "unknown",
            "monitor_mode": "unknown",
            "mean_rank_ic": 0.0,
            "p_value": 1.0,
        }

    def _fallback_playbook_meta(self, key: str) -> dict[str, Any]:
        playbook_id, _, version = key.partition(":")
        return {
            "id": playbook_id,
            "version": version or "v1",
            "name": playbook_id or key,
            "weight": 0.0,
            "market_phases": [],
        }

    def _resolve_hit_symbols(self, record: dict[str, Any]) -> list[str]:
        adoption = dict(record.get("adoption") or {})
        outcome = dict(record.get("outcome") or {})
        symbols = list(adoption.get("adopted_symbols") or []) + list(adoption.get("watchlist_symbols") or [])
        symbols += list(outcome.get("filled_symbols") or []) + list(record.get("selected_symbols") or [])
        return [str(item) for item in dict.fromkeys(symbols) if str(item).strip()]

    def _resolve_market_phase_label(self, record: dict[str, Any]) -> str:
        market_context = dict(record.get("market_context") or {})
        market_summary = dict(record.get("market_summary") or {})
        runtime_job = dict(record.get("runtime_job") or {})
        for key in ("market_phase", "market_regime", "regime", "phase"):
            value = market_context.get(key)
            if str(value or "").strip():
                return str(value)
        for key in ("market_phase", "market_regime", "phase"):
            value = market_summary.get(key)
            if str(value or "").strip():
                return str(value)
        for key in ("market_phase", "market_regime", "phase"):
            value = runtime_job.get(key)
            if str(value or "").strip():
                return str(value)
        trade_horizon = str((record.get("intent") or {}).get("trade_horizon") or "").strip()
        return trade_horizon or "unknown"

    def _resolve_trade_date_label(self, record: dict[str, Any]) -> str:
        adoption = dict(record.get("adoption") or {})
        value = str(adoption.get("trade_date") or "").strip()
        if value:
            return value
        generated_at = str(record.get("generated_at") or "").strip()
        if generated_at:
            return generated_at[:10]
        return ""

    def _extract_return_metric(self, record: dict[str, Any]) -> float:
        outcome = dict(record.get("outcome") or {})
        posterior = dict(outcome.get("posterior_metrics") or {})
        if "avg_next_day_close_pct" in posterior:
            return float(posterior.get("avg_next_day_close_pct") or 0.0)
        backtest_metrics = dict(posterior.get("backtest_metrics") or {})
        if "total_return" in backtest_metrics:
            return float(backtest_metrics.get("total_return") or 0.0)
        if "hit_rate" in posterior:
            return float(posterior.get("hit_rate") or 0.0) - 0.5
        return 0.0

    def _extract_risk_metric(self, record: dict[str, Any]) -> float:
        outcome = dict(record.get("outcome") or {})
        posterior = dict(outcome.get("posterior_metrics") or {})
        backtest_metrics = dict(posterior.get("backtest_metrics") or {})
        if "max_drawdown" in backtest_metrics:
            return abs(float(backtest_metrics.get("max_drawdown") or 0.0))
        if "max_drawdown" in posterior:
            return abs(float(posterior.get("max_drawdown") or 0.0))
        return max(0.0, -self._extract_return_metric(record))

    def _extract_effective_score(self, record: dict[str, Any], *, return_metric: float) -> float:
        if return_metric > 0:
            return 1.0
        if return_metric < 0:
            return 0.0
        adoption_status = str((record.get("adoption") or {}).get("status") or "")
        if adoption_status in {"adopted", "watchlist_only", "mixed"}:
            return 0.6
        return 0.2

    def _extract_failure_mode(self, record: dict[str, Any]) -> str:
        outcome = dict(record.get("outcome") or {})
        adoption = dict(record.get("adoption") or {})
        return_metric = self._extract_return_metric(record)
        if return_metric >= 0 and str(outcome.get("status") or "") == "settled":
            return ""
        for candidate in [
            str(outcome.get("reconciliation_status") or "").strip(),
            str(outcome.get("status") or "").strip(),
            str(adoption.get("status") or "").strip(),
        ]:
            if candidate:
                return candidate
        return "pending"

    def _build_effectiveness_summary(
        self,
        events: list[dict[str, Any]],
        recent_trade_dates: dict[int, set[str]],
    ) -> dict[str, float]:
        summary: dict[str, float] = {}
        for window in (5, 20, 60):
            allowed_dates = recent_trade_dates.get(window, set())
            filtered = [float(item.get("score", 0.0) or 0.0) for item in events if str(item.get("trade_date") or "") in allowed_dates]
            summary[f"last_{window}d"] = round(self._mean(filtered), 6)
        return summary

    def _recent_trade_dates(self, records: list[dict[str, Any]]) -> dict[int, set[str]]:
        ordered_dates: list[str] = []
        for record in records:
            trade_date = self._resolve_trade_date_label(record)
            if trade_date and trade_date not in ordered_dates:
                ordered_dates.append(trade_date)
        return {window: set(ordered_dates[-window:]) for window in (5, 20, 60)}

    def _build_combo_key(self, factor_specs: list[dict[str, Any]], playbook_specs: list[dict[str, Any]]) -> str:
        factor_part = ",".join(
            sorted(
                f"{item['id']}@{item['version']}:{float(item.get('weight', 0.0) or 0.0):.2f}"
                for item in factor_specs
            )
        )
        playbook_part = ",".join(
            sorted(
                f"{item['id']}@{item['version']}:{float(item.get('weight', 0.0) or 0.0):.2f}"
                for item in playbook_specs
            )
        )
        if not factor_part and not playbook_part:
            return ""
        return f"playbooks[{playbook_part}]|factors[{factor_part}]"

    def _slim_candidate_record(self, item: dict[str, Any]) -> dict[str, Any]:
        factor_scores = {
            str(key): round(float(value or 0.0), 4)
            for key, value in dict(item.get("factor_scores") or {}).items()
            if str(key).strip()
        }
        return {
            "symbol": str(item.get("symbol") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "rank": int(item.get("rank", 0) or 0),
            "selection_score": round(float(item.get("selection_score", 0.0) or 0.0), 4),
            "composite_adjustment": round(float(item.get("composite_adjustment", 0.0) or 0.0), 6),
            "composite_score": round(float(item.get("composite_score", 0.0) or 0.0), 4),
            "action_hint": str(item.get("action_hint") or "").strip(),
            "resolved_sector": str(item.get("resolved_sector") or item.get("sector") or "").strip(),
            "factor_scores": factor_scores,
            "scoring_meta": {
                "composite_adjustment_multiplier": round(
                    float(((item.get("scoring_meta") or {}).get("composite_adjustment_multiplier", 0.0) or 0.0)),
                    4,
                ),
            },
        }

    def _slim_filtered_record(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(item.get("symbol") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "stage": str(item.get("stage") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
        }

    def _build_composite_multiplier_samples(self, records: list[dict[str, Any]]) -> list[dict[str, float]]:
        samples: list[dict[str, float]] = []
        for record in records:
            outcome = dict(record.get("outcome") or {})
            if str(outcome.get("status") or "") != "settled":
                continue
            candidate_map = {
                str(item.get("symbol") or "").strip(): dict(item)
                for item in list(record.get("candidates") or [])
                if str(item.get("symbol") or "").strip()
            }
            if not candidate_map:
                continue
            attribution_items = list(outcome.get("attribution_items") or [])
            for attribution in attribution_items:
                symbol = str(attribution.get("symbol") or "").strip()
                candidate = candidate_map.get(symbol)
                if candidate is None:
                    continue
                realized_return = attribution.get("holding_return_pct")
                if realized_return is None:
                    realized_return = attribution.get("next_day_close_pct")
                if realized_return is None:
                    continue
                selection_score = float(candidate.get("selection_score", 0.0) or 0.0)
                composite_adjustment = float(candidate.get("composite_adjustment", 0.0) or 0.0)
                if composite_adjustment == 0.0:
                    continue
                samples.append(
                    {
                        "selection_score": selection_score,
                        "composite_adjustment": composite_adjustment,
                        "realized_return": float(realized_return or 0.0),
                    }
                )
        return samples

    def _mean(self, values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(float(item) for item in values) / len(values)

    def _variance(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean_value = self._mean(values)
        return sum((float(item) - mean_value) ** 2 for item in values) / len(values)

    def _covariance(self, xs: list[float], ys: list[float]) -> float:
        if len(xs) < 2 or len(ys) < 2 or len(xs) != len(ys):
            return 0.0
        mean_x = self._mean(xs)
        mean_y = self._mean(ys)
        return sum((float(x) - mean_x) * (float(y) - mean_y) for x, y in zip(xs, ys)) / len(xs)

    def _regression_slope(self, xs: list[float], ys: list[float]) -> float:
        variance = self._variance(xs)
        if variance <= 1e-12:
            return 0.0
        return self._covariance(xs, ys) / variance

    def _pearson(self, xs: list[float], ys: list[float]) -> float:
        if len(xs) < 2 or len(ys) < 2 or len(xs) != len(ys):
            return 0.0
        mean_x = self._mean(xs)
        mean_y = self._mean(ys)
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        denominator_x = sqrt(sum((x - mean_x) ** 2 for x in xs))
        denominator_y = sqrt(sum((y - mean_y) ** 2 for y in ys))
        denominator = denominator_x * denominator_y
        if denominator <= 0:
            return 0.0
        return numerator / denominator

    def _append_audit(self, *, message: str, payload: dict[str, Any]) -> None:
        if self._audit_store is not None:
            self._audit_store.append(category="compose_evaluation", message=message, payload=payload)

    def _read_records(self) -> list[dict[str, Any]]:
        if self._state_store is None:
            return []
        return list(self._state_store.get(self._STATE_KEY, []) or [])

    def _write_records(self, records: list[dict[str, Any]]) -> None:
        if self._state_store is None:
            return
        self._state_store.set(self._STATE_KEY, records[-self._MAX_HISTORY :])

    def _upsert_record(self, record: dict[str, Any]) -> None:
        records = self._read_records()
        trace_id = str(record.get("trace_id") or "")
        for index, item in enumerate(records):
            if str(item.get("trace_id") or "") == trace_id:
                records[index] = record
                self._write_records(records)
                return
        records.append(record)
        self._write_records(records)

    def _find_record(self, trace_id: str) -> dict[str, Any]:
        for item in self._read_records():
            if str(item.get("trace_id") or "") == trace_id:
                return item
        raise KeyError(f"未找到 compose 评估记录: {trace_id}")

    def _resolve_target_symbols(self, record: dict[str, Any]) -> list[str]:
        adoption = dict(record.get("adoption") or {})
        preferred = self._normalize_case_ids(adoption.get("adopted_symbols"))
        if preferred:
            return preferred
        selected = self._normalize_case_ids(record.get("selected_symbols"))
        if selected:
            return selected
        runtime_job = dict(record.get("runtime_job") or {})
        case_ids = self._normalize_case_ids(runtime_job.get("case_ids"))
        if not case_ids or self._candidate_case_service is None:
            return []
        symbols: list[str] = []
        for case_id in case_ids:
            case = self._candidate_case_service.get_case(case_id)
            if case is None:
                continue
            symbol = str(case.symbol or "").strip()
            if symbol:
                symbols.append(symbol)
        return list(dict.fromkeys(symbols))

    def _resolve_trade_date(self, record: dict[str, Any], latest_reconciliation: dict[str, Any]) -> str:
        adoption = dict(record.get("adoption") or {})
        trade_date = str(adoption.get("trade_date") or "").strip()
        if trade_date:
            return trade_date
        case_statuses = list(adoption.get("case_statuses") or [])
        dates = sorted(
            {
                str(item.get("trade_date") or "").strip()
                for item in case_statuses
                if str(item.get("trade_date") or "").strip()
            }
        )
        if len(dates) == 1:
            return dates[0]
        reconciled_at = str(latest_reconciliation.get("reconciled_at") or "").strip()
        if reconciled_at:
            return reconciled_at[:10]
        generated_at = str(record.get("generated_at") or "").strip()
        return generated_at[:10] if generated_at else ""

    def _build_learning_bridge(
        self,
        *,
        record: dict[str, Any],
        tracked_symbols: list[str],
        trade_date: str,
        score_date: str,
        sync_registry_weights: bool,
    ) -> dict[str, Any]:
        target_symbol_set = set(tracked_symbols)
        bridge: dict[str, Any] = {
            "trade_date": trade_date,
            "score_date": score_date,
            "summary": {
                "trade_count": 0,
                "targeted_trade_count": 0,
                "targeted_win_rate": 0.0,
                "parameter_hint_count": 0,
                "nightly_priority_hit_count": 0,
                "active_learned_asset_count": 0,
                "learned_asset_review_required_suggestion_count": 0,
                "registry_synced": False,
            },
            "attribution": {
                "available": False,
                "trade_count": 0,
                "targeted_trade_count": 0,
                "targeted_items": [],
                "parameter_hints": [],
                "by_playbook": [],
                "by_regime": [],
                "summary_lines": [],
            },
            "score_states": {
                "available": False,
                "items": [],
            },
            "learned_assets": {
                "available": False,
                "items": [],
                "recent_approvals": [],
                "summary": {
                    "tracked_count": 0,
                    "active_count": 0,
                    "review_required_count": 0,
                    "experimental_count": 0,
                    "maintain_active_count": 0,
                    "observe_count": 0,
                    "review_required_suggestion_count": 0,
                },
            },
            "registry_weights": {
                "available": False,
                "current_weights": {},
                "expected_weights": {},
                "drift_agents": [],
                "synced": False,
            },
            "nightly_sandbox": {
                "available": False,
                "tomorrow_priorities": [],
                "matched_priorities": [],
                "summary_lines": [],
            },
        }
        if self._trade_attribution_service is not None:
            report = self._trade_attribution_service.build_report(
                trade_date=trade_date or None,
                score_date=score_date or None,
            )
            report_payload = report.model_dump()
            targeted_items = [
                dict(item)
                for item in list(report_payload.get("items") or [])
                if not target_symbol_set or str(item.get("symbol") or "") in target_symbol_set
            ]
            targeted_returns = [float(item.get("next_day_close_pct", 0.0) or 0.0) for item in targeted_items]
            bridge["attribution"] = {
                "available": bool(report_payload.get("available")),
                "trade_count": int(report_payload.get("trade_count", 0) or 0),
                "targeted_trade_count": len(targeted_items),
                "targeted_avg_next_day_close_pct": round(sum(targeted_returns) / len(targeted_returns), 6)
                if targeted_returns
                else 0.0,
                "targeted_win_rate": round(
                    sum(1 for value in targeted_returns if value >= 0.02) / len(targeted_returns),
                    6,
                )
                if targeted_returns
                else 0.0,
                "targeted_items": targeted_items,
                "parameter_hints": list(report_payload.get("parameter_hints") or []),
                "by_playbook": list(report_payload.get("by_playbook") or [])[:5],
                "by_regime": list(report_payload.get("by_regime") or [])[:5],
                "review_summary": dict(report_payload.get("review_summary") or {}),
                "summary_lines": list(report_payload.get("summary_lines") or [])[:5],
            }
            bridge["summary"].update(
                {
                    "trade_count": bridge["attribution"]["trade_count"],
                    "targeted_trade_count": bridge["attribution"]["targeted_trade_count"],
                    "targeted_win_rate": bridge["attribution"]["targeted_win_rate"],
                    "parameter_hint_count": len(bridge["attribution"]["parameter_hints"]),
                }
            )
        if self._agent_score_service is not None and score_date:
            score_states = self._agent_score_service.list_scores(score_date)
            score_items = [
                {
                    "agent_id": item.agent_id,
                    "new_score": item.new_score,
                    "weight_value": item.weight_value,
                    "weight_bucket": item.weight_bucket,
                    "governance_state": item.governance_state,
                    "updated_at": item.updated_at,
                }
                for item in score_states
            ]
            bridge["score_states"] = {
                "available": bool(score_items),
                "items": score_items,
            }
            if self._registry_updater is not None:
                expected_weights = self._agent_score_service.export_weights(score_date)
                current_weights = self._registry_updater.read_current_weights()
                drift_agents = [
                    agent_id
                    for agent_id, expected in expected_weights.items()
                    if round(float(current_weights.get(agent_id, 0.0) or 0.0), 4) != round(float(expected or 0.0), 4)
                ]
                synced = False
                if sync_registry_weights and score_states:
                    synced = self._registry_updater.update_from_scores([item.model_dump() for item in score_states])
                    current_weights = self._registry_updater.read_current_weights()
                    drift_agents = [
                        agent_id
                        for agent_id, expected in expected_weights.items()
                        if round(float(current_weights.get(agent_id, 0.0) or 0.0), 4) != round(float(expected or 0.0), 4)
                    ]
                bridge["registry_weights"] = {
                    "available": True,
                    "current_weights": current_weights,
                    "expected_weights": expected_weights,
                    "drift_agents": drift_agents,
                    "synced": synced,
                }
                bridge["summary"]["registry_synced"] = synced
        if self._nightly_sandbox is not None and trade_date:
            sandbox_result = self._nightly_sandbox.load_result(trade_date)
            if sandbox_result is not None:
                matched_priorities = [
                    symbol for symbol in list(sandbox_result.tomorrow_priorities or [])
                    if not target_symbol_set or symbol in target_symbol_set
                ]
                bridge["nightly_sandbox"] = {
                    "available": True,
                    "tomorrow_priorities": list(sandbox_result.tomorrow_priorities or []),
                    "matched_priorities": matched_priorities,
                    "summary_lines": list(sandbox_result.summary_lines or []),
                }
                bridge["summary"]["nightly_priority_hit_count"] = len(matched_priorities)
        if self._learned_asset_service is not None:
            tracked_asset_ids = self._normalize_case_ids(record.get("learned_asset_ids"))
            approvals = self._learned_asset_service.recent_approvals(limit=50)
            approval_map = {
                f"{str(item.get('asset_id') or '').strip()}:{str(item.get('version') or '').strip()}": item
                for item in approvals
            }
            items: list[dict[str, Any]] = []
            active_count = 0
            review_required_count = 0
            experimental_count = 0
            maintain_active_count = 0
            observe_count = 0
            review_required_suggestion_count = 0
            for asset_id in tracked_asset_ids:
                entry = self._learned_asset_service._repository.get(asset_id)  # type: ignore[attr-defined]
                if entry is None:
                    continue
                if entry.status == "active":
                    active_count += 1
                elif entry.status == "review_required":
                    review_required_count += 1
                elif entry.status == "experimental":
                    experimental_count += 1
                approval_key = f"{entry.id}:{entry.version}"
                approval_record = approval_map.get(approval_key, {})
                advice = self._build_learned_asset_advice(
                    asset_id=entry.id,
                    current_status=str(entry.status),
                    attribution=bridge.get("attribution") or {},
                    nightly_sandbox=bridge.get("nightly_sandbox") or {},
                    learned_asset_options=record.get("learned_asset_options") or {},
                    auto_selected_asset_ids=self._normalize_case_ids(record.get("auto_selected_learned_asset_ids")),
                )
                if advice.get("recommended_action") == "maintain_active":
                    maintain_active_count += 1
                elif advice.get("recommended_action") == "review_required":
                    review_required_suggestion_count += 1
                else:
                    observe_count += 1
                items.append(
                    {
                        "id": entry.id,
                        "version": entry.version,
                        "type": entry.type,
                        "status": entry.status,
                        "author": entry.author,
                        "source": entry.source,
                        "tags": list(entry.tags or []),
                        "last_transition": {
                            "to_status": approval_record.get("to_status"),
                            "operator": approval_record.get("operator"),
                            "recorded_at": approval_record.get("recorded_at"),
                            "discussion_binding": approval_record.get("discussion_binding") or {},
                        },
                        "advice": advice,
                    }
                )
            bridge["learned_assets"] = {
                "available": bool(items),
                "items": items,
                "recent_approvals": [
                    item for item in approvals if str(item.get("asset_id") or "").strip() in set(tracked_asset_ids)
                ][:10],
                "summary": {
                    "tracked_count": len(items),
                    "active_count": active_count,
                    "review_required_count": review_required_count,
                    "experimental_count": experimental_count,
                    "maintain_active_count": maintain_active_count,
                    "observe_count": observe_count,
                    "review_required_suggestion_count": review_required_suggestion_count,
                },
            }
            bridge["summary"]["active_learned_asset_count"] = active_count
            bridge["summary"]["learned_asset_review_required_suggestion_count"] = review_required_suggestion_count
        return bridge

    @staticmethod
    def _build_learned_asset_advice(
        *,
        asset_id: str,
        current_status: str,
        attribution: dict[str, Any],
        nightly_sandbox: dict[str, Any],
        learned_asset_options: dict[str, Any],
        auto_selected_asset_ids: list[str],
    ) -> dict[str, Any]:
        targeted_avg = float(attribution.get("targeted_avg_next_day_close_pct", 0.0) or 0.0)
        targeted_win_rate = float(attribution.get("targeted_win_rate", 0.0) or 0.0)
        parameter_hints = list(attribution.get("parameter_hints") or [])
        positive_hint_count = 0
        negative_hint_count = 0
        reasons: list[str] = []

        for item in parameter_hints:
            action = str(item.get("action") or item.get("suggested_action") or "").strip().lower()
            if action in {"promote", "increase", "boost", "relax"}:
                positive_hint_count += 1
            elif action in {"reduce", "tighten", "suppress", "block"}:
                negative_hint_count += 1

        matched_priorities = list(nightly_sandbox.get("matched_priorities") or [])
        auto_apply_active = bool(learned_asset_options.get("auto_apply_active"))
        used_as_auto_selected = asset_id in set(auto_selected_asset_ids)

        positive_signals = 0
        negative_signals = 0
        if targeted_avg >= 0.02:
            positive_signals += 1
            reasons.append(f"目标标的次日均收益 {targeted_avg:.2%}")
        elif targeted_avg <= -0.02:
            negative_signals += 1
            reasons.append(f"目标标的次日均收益偏弱 {targeted_avg:.2%}")

        if targeted_win_rate >= 0.5:
            positive_signals += 1
            reasons.append(f"目标胜率 {targeted_win_rate:.0%}")
        elif targeted_win_rate > 0:
            negative_signals += 1
            reasons.append(f"目标胜率偏弱 {targeted_win_rate:.0%}")

        if positive_hint_count > negative_hint_count and positive_hint_count > 0:
            positive_signals += 1
            reasons.append(f"参数提示偏正向 {positive_hint_count} 条")
        elif negative_hint_count > 0:
            negative_signals += 1
            reasons.append(f"参数提示偏负向 {negative_hint_count} 条")

        if matched_priorities:
            positive_signals += 1
            reasons.append(f"夜间沙盘命中次日优先 {len(matched_priorities)} 支")

        if auto_apply_active and used_as_auto_selected:
            reasons.append("本轮通过自动吸附进入主链")

        recommended_action = "observe"
        if current_status == "active":
            if negative_signals >= 2 or targeted_avg <= -0.03:
                recommended_action = "review_required"
            elif positive_signals >= 2 and negative_signals == 0:
                recommended_action = "maintain_active"
        elif current_status in {"review_required", "experimental"}:
            if positive_signals >= 2 and negative_signals == 0:
                recommended_action = "promote_active"
            elif negative_signals > 0:
                recommended_action = "keep_review"

        if not reasons:
            reasons.append("当前后验信号不足，建议继续观察")

        return {
            "recommended_action": recommended_action,
            "confidence": round(min(max((positive_signals + negative_signals) / 4.0, 0.25), 0.95), 4),
            "positive_signal_count": positive_signals,
            "negative_signal_count": negative_signals,
            "used_as_auto_selected": used_as_auto_selected,
            "auto_apply_active": auto_apply_active,
            "reasons": reasons[:4],
        }

    @staticmethod
    def _normalize_case_ids(raw_case_ids: Any) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in list(raw_case_ids or []) if str(item).strip()))

    @staticmethod
    def _count_attr_values(items: list[Any], attr_name: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            key = str(getattr(item, attr_name, "") or "").strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _resolve_adoption_status(
        *,
        selected_count: int,
        watchlist_count: int,
        rejected_count: int,
        resolved_case_count: int,
        missing_case_ids: list[str],
    ) -> str:
        if selected_count > 0:
            return "adopted"
        if watchlist_count > 0:
            return "watchlist"
        if rejected_count > 0 and rejected_count == resolved_case_count:
            return "rejected"
        if resolved_case_count <= 0 and missing_case_ids:
            return "missing_cases"
        if resolved_case_count <= 0:
            return "no_cases"
        return "pending"

    @staticmethod
    def _build_reconcile_note(
        *,
        case_count: int,
        resolved_case_count: int,
        selected_count: int,
        watchlist_count: int,
        rejected_count: int,
        missing_case_ids: list[str],
    ) -> str:
        parts = [
            f"回看 discussion case {case_count} 条",
            f"resolved={resolved_case_count}",
            f"selected={selected_count}",
            f"watchlist={watchlist_count}",
            f"rejected={rejected_count}",
        ]
        if missing_case_ids:
            parts.append("missing=" + ",".join(missing_case_ids))
        return "；".join(parts)

    @staticmethod
    def _resolve_outcome_status(
        *,
        reconciliation_status: str,
        tracked_symbol_count: int,
        filled_symbol_count: int,
        matched_symbol_count: int,
    ) -> str:
        if reconciliation_status == "error":
            return "error"
        if tracked_symbol_count <= 0:
            return "no_targets"
        if filled_symbol_count >= tracked_symbol_count and tracked_symbol_count > 0:
            return "settled"
        if filled_symbol_count > 0:
            return "partial"
        if matched_symbol_count > 0:
            return "matched_unfilled"
        return "pending"

    @staticmethod
    def _build_outcome_note(
        *,
        tracked_symbol_count: int,
        matched_symbol_count: int,
        filled_symbol_count: int,
        attribution_count: int,
        reconciliation_status: str,
    ) -> str:
        return (
            f"执行对账状态 {reconciliation_status}；"
            f"tracked={tracked_symbol_count}；"
            f"matched={matched_symbol_count}；"
            f"filled={filled_symbol_count}；"
            f"attribution={attribution_count}"
        )
