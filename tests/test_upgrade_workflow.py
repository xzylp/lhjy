import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import (
    get_agent_score_service,
    get_candidate_case_service,
    get_discussion_cycle_service,
    get_feishu_longconn_state_store,
    get_meeting_state_store,
    get_monitor_state_store,
    get_research_state_store,
    get_runtime_state_store,
    reset_container,
)
from ashare_system.container import get_execution_adapter, get_market_adapter
from ashare_system.container import get_monitor_state_service, get_runtime_config_manager
from ashare_system.contracts import (
    BalanceSnapshot,
    EventFetchResult,
    OrderSnapshot,
    PositionSnapshot,
    StructuredEvent,
    TradeSnapshot,
)
from ashare_system.contracts import MarketProfile, PlaceOrderRequest
from ashare_system.data.event_bus import EventBus
from ashare_system.data.event_fetcher import EventFetcher
from ashare_system.data.serving import ServingStore
from ashare_system.data.storage import ensure_storage_layout
from ashare_system.discussion.discussion_service import DiscussionCycle, DiscussionCycleService
from ashare_system.infra.adapters import WindowsProxyExecutionAdapter
from ashare_system.infra.market_adapter import WindowsProxyMarketDataAdapter
from ashare_system.infra.audit_store import StateStore
from ashare_system.monitor.persistence import build_execution_bridge_health_ingress_payload
from ashare_system.notify.templates import agent_supervision_template
from ashare_system.portfolio import build_test_trading_budget
from ashare_system.scheduler import POST_MARKET_TASKS
from ashare_system.scheduler import _normalize_crontab_day_of_week
from ashare_system.settings import AppSettings, WindowsGatewaySettings
from ashare_system.strategy.atomic_repository import StrategyRepository, StrategyRepositoryEntry
from ashare_system.strategy.factor_registry import FactorDefinition
from ashare_system.strategy.constraint_pack import apply_constraint_pack, resolve_constraint_pack
from ashare_system.strategy.factor_registry import bootstrap_factor_registry, factor_registry
from ashare_system.strategy.learned_asset_service import LearnedAssetService
from ashare_system.strategy.nightly_sandbox import NightlySandbox
from ashare_system.strategy.playbook_registry import PlaybookDefinition
from ashare_system.strategy.playbook_registry import bootstrap_playbook_registry, playbook_registry
from ashare_system.strategy.strategy_composer import StrategyComposer
from ashare_system.supervision_tasks import (
    build_agent_task_plan,
    record_agent_task_completion,
    record_agent_task_dispatch,
)
from ashare_system.supervision_tasks import task_dispatch_control_key
from ashare_system.runtime_compose_contracts import RuntimeComposeRequest
from ashare_system.risk.guard import ExecutionGuard
from ashare_system.risk.rules import RiskRules, RiskThresholds
from ashare_system.supervision_state import annotate_supervision_payload, record_supervision_notification


class UpgradeWorkflowTests(unittest.TestCase):
    def _build_supervision_answer_lines_for_test(self, supervision: dict[str, object]) -> list[str]:
        payload = dict(supervision or {})
        trade_date = str(payload.get("trade_date") or "").strip() or "-"
        phase = dict((payload.get("task_dispatch_plan") or {}).get("phase") or {})
        phase_label = str(phase.get("label") or "").strip() or "未知阶段"
        attention_items = [dict(item) for item in list(payload.get("attention_items") or [])]
        notify_items = [dict(item) for item in list(payload.get("notify_items") or [])]
        acknowledged_items = [dict(item) for item in list(payload.get("acknowledged_items") or [])]
        working_items = [
            dict(item)
            for item in list(payload.get("items") or [])
            if str(item.get("status") or "") == "working"
        ]

        lines = [
            f"监督席位: trade_date={trade_date} phase={phase_label} attention={len(attention_items)} notify={len(notify_items)} acked={len(acknowledged_items)}。",
        ]
        quality_summary_lines = [str(item).strip() for item in list(payload.get("quality_summary_lines") or []) if str(item).strip()]
        progress_blockers = [str(item).strip() for item in list(payload.get("progress_blockers") or []) if str(item).strip()]
        if quality_summary_lines:
            lines.append("推进质量: " + "；".join(quality_summary_lines[:2]))
        if payload.get("escalated"):
            lines.append("当前有重复未解除的阻塞，监督已升级为强提醒。")
        elif notify_items:
            lines.append("当前监督以任务派发为主，优先催办仍缺新产出的岗位。")
        elif working_items:
            lines.append("当前大部分岗位有新活动痕迹，监督以值班观察为主。")
        if progress_blockers:
            lines.append("当前卡点: " + "；".join(progress_blockers[:2]))
        if payload.get("escalated"):
            lead_reason = next(
                (
                    str(item.get("supervision_action_reason") or "").strip()
                    for item in notify_items
                    if str(item.get("supervision_action_reason") or "").strip()
                ),
                "",
            )
            if lead_reason:
                lines.append("升级原因: " + lead_reason)

        for item in notify_items[:3]:
            tier = str(item.get("supervision_tier") or item.get("status") or "").strip()
            task_reason = str(item.get("task_reason") or "").strip() or "当前有待补齐任务"
            outputs = [str(output).strip() for output in list(item.get("expected_outputs") or []) if str(output).strip()]
            line = f"{item.get('agent_id')} 需处理 [{tier}]: {task_reason}。"
            quality_state = str(item.get("quality_state") or "").strip()
            quality_reason = str(item.get("quality_reason") or "").strip()
            if quality_state and quality_reason:
                line += f" 推进质量={quality_state}，{quality_reason}"
            action_reason = str(item.get("supervision_action_reason") or "").strip()
            if action_reason:
                line += f" 优先原因={action_reason}"
            if outputs:
                line += " 预期产物=" + " / ".join(outputs[:3]) + "。"
            lines.append(line)

        if not notify_items:
            for item in working_items[:3]:
                activity_label = str(item.get("activity_label") or "任务").strip()
                reasons = [str(reason).strip() for reason in list(item.get("reasons") or []) if str(reason).strip()]
                line = f"{item.get('agent_id')} 正在值班: {activity_label}。"
                if reasons:
                    line += " " + reasons[0]
                lines.append(line)

        if acknowledged_items:
            acked_preview = "；".join(str(item.get("agent_id") or "-") for item in acknowledged_items[:3])
            lines.append(f"已确认催办对象: {acked_preview}。")
        return lines

    def test_execution_guard_uses_runtime_style_risk_thresholds(self) -> None:
        guard = ExecutionGuard(
            rules=RiskRules(
                RiskThresholds(
                    max_single_position=0.1,
                    max_daily_loss=0.02,
                    max_consecutive_loss=2,
                )
            )
        )
        request = PlaceOrderRequest(
            account_id="sim-001",
            symbol="600519.SH",
            side="BUY",
            quantity=1000,
            price=20.0,
            request_id="guard-001",
        )
        balance = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
        approved, reason, adjusted = guard.approve(
            request,
            balance,
            profile=MarketProfile(sentiment_phase="主升", position_ceiling=0.3),
            consecutive_losses=0,
            daily_pnl=0.0,
        )
        self.assertTrue(approved)
        self.assertEqual(adjusted.quantity, 500)
        self.assertEqual(reason, "通过")

        rejected, reject_reason, _ = guard.approve(
            request,
            balance,
            consecutive_losses=2,
            daily_pnl=-3000.0,
        )
        self.assertFalse(rejected)
        self.assertIn("单日亏损", reject_reason)

    def test_constraint_pack_supports_market_rules_and_execution_barrier_filtering(self) -> None:
        resolved = resolve_constraint_pack(
            {
                "market_rules": {
                    "allowed_regimes": ["trend"],
                    "min_regime_score": 0.6,
                    "allow_inferred_market_profile": False,
                },
                "execution_barriers": {
                    "max_price_deviation_pct": 0.02,
                },
            },
            get_runtime_config_manager().get(),
        )
        kept, filtered = apply_constraint_pack(
            [
                {
                    "symbol": "AAA",
                    "name": "甲",
                    "market_snapshot": {
                        "last_price": 10.0,
                        "bid_price": 9.6,
                        "ask_price": 9.7,
                    },
                }
            ],
            resolved,
            market_profile={"regime": "trend", "regime_score": 0.8, "inferred": False},
        )
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["stage"], "execution_barriers")
        self.assertIn("price_deviation_exceeded", filtered[0]["reason"])

        kept2, filtered2 = apply_constraint_pack(
            [{"symbol": "BBB", "name": "乙"}],
            resolved,
            market_profile={"regime": "rotation", "regime_score": 0.7, "inferred": False},
        )
        self.assertEqual(kept2, [])
        self.assertEqual(filtered2[0]["stage"], "market_rules")
        self.assertIn("market_regime_not_allowed", filtered2[0]["reason"])

    def test_learned_asset_service_supports_review_and_activation_flow(self) -> None:
        class StubCaseService:
            def get_case(self, case_id: str):
                if case_id == "case-001":
                    return type(
                        "Case",
                        (),
                        {
                            "risk_gate": "allow",
                            "audit_gate": "clear",
                            "final_status": "selected",
                            "trade_date": "2026-04-15",
                            "symbol": "600519.SH",
                        },
                    )()
                if case_id == "case-bad":
                    return type("Case", (), {"risk_gate": "allow", "audit_gate": "clear", "final_status": "rejected"})()
                return None

            def build_trade_date_summary(self, trade_date: str):
                return {
                    "selected_count": 1,
                    "watchlist_count": 0,
                    "rejected_count": 0,
                }

            def build_reason_board(self, trade_date: str):
                return {
                    "selected_count": 1,
                    "watchlist_count": 0,
                    "rejected_count": 0,
                }

            def build_case_vote_detail(self, case_id: str):
                return {
                    "headline_line": "600519.SH 贵州茅台 获多数支持",
                    "agent_votes": [{"agent_id": "ashare-strategy", "stance": "support"}],
                }

            def build_reply_pack(self, trade_date: str):
                return {
                    "selected_count": 1,
                    "watchlist_count": 0,
                    "rejected_count": 0,
                    "overview_lines": [f"交易日 {trade_date}，当前优先执行池关注 贵州茅台。"],
                }

        repository = StrategyRepository()
        repository.submit_learned_entry(
            asset_id="learned-alpha-001",
            name="学习战法 001",
            asset_type="learned_combo",
            author="ashare-strategy",
            source="nightly_sandbox",
            content={"playbooks": ["trend_acceleration"]},
        )
        service = LearnedAssetService(repository, candidate_case_service=StubCaseService())
        review_item = service.transition(
            asset_id="learned-alpha-001",
            version="v1",
            target_status="review_required",
            operator="ashare-audit",
            note="进入评审",
        )
        self.assertEqual(review_item.status, "review_required")
        with self.assertRaises(ValueError):
            service.transition(
                asset_id="learned-alpha-001",
                version="v1",
                target_status="active",
                operator="ashare-risk",
                note="缺审批上下文不允许直接转正",
            )
        with self.assertRaises(ValueError):
            service.transition(
                asset_id="learned-alpha-001",
                version="v1",
                target_status="active",
                operator="ashare-risk",
                note="缺正式引用不允许直接转正",
                approval_context={
                    "discussion_passed": True,
                    "risk_passed": True,
                    "audit_passed": True,
                },
            )
        with self.assertRaises(ValueError):
            service.transition(
                asset_id="learned-alpha-001",
                version="v1",
                target_status="active",
                operator="ashare-risk",
                note="引用 rejected case 不允许转正",
                approval_context={
                    "discussion_passed": True,
                    "risk_passed": True,
                    "audit_passed": True,
                    "discussion_case_id": "case-bad",
                    "risk_gate": "allow",
                    "audit_gate": "clear",
                },
            )
        active_item = service.transition(
            asset_id="learned-alpha-001",
            version="v1",
            target_status="active",
            operator="ashare-risk",
            note="通过风控和审计",
            evaluation_summary={"win_rate": 0.61},
            approval_context={
                "discussion_passed": True,
                "risk_passed": True,
                "audit_passed": True,
                "discussion_case_id": "case-001",
                "risk_gate": "allow",
                "audit_gate": "clear",
            },
        )
        self.assertEqual(active_item.status, "active")
        self.assertEqual(active_item.evaluation_summary["win_rate"], 0.61)

    def test_learned_asset_service_resolve_advice_requires_explicit_transition_and_approval(self) -> None:
        class StubCaseService:
            def get_case(self, case_id: str):
                if case_id != "case-001":
                    return None
                return type(
                    "Case",
                    (),
                    {
                        "risk_gate": "allow",
                        "audit_gate": "clear",
                        "final_status": "selected",
                        "trade_date": "2026-04-16",
                        "symbol": "002882.SZ",
                    },
                )()

            def build_trade_date_summary(self, trade_date: str):
                return {"selected_count": 1, "watchlist_count": 0, "rejected_count": 0}

            def build_reason_board(self, trade_date: str):
                return {"selected_count": 1, "watchlist_count": 0, "rejected_count": 0}

            def build_case_vote_detail(self, case_id: str):
                return {"headline_line": "002882.SZ 金龙羽 获多数支持", "agent_votes": []}

            def build_reply_pack(self, trade_date: str):
                return {"selected_count": 1, "watchlist_count": 0, "rejected_count": 0, "overview_lines": ["测试回包"]}

        repository = StrategyRepository()
        entry = repository.submit_learned_entry(
            asset_id="learned-advice-001",
            name="学习战法 resolve 测试",
            asset_type="learned_combo",
            author="ashare-strategy",
            source="nightly_sandbox",
            content={"playbooks": ["trend_acceleration"]},
        )
        state_store = StateStore(Path(tempfile.mkdtemp()) / "state.json")
        service = LearnedAssetService(repository, state_store=state_store, candidate_case_service=StubCaseService())
        queue_item = service.stage_advice(
            asset_id=entry.id,
            version=entry.version,
            trace_id="trace-resolve-001",
            operator="ashare-audit",
            advice={"recommended_action": "promote_active"},
            note="等待治理决议",
        )
        resolved = service.resolve_advice(
            queue_id=queue_item["queue_id"],
            resolution_status="accepted",
            operator="ashare-audit",
            note="先通过治理，不自动改状态",
        )
        self.assertEqual(resolved["status"], "accepted")
        self.assertFalse(resolved["transition_applied"])
        self.assertEqual(repository.get(entry.id, entry.version).status, "draft")

        queue_item_2 = service.stage_advice(
            asset_id=entry.id,
            version=entry.version,
            trace_id="trace-resolve-002",
            operator="ashare-audit",
            advice={"recommended_action": "promote_active"},
            note="二次治理",
        )
        with self.assertRaises(ValueError):
            service.resolve_advice(
                queue_id=queue_item_2["queue_id"],
                resolution_status="applied",
                operator="ashare-risk",
                apply_transition=True,
                target_status="active",
                note="审批上下文不足",
            )
        self.assertEqual(repository.get(entry.id, entry.version).status, "draft")

        service.transition(
            asset_id=entry.id,
            version=entry.version,
            target_status="review_required",
            operator="ashare-audit",
            note="进入评审态",
        )
        queue_item_3 = service.stage_advice(
            asset_id=entry.id,
            version=entry.version,
            trace_id="trace-resolve-003",
            operator="ashare-audit",
            advice={"recommended_action": "promote_active"},
            note="三次治理",
        )
        resolved_applied = service.resolve_advice(
            queue_id=queue_item_3["queue_id"],
            resolution_status="applied",
            operator="ashare-risk",
            apply_transition=True,
            target_status="active",
            note="审批通过后正式转正",
            approval_context={
                "discussion_passed": True,
                "risk_passed": True,
                "audit_passed": True,
                "discussion_case_id": "case-001",
                "risk_gate": "allow",
                "audit_gate": "clear",
            },
        )
        self.assertEqual(resolved_applied["status"], "applied")
        self.assertTrue(resolved_applied["transition_applied"])
        self.assertEqual(resolved_applied["transition_result"]["status"], "active")
        self.assertEqual(repository.get(entry.id, entry.version).status, "active")

    def test_factor_and_playbook_registries_expose_executable_adapters(self) -> None:
        bootstrap_factor_registry()
        bootstrap_playbook_registry()
        factor_eval = factor_registry.evaluate(
            "momentum_slope",
            version="v1",
            candidate={"selection_score": 80.0, "score_breakdown": {"liquidity_score": 10.0}},
            context={"hot_sectors": ["机器人"], "market_hypothesis": "机器人走强"},
        )
        playbook_eval = playbook_registry.evaluate(
            "trend_acceleration",
            version="v1",
            candidate={
                "selection_score": 80.0,
                "rank": 1,
                "action": "BUY",
                "score_breakdown": {"momentum_pct": 3.0, "liquidity_score": 10.0},
            },
            context={"trade_horizon": "intraday_to_overnight", "holding_symbols": []},
        )
        self.assertGreater(factor_eval["score"], 0.0)
        self.assertGreater(playbook_eval["score"], 0.0)
        self.assertTrue(factor_eval["evidence"])
        self.assertTrue(playbook_eval["evidence"])
        self.assertGreater(playbook_eval["score"], 0.5)
        sector_resonance_eval = playbook_registry.evaluate(
            "sector_resonance",
            version="v1",
            candidate={
                "selection_score": 78.0,
                "rank": 2,
                "resolved_sector": "机器人",
                "score_breakdown": {"liquidity_score": 9.0},
            },
            context={"hot_sectors": ["机器人"]},
        )
        self.assertGreater(sector_resonance_eval["score"], 0.5)
        weak_to_strong_eval = playbook_registry.evaluate(
            "weak_to_strong_intraday",
            version="v1",
            candidate={
                "selection_score": 78.0,
                "rank": 2,
                "action": "BUY",
                "score_breakdown": {"momentum_pct": 2.5, "liquidity_score": 9.0},
            },
            context={},
        )
        self.assertGreater(weak_to_strong_eval["score"], 0.5)
        replacement_eval = playbook_registry.evaluate(
            "position_replacement",
            version="v1",
            candidate={"selection_score": 76.0, "rank": 2},
            context={"holding_symbols": ["600166.SH"]},
        )
        self.assertGreater(replacement_eval["score"], 0.5)
        tail_close_eval = playbook_registry.evaluate(
            "tail_close_ambush",
            version="v1",
            candidate={"selection_score": 70.0, "action": "BUY", "score_breakdown": {"liquidity_score": 8.0}},
            context={"trade_horizon": "intraday_to_overnight"},
        )
        self.assertGreater(tail_close_eval["score"], 0.5)
        breakout_eval = factor_registry.evaluate(
            "breakout_quality",
            version="v1",
            candidate={"selection_score": 80.0, "score_breakdown": {"price_bias_score": 4.0, "liquidity_score": 10.0}},
            context={},
        )
        self.assertGreater(breakout_eval["score"], 0.5)

    def test_strategy_composer_builds_composite_candidates_from_registered_assets(self) -> None:
        bootstrap_factor_registry()
        bootstrap_playbook_registry()
        composer = StrategyComposer(factor_registry, playbook_registry)
        request = RuntimeComposeRequest.model_validate(
            {
                "intent": {
                    "mode": "opportunity_scan",
                    "objective": "寻找趋势加速机会",
                    "market_hypothesis": "主升阶段优先趋势加速与板块共振",
                    "trade_horizon": "intraday_to_overnight",
                },
                "strategy": {
                    "playbooks": [
                        {"id": "trend_acceleration", "weight": 0.7},
                        {"id": "sector_resonance", "weight": 0.3},
                    ],
                    "factors": [
                        {"id": "momentum_slope", "group": "momentum", "weight": 0.3},
                        {"id": "sector_heat_score", "group": "sentiment", "weight": 0.2},
                    ],
                    "ranking": {
                        "primary_score": "composite_score",
                        "secondary_keys": ["sector_heat_score"],
                    },
                },
                "output": {"max_candidates": 2},
            }
        )
        payload = composer.build_output(
            request,
            {
                "top_picks": [
                    {"symbol": "AAA", "name": "甲", "rank": 2, "action": "BUY", "selection_score": 70.0, "summary": "候选甲"},
                    {"symbol": "BBB", "name": "乙", "rank": 1, "action": "BUY", "selection_score": 68.0, "summary": "候选乙"},
                ],
                "market_profile": {"regime": "trend", "hot_sectors": ["机器人"], "market_risk_flags": []},
            },
            get_runtime_config_manager().get(),
        )
        self.assertEqual(payload["candidates"][0]["symbol"], "AAA")
        self.assertIn("trend_acceleration", payload["candidates"][0]["playbook_fit"])
        self.assertIn("momentum_slope", payload["candidates"][0]["factor_scores"])
        self.assertGreater(payload["candidates"][0]["composite_score"], 70.0)
        self.assertTrue(payload["candidates"][0]["evidence"])

    def test_strategy_composer_applies_active_learned_asset_bias_only_after_activation(self) -> None:
        bootstrap_factor_registry()
        bootstrap_playbook_registry()
        composer = StrategyComposer(factor_registry, playbook_registry)
        request = RuntimeComposeRequest.model_validate(
            {
                "intent": {
                    "mode": "opportunity_scan",
                    "objective": "验证转正 learned asset 会进入 compose 主链",
                    "market_hypothesis": "同等条件下让已验证学习产物提升指定因子权重",
                    "trade_horizon": "intraday_to_overnight",
                },
                "strategy": {
                    "playbooks": [{"id": "trend_acceleration", "weight": 1.0}],
                    "factors": [{"id": "sector_heat_score", "group": "sentiment", "weight": 0.1}],
                },
                "learned_assets": [
                    {
                        "id": "learned-bias-001",
                        "name": "板块热度增强",
                        "type": "learned_combo",
                        "content": {"weights": {"sector_heat_score": 0.4}, "score_bonus": 1.2},
                    }
                ],
            }
        )
        pipeline_payload = {
            "top_picks": [
                {
                    "symbol": "AAA",
                    "name": "甲",
                    "rank": 1,
                    "action": "BUY",
                    "selection_score": 70.0,
                    "summary": "候选甲",
                    "score_breakdown": {"sector_score": 9.0},
                    "resolved_sector": "机器人",
                }
            ],
            "market_profile": {"regime": "trend", "hot_sectors": ["机器人"], "market_risk_flags": []},
        }
        inactive_payload = composer.build_output(
            request,
            pipeline_payload,
            get_runtime_config_manager().get(),
            learned_asset_entries=[
                {
                    "id": "learned-bias-001",
                    "status": "review_required",
                    "content": {"weights": {"sector_heat_score": 0.4}, "score_bonus": 1.2},
                }
            ],
        )
        active_payload = composer.build_output(
            request,
            pipeline_payload,
            get_runtime_config_manager().get(),
            learned_asset_entries=[
                {
                    "id": "learned-bias-001",
                    "status": "active",
                    "content": {"weights": {"sector_heat_score": 0.4}, "score_bonus": 1.2},
                }
            ],
        )
        inactive_candidate = inactive_payload["candidates"][0]
        active_candidate = active_payload["candidates"][0]
        self.assertEqual(inactive_payload["explanations"]["learned_asset_summary"]["active_count"], 0)
        self.assertEqual(active_payload["explanations"]["learned_asset_summary"]["active_count"], 1)
        self.assertEqual(active_candidate["learned_asset_adjustments"]["active_assets"], ["learned-bias-001"])
        self.assertGreater(
            active_candidate["factor_scores"]["sector_heat_score"],
            inactive_candidate["factor_scores"]["sector_heat_score"],
        )
        self.assertGreater(active_candidate["composite_score"], inactive_candidate["composite_score"])
        self.assertIn("学习产物加权", " ".join(active_candidate["evidence"]))

    def test_strategy_composer_accepts_auto_selected_active_learned_assets(self) -> None:
        bootstrap_factor_registry()
        bootstrap_playbook_registry()
        composer = StrategyComposer(factor_registry, playbook_registry)
        request = RuntimeComposeRequest.model_validate(
            {
                "intent": {
                    "mode": "opportunity_scan",
                    "objective": "验证自动吸附 active learned asset",
                    "market_hypothesis": "热点板块扩散时自动吸附轮动增强资产",
                    "trade_horizon": "intraday_to_overnight",
                },
                "strategy": {
                    "playbooks": [{"id": "trend_acceleration", "weight": 0.7}],
                    "factors": [{"id": "sector_heat_score", "group": "sentiment", "weight": 0.1}],
                },
                "learned_asset_options": {
                    "auto_apply_active": True,
                    "max_auto_apply": 2,
                    "preferred_tags": ["trend", "rotation"],
                },
            }
        )
        payload = composer.build_output(
            request,
            {
                "top_picks": [
                    {"symbol": "AAA", "name": "甲", "rank": 1, "action": "BUY", "selection_score": 70.0, "summary": "候选甲"}
                ],
                "market_profile": {"regime": "trend", "hot_sectors": ["机器人"], "market_risk_flags": []},
            },
            get_runtime_config_manager().get(),
            learned_asset_entries=[
                {
                    "id": "auto-asset-001",
                    "status": "active",
                    "tags": ["trend", "rotation"],
                    "content": {"factor_weights": {"sector_heat_score": 0.3}, "score_bonus": 0.8},
                }
            ],
        )
        self.assertEqual(payload["explanations"]["learned_asset_summary"]["requested_count"], 0)
        self.assertEqual(payload["explanations"]["learned_asset_summary"]["auto_selected_count"], 1)
        self.assertEqual(
            payload["explanations"]["learned_asset_summary"]["auto_selected_asset_ids"],
            ["auto-asset-001"],
        )
        self.assertEqual(
            payload["candidates"][0]["learned_asset_adjustments"]["active_assets"],
            ["auto-asset-001"],
        )

    def test_strategy_atomic_repository_supports_registration_and_partition_queries(self) -> None:
        repository = StrategyRepository()
        repository.register(
            StrategyRepositoryEntry(
                id="sector_heat_score",
                name="板块热度因子",
                type="factor",
                status="active",
                version="v1",
                source="manual",
                params_schema={"window": {"type": "integer", "default": 5}},
            )
        )
        repository.register(
            StrategyRepositoryEntry(
                id="weak_to_strong_intraday",
                name="弱转强盘中战法",
                type="playbook",
                status="experimental",
                version="v1",
                source="research",
            )
        )

        active_entries = repository.list_entries(status="active")
        self.assertEqual(len(active_entries), 1)
        self.assertEqual(active_entries[0].id, "sector_heat_score")

        playbooks = repository.list_entries(type="playbook")
        self.assertEqual(len(playbooks), 1)
        self.assertEqual(playbooks[0].id, "weak_to_strong_intraday")

        summary = repository.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_status"]["active"], 1)
        self.assertEqual(summary["by_status"]["experimental"], 1)

    def test_strategy_atomic_repository_supports_learned_entry_submission_and_status_promotion(self) -> None:
        repository = StrategyRepository()
        entry = repository.submit_learned_entry(
            asset_id="agent_trend_rotation_combo",
            name="Agent 学习出的趋势轮动组合",
            asset_type="learned_combo",
            author="ashare-strategy",
            source="nightly_sandbox",
            content={
                "playbooks": ["sector_resonance", "trend_acceleration"],
                "factor_weights": {"sector_heat_score": 0.22, "breakout_quality": 0.18},
            },
            params_schema={"min_sector_heat": {"type": "number", "default": 0.7}},
            evidence_schema={"required": ["market_hypothesis", "evaluation_summary"]},
            risk_notes=["需确认退潮期是否失效"],
            tags=["agent-learned", "sandbox"],
        )
        self.assertEqual(entry.status, "draft")
        self.assertEqual(entry.author, "ashare-strategy")

        promoted = repository.set_status("agent_trend_rotation_combo", "v1", "review_required")
        self.assertEqual(promoted.status, "review_required")

        latest = repository.get("agent_trend_rotation_combo")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.status, "review_required")
        self.assertEqual(latest.type, "learned_combo")
        self.assertEqual(latest.content["playbooks"], ["sector_resonance", "trend_acceleration"])

    def test_strategy_atomic_repository_exposes_runtime_policy_by_status_and_type(self) -> None:
        active_factor = StrategyRepositoryEntry(
            id="momentum_slope",
            name="动量斜率",
            type="factor",
            status="active",
            version="v1",
        )
        experimental_playbook = StrategyRepositoryEntry(
            id="weak_to_strong_intraday",
            name="弱转强盘中战法",
            type="playbook",
            status="experimental",
            version="v1",
        )
        active_learned = StrategyRepositoryEntry(
            id="agent_rotation_combo",
            name="轮动组合",
            type="learned_combo",
            status="active",
            version="v1",
        )
        review_template = StrategyRepositoryEntry(
            id="review_template",
            name="评审模板",
            type="template",
            status="review_required",
            version="v1",
        )
        archived_factor = StrategyRepositoryEntry(
            id="deprecated_factor",
            name="废弃因子",
            type="factor",
            status="archived",
            version="v1",
        )

        self.assertEqual(active_factor.runtime_policy()["mode"], "default")
        self.assertTrue(active_factor.runtime_policy()["default_enabled"])
        self.assertEqual(experimental_playbook.runtime_policy()["mode"], "explicit_only")
        self.assertTrue(experimental_playbook.runtime_policy()["explicit_enabled"])
        self.assertFalse(experimental_playbook.runtime_policy()["default_enabled"])
        self.assertEqual(active_learned.runtime_policy()["mode"], "auto_or_explicit")
        self.assertTrue(active_learned.runtime_policy()["auto_selectable"])
        self.assertEqual(review_template.runtime_policy()["mode"], "governance_only")
        self.assertFalse(review_template.runtime_policy()["explicit_enabled"])
        self.assertEqual(archived_factor.runtime_policy()["mode"], "blocked")
        self.assertFalse(archived_factor.runtime_policy()["explicit_enabled"])

    def test_strategy_atomic_repository_builds_version_view_with_recommended_versions(self) -> None:
        repository = StrategyRepository()
        repository.register(
            StrategyRepositoryEntry(
                id="trend_combo",
                name="趋势组合",
                type="playbook",
                status="experimental",
                version="v2",
            )
        )
        repository.register(
            StrategyRepositoryEntry(
                id="trend_combo",
                name="趋势组合",
                type="playbook",
                status="active",
                version="v1",
            )
        )
        repository.register(
            StrategyRepositoryEntry(
                id="trend_combo",
                name="趋势组合",
                type="playbook",
                status="archived",
                version="v0",
            )
        )

        version_view = repository.version_view(type="playbook", asset_id="trend_combo")
        self.assertEqual(len(version_view), 1)
        item = version_view[0]
        self.assertEqual(item["recommended_version"], "v1")
        self.assertEqual(item["recommended_runtime_mode"], "default")
        self.assertEqual(item["default_versions"], ["v1"])
        self.assertIn("v2", item["explicit_candidate_versions"])
        self.assertIn("v0", item["blocked_versions"])

    def test_build_test_trading_budget_respects_position_limit_and_baseline(self) -> None:
        budget = build_test_trading_budget(
            total_asset=101028.55,
            equity_value=0.0,
            reverse_repo_value=0.0,
            minimum_total_invested_amount=100000.0,
            reverse_repo_reserved_amount=0.0,
            stock_position_limit_ratio=0.3,
        )
        self.assertAlmostEqual(budget.stock_test_budget_amount, 30308.565, places=3)
        self.assertAlmostEqual(budget.stock_test_budget_remaining, 30308.565, places=3)

        tighter_budget = build_test_trading_budget(
            total_asset=150000.0,
            equity_value=5000.0,
            reverse_repo_value=0.0,
            minimum_total_invested_amount=100000.0,
            reverse_repo_reserved_amount=20000.0,
            stock_position_limit_ratio=0.5,
        )
        self.assertEqual(tighter_budget.stock_test_budget_amount, 75000.0)
        self.assertEqual(tighter_budget.stock_test_budget_remaining, 70000.0)

    def _promote_case_to_selected(self, client: TestClient, case_id: str) -> dict:
        payload = client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": case_id,
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "reasons": ["板块与消息催化延续，具备继续跟踪价值"],
                        "evidence_refs": ["/system/research/summary"],
                    },
                    {
                        "case_id": case_id,
                        "round": 1,
                        "agent_id": "ashare-strategy",
                        "stance": "support",
                        "reasons": ["交易策略接受该标的进入执行池"],
                        "evidence_refs": ["/system/discussions/reason-board"],
                    },
                    {
                        "case_id": case_id,
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "support",
                        "reasons": ["风险口允许按测试仓位约束进入预检"],
                        "evidence_refs": ["/system/discussions/execution-precheck"],
                    },
                    {
                        "case_id": case_id,
                        "round": 1,
                        "agent_id": "ashare-audit",
                        "stance": "support",
                        "reasons": ["审计口确认当前提案具备进入执行池的最小依据"],
                        "evidence_refs": ["/system/audits"],
                    },
                ],
            },
        ).json()
        self.assertTrue(payload["ok"])
        updated_case = next(item for item in payload["items"] if item["case_id"] == case_id)
        self.assertEqual(updated_case["final_status"], "selected")
        self.assertEqual(updated_case["risk_gate"], "allow")
        self.assertEqual(updated_case["audit_gate"], "clear")
        return updated_case

    def tearDown(self) -> None:
        reset_container()

    def test_runtime_pipeline_response_exposes_case_ids_and_case_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()
            adapter = get_execution_adapter()
            adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
            adapter.positions["sim-001"] = []
            adapter.trades["sim-001"] = []

            try:
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ"],
                            "max_candidates": 2,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()

                    self.assertIn("case_ids", payload)
                    self.assertIn("case_count", payload)
                    self.assertEqual(payload["case_count"], 2)
                    self.assertEqual(len(payload["case_ids"]), payload["case_count"])

                    trade_date = str(payload["generated_at"])[:10]
                    cases_payload = client.get(f"/system/cases?trade_date={trade_date}&limit=50").json()
                    self.assertEqual(cases_payload["count"], payload["case_count"])
                    self.assertEqual(
                        {item["case_id"] for item in cases_payload["items"]},
                        set(payload["case_ids"]),
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_returns_structured_payload_and_registers_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    response = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-test-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-strategy",
                                "role": "strategy",
                                "session_id": "cycle-20260415",
                                "proposal_id": "proposal-001",
                            },
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "寻找热点方向机会",
                                "market_hypothesis": "热点扩散但不追银行",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {
                                "scope": "custom",
                                "symbol_pool": ["600519.SH", "000001.SZ", "000333.SZ"],
                                "sector_blacklist": ["银行"],
                            },
                            "strategy": {
                                "playbooks": [
                                    {"id": "trend_acceleration_test", "version": "v1", "weight": 0.6, "params": {"window": 20}}
                                ],
                                "factors": [
                                    {"id": "sector_heat_score_test", "group": "sentiment", "version": "v1", "weight": 0.2, "params": {"window": 5}}
                                ],
                                "ranking": {
                                    "primary_score": "composite_score",
                                    "secondary_keys": ["sector_heat_score_test"],
                                },
                            },
                            "constraints": {
                                "hard_filters": {
                                    "symbol_blacklist": ["600519.SH"],
                                },
                                "user_preferences": {
                                    "excluded_theme_keywords": ["银行"],
                                    "max_single_amount": 20000,
                                    "equity_position_limit": 0.3,
                                },
                                "position_rules": {
                                    "holding_symbols": ["000333.SZ"],
                                    "avoid_existing_holdings": True,
                                },
                                "risk_rules": {
                                    "blocked_symbols": ["000001.SZ"],
                                },
                            },
                            "output": {
                                "max_candidates": 2,
                                "include_filtered_reasons": True,
                                "include_score_breakdown": True,
                                "include_evidence": True,
                                "include_counter_evidence": True,
                                "return_mode": "proposal_ready",
                            },
                            "learned_assets": [
                                {
                                    "id": "agent_combo_test_001",
                                    "name": "Agent 组合测试",
                                    "type": "learned_combo",
                                    "version": "v1",
                                    "source": "nightly_sandbox",
                                    "content": {"weights": {"sector_heat_score_test": 0.2}},
                                }
                            ],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(payload["request_id"], "compose-test-001")
                    self.assertEqual(payload["agent"]["agent_id"], "ashare-strategy")
                    self.assertEqual(payload["intent"]["mode"], "opportunity_scan")
                    self.assertIn("candidates", payload)
                    self.assertIn("repository", payload)
                    self.assertIn("runtime_job", payload)
                    self.assertIn("applied_constraints", payload)
                    self.assertIn("evaluation_trace", payload)
                    self.assertTrue(payload["evaluation_trace"]["stored"])
                    self.assertGreaterEqual(len(payload["repository"]["used_assets"]), 2)
                    self.assertEqual(payload["repository"]["learned_assets"][0]["id"], "agent_combo_test_001")
                    self.assertEqual(payload["repository"]["used_assets"][0]["runtime_policy"]["mode"], "explicit_only")
                    self.assertEqual(
                        payload["repository"]["learned_assets"][0]["runtime_policy"]["mode"],
                        "governance_only",
                    )
                    self.assertEqual(
                        payload["applied_constraints"]["position_rules"]["equity_position_limit"],
                        0.3,
                    )
                    self.assertEqual(
                        payload["applied_constraints"]["hard_filters"]["symbol_blacklist"],
                        ["600519.SH"],
                    )
                    filtered_stages = {item["stage"] for item in payload["filtered_out"]}
                    self.assertIn("risk_rules", filtered_stages)
                    self.assertIn("position_rules", filtered_stages)
                    candidate_symbols = {item["symbol"] for item in payload["candidates"]}
                    self.assertNotIn("600519.SH", candidate_symbols)
                    self.assertNotIn("000001.SZ", candidate_symbols)
                    self.assertNotIn("000333.SZ", candidate_symbols)
                    trace_id = payload["evaluation_trace"]["trace_id"]

                    evaluations_payload = client.get("/runtime/evaluations?limit=5").json()
                    self.assertTrue(evaluations_payload["ok"])
                    self.assertGreaterEqual(len(evaluations_payload["items"]), 1)
                    self.assertEqual(evaluations_payload["items"][0]["trace_id"], trace_id)
                    self.assertIn("proposal_packet", payload)

                    feedback_payload = client.post(
                        "/runtime/evaluations/feedback",
                        json={
                            "trace_id": trace_id,
                            "adoption": {
                                "status": "adopted",
                                "adopted_symbols": ["600166.SH"],
                                "trade_date": "2026-04-15",
                                "updated_by": "ashare-strategy",
                            },
                            "outcome": {
                                "status": "settled",
                                "posterior_metrics": {"hit_rate": 0.5},
                                "updated_by": "ashare-audit",
                            },
                        },
                    ).json()
                    self.assertTrue(feedback_payload["ok"])
                    self.assertEqual(feedback_payload["item"]["adoption"]["status"], "adopted")
                    self.assertEqual(feedback_payload["item"]["outcome"]["status"], "settled")

                    panel_payload = client.get("/runtime/evaluations/panel?limit=5").json()
                    self.assertTrue(panel_payload["ok"])
                    self.assertGreaterEqual(panel_payload["summary"]["adopted_count"], 1)
                    self.assertGreaterEqual(panel_payload["summary"]["settled_count"], 1)

                    repo_payload = client.get("/runtime/strategy-repository").json()
                    repo_ids = {item["id"] for item in repo_payload["items"]}
                    self.assertIn("trend_acceleration_test", repo_ids)
                    self.assertIn("sector_heat_score_test", repo_ids)
                    self.assertIn("agent_combo_test_001", repo_ids)
                    self.assertIn("version_view", repo_payload)
                    self.assertIn("governance_summary", repo_payload)
                    repo_policies = {item["id"]: item["runtime_policy"]["mode"] for item in repo_payload["items"]}
                    self.assertEqual(repo_policies["trend_acceleration_test"], "explicit_only")
                    self.assertEqual(repo_policies["sector_heat_score_test"], "explicit_only")
                    self.assertEqual(repo_policies["agent_combo_test_001"], "governance_only")
                    self.assertIn("race_summary", repo_payload["version_view"][0])
                    self.assertIn("governance_suggestion", repo_payload["version_view"][0])

                    transition_payload = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_test_001",
                            "version": "v1",
                            "target_status": "review_required",
                            "operator": "ashare-audit",
                            "note": "进入评审",
                            "evaluation_summary": {"sandbox_score": 0.77},
                        },
                    ).json()
                    self.assertTrue(transition_payload["ok"])
                    self.assertEqual(transition_payload["item"]["status"], "review_required")

                    failed_activation = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_test_001",
                            "version": "v1",
                            "target_status": "active",
                            "operator": "ashare-risk",
                            "note": "缺审批上下文应失败",
                        },
                    ).json()
                    self.assertFalse(failed_activation["ok"])

                    real_case_id = payload["runtime_job"]["case_ids"][0]
                    promoted_case = self._promote_case_to_selected(client, real_case_id)

                    passed_activation = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_test_001",
                            "version": "v1",
                            "target_status": "active",
                            "operator": "ashare-risk",
                            "note": "通过三道门后转正",
                            "approval_context": {
                                "discussion_passed": True,
                                "risk_passed": True,
                                "audit_passed": True,
                                "discussion_case_id": real_case_id,
                                "risk_gate": promoted_case["risk_gate"],
                                "audit_gate": promoted_case["audit_gate"],
                            },
                        },
                    ).json()
                    self.assertTrue(passed_activation["ok"])
                    self.assertEqual(passed_activation["item"]["status"], "active")

                    approvals_payload = client.get("/runtime/learned-assets/approvals?limit=5").json()
                    self.assertTrue(approvals_payload["ok"])
                    self.assertGreaterEqual(len(approvals_payload["items"]), 2)
                    self.assertEqual(approvals_payload["items"][0]["asset_id"], "agent_combo_test_001")
                    self.assertEqual(
                        approvals_payload["items"][0]["discussion_binding"]["case_id"],
                        real_case_id,
                    )
                    self.assertIn(
                        "reason_board_summary",
                        approvals_payload["items"][0]["discussion_binding"],
                    )
                    self.assertIn(
                        "vote_detail",
                        approvals_payload["items"][0]["discussion_binding"],
                    )
                    self.assertIn(
                        "reply_pack_summary",
                        approvals_payload["items"][0]["discussion_binding"],
                    )
                    self.assertIn(
                        "final_brief_summary",
                        approvals_payload["items"][0]["discussion_binding"],
                    )
                    self.assertIn(
                        "finalize_packet_summary",
                        approvals_payload["items"][0]["discussion_binding"],
                    )
                    self.assertIn(
                        "status",
                        approvals_payload["items"][0]["discussion_binding"]["final_brief_summary"],
                    )
                    self.assertIn(
                        "blocked",
                        approvals_payload["items"][0]["discussion_binding"]["finalize_packet_summary"],
                    )

                    panel_payload = client.get("/runtime/learned-assets/panel?limit=5").json()
                    self.assertTrue(panel_payload["ok"])
                    self.assertGreaterEqual(panel_payload["summary"]["total"], 1)
                    self.assertGreaterEqual(len(panel_payload["recent_approvals"]), 1)
                    self.assertGreaterEqual(panel_payload["summary"]["linked_case_count"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_evaluation_reconcile_reads_discussion_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    response = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-reconcile-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-research",
                                "role": "research",
                                "session_id": "cycle-20260415-reconcile",
                                "proposal_id": "proposal-reconcile-001",
                            },
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "验证 compose 评估账本与 discussion case 的自动回看联动",
                                "market_hypothesis": "先用最小限制跑出候选，再回看 case 状态",
                                "trade_horizon": "intraday",
                            },
                            "universe": {
                                "scope": "custom",
                                "symbol_pool": ["600519.SH", "000001.SZ"],
                            },
                            "strategy": {
                                "playbooks": [
                                    {"id": "trend_acceleration_test", "version": "v1", "weight": 0.7, "params": {}}
                                ],
                                "factors": [
                                    {"id": "sector_heat_score_test", "group": "sentiment", "version": "v1", "weight": 0.3, "params": {}}
                                ],
                                "ranking": {
                                    "primary_score": "composite_score",
                                    "secondary_keys": ["sector_heat_score_test"],
                                },
                            },
                            "constraints": {},
                            "output": {
                                "max_candidates": 2,
                                "include_filtered_reasons": True,
                                "include_score_breakdown": True,
                                "include_evidence": True,
                                "include_counter_evidence": True,
                                "return_mode": "proposal_ready",
                            },
                            "learned_assets": [
                                {
                                    "id": "agent_combo_outcome_001",
                                    "name": "Outcome 联动组合",
                                    "type": "learned_combo",
                                    "version": "v1",
                                    "source": "nightly_sandbox",
                                    "content": {"weights": {"sector_heat_score_test": 0.3}},
                                }
                            ],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    trace_id = payload["evaluation_trace"]["trace_id"]
                    case_ids = list(payload["runtime_job"].get("case_ids") or [])
                    self.assertGreaterEqual(len(case_ids), 1)

                    reconcile_payload = client.post(
                        "/runtime/evaluations/reconcile",
                        json={
                            "trace_id": trace_id,
                            "updated_by": "ashare-audit",
                        },
                    ).json()
                    self.assertTrue(reconcile_payload["ok"])
                    adoption = reconcile_payload["item"]["adoption"]
                    self.assertEqual(adoption["discussion_case_ids"], case_ids)
                    self.assertEqual(adoption["resolved_case_count"], len(case_ids))
                    self.assertEqual(len(adoption["case_statuses"]), len(case_ids))
                    self.assertEqual(adoption["missing_case_ids"], [])
                    self.assertIn(adoption["status"], {"adopted", "watchlist", "rejected"})

                    panel_payload = client.get("/runtime/evaluations/panel?limit=5").json()
                    self.assertTrue(panel_payload["ok"])
                    panel_item = next(item for item in panel_payload["items"] if item["trace_id"] == trace_id)
                    self.assertEqual(panel_item["adoption"]["status"], adoption["status"])
                    self.assertEqual(panel_item["adoption"]["discussion_case_ids"], case_ids)
                    if adoption["status"] == "adopted":
                        self.assertGreaterEqual(panel_payload["summary"]["adopted_count"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_evaluation_reconcile_outcome_reads_execution_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=95000.0)
                    adapter.positions["sim-001"] = []
                    adapter.orders["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    response = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-outcome-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-strategy",
                                "role": "strategy",
                                "session_id": "cycle-20260415-outcome",
                                "proposal_id": "proposal-outcome-001",
                            },
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "验证 compose 评估账本与执行后验的自动回看联动",
                                "market_hypothesis": "产出候选后进入执行对账回填",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {
                                "scope": "custom",
                                "symbol_pool": ["600519.SH", "000001.SZ"],
                            },
                            "strategy": {
                                "playbooks": [
                                    {"id": "trend_acceleration_test", "version": "v1", "weight": 0.7, "params": {}}
                                ],
                                "factors": [
                                    {"id": "sector_heat_score_test", "group": "sentiment", "version": "v1", "weight": 0.3, "params": {}}
                                ],
                                "ranking": {
                                    "primary_score": "composite_score",
                                    "secondary_keys": ["sector_heat_score_test"],
                                },
                            },
                            "constraints": {},
                            "output": {
                                "max_candidates": 2,
                                "include_filtered_reasons": True,
                                "include_score_breakdown": True,
                                "include_evidence": True,
                                "include_counter_evidence": True,
                                "return_mode": "proposal_ready",
                            },
                            "learned_assets": [
                                {
                                    "id": "agent_combo_outcome_001",
                                    "name": "Outcome 联动组合",
                                    "type": "learned_combo",
                                    "version": "v1",
                                    "source": "nightly_sandbox",
                                    "content": {"weights": {"sector_heat_score_test": 0.3}},
                                }
                            ],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    trace_id = payload["evaluation_trace"]["trace_id"]
                    real_case_id = payload["runtime_job"]["case_ids"][0]
                    promoted_case = self._promote_case_to_selected(client, real_case_id)
                    symbol = promoted_case["symbol"]

                    reconcile_adoption_payload = client.post(
                        "/runtime/evaluations/reconcile",
                        json={"trace_id": trace_id, "updated_by": "ashare-audit"},
                    ).json()
                    self.assertTrue(reconcile_adoption_payload["ok"])
                    self.assertEqual(reconcile_adoption_payload["item"]["adoption"]["status"], "adopted")
                    learned_asset_activation_payload = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_outcome_001",
                            "version": "v1",
                            "target_status": "review_required",
                            "operator": "ashare-audit",
                            "note": "进入评审",
                            "evaluation_summary": {"sandbox_score": 0.83},
                        },
                    ).json()
                    self.assertTrue(learned_asset_activation_payload["ok"], learned_asset_activation_payload)
                    learned_asset_activation_payload = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_outcome_001",
                            "version": "v1",
                            "target_status": "active",
                            "operator": "ashare-risk",
                            "note": "学习产物联动转正",
                            "approval_context": {
                                "discussion_passed": True,
                                "risk_passed": True,
                                "audit_passed": True,
                                "discussion_case_id": real_case_id,
                                "risk_gate": promoted_case["risk_gate"],
                                "audit_gate": promoted_case["audit_gate"],
                            },
                        },
                    ).json()
                    self.assertTrue(learned_asset_activation_payload["ok"], learned_asset_activation_payload)

                    adapter.orders["sim-001"] = [
                        OrderSnapshot(
                            order_id="order-outcome-001",
                            account_id="sim-001",
                            symbol=symbol,
                            side="BUY",
                            quantity=500,
                            price=10.0,
                            status="FILLED",
                        )
                    ]
                    adapter.trades["sim-001"] = [
                        TradeSnapshot(
                            trade_id="trade-outcome-001",
                            order_id="order-outcome-001",
                            account_id="sim-001",
                            symbol=symbol,
                            side="BUY",
                            quantity=500,
                            price=10.0,
                        )
                    ]
                    adapter.positions["sim-001"] = [
                        PositionSnapshot(
                            account_id="sim-001",
                            symbol=symbol,
                            quantity=500,
                            available=500,
                            cost_price=10.0,
                            last_price=10.5,
                        )
                    ]
                    meeting_state_store = get_meeting_state_store()
                    meeting_state_store.set(
                        "execution_order_journal",
                        [
                            {
                                "order_id": "order-outcome-001",
                                "symbol": symbol,
                                "name": promoted_case.get("name", ""),
                                "decision_id": real_case_id,
                                "trade_date": promoted_case["trade_date"],
                                "submitted_at": datetime.now().isoformat(),
                                "request": {
                                    "side": "BUY",
                                    "playbook": "trend_acceleration_test",
                                    "regime": "trend",
                                },
                                "review_tags": ["sector_relative_trend_weak"],
                            }
                        ],
                    )

                    execution_reconciliation_payload = client.post(
                        "/system/execution-reconciliation/run?account_id=sim-001"
                    ).json()
                    self.assertTrue(execution_reconciliation_payload["ok"])
                    self.assertEqual(execution_reconciliation_payload["filled_order_count"], 1)
                    self.assertEqual(execution_reconciliation_payload["attribution"]["update_count"], 1)
                    score_service = get_agent_score_service()
                    score_service.record_settlement(
                        agent_id="ashare-strategy",
                        score_date=promoted_case["trade_date"],
                        result_score_delta=1.2,
                        cases_evaluated=[
                            {
                                "symbol": symbol,
                                "stance": "support",
                                "next_day_close_outcome": "positive",
                                "delta": 1.2,
                            }
                        ],
                    )
                    sandbox = NightlySandbox(Path(tmp_dir))
                    sandbox.run_simulation(
                        trade_date=promoted_case["trade_date"],
                        finalize_bundle={
                            "watchlist": [
                                {
                                    "symbol": symbol,
                                    "score": 1.0,
                                    "final_status": "watchlist",
                                }
                            ]
                        },
                        attribution_report=execution_reconciliation_payload["attribution"]["report"],
                        parameter_hints=[
                            {
                                "symbol": symbol,
                                "action": "promote",
                                "weight": 1.2,
                                "reason": "回测桥提示应提升次日优先级",
                            }
                        ],
                    )

                    outcome_payload = client.post(
                        "/runtime/evaluations/reconcile-outcome",
                        json={
                            "trace_id": trace_id,
                            "account_id": "sim-001",
                            "updated_by": "ashare-audit",
                        },
                    ).json()
                    self.assertTrue(outcome_payload["ok"])
                    outcome = outcome_payload["item"]["outcome"]
                    self.assertEqual(outcome["status"], "settled")
                    self.assertEqual(outcome["account_id"], "sim-001")
                    self.assertEqual(outcome["filled_symbols"], [symbol])
                    self.assertEqual(outcome["pending_symbols"], [])
                    self.assertEqual(outcome["posterior_metrics"]["filled_symbol_count"], 1)
                    self.assertEqual(outcome["posterior_metrics"]["filled_order_count"], 1)
                    self.assertAlmostEqual(
                        outcome["posterior_metrics"]["avg_next_day_close_pct"],
                        0.05,
                        places=6,
                    )
                    self.assertEqual(len(outcome["attribution_items"]), 1)
                    self.assertIn("learning_bridge", outcome)
                    learning_bridge = outcome["learning_bridge"]
                    self.assertEqual(learning_bridge["summary"]["targeted_trade_count"], 1)
                    self.assertGreaterEqual(learning_bridge["summary"]["parameter_hint_count"], 1)
                    self.assertTrue(learning_bridge["score_states"]["available"])
                    self.assertTrue(
                        any(item["agent_id"] == "ashare-strategy" for item in learning_bridge["score_states"]["items"])
                    )
                    self.assertTrue(learning_bridge["learned_assets"]["available"])
                    self.assertEqual(learning_bridge["learned_assets"]["summary"]["active_count"], 1)
                    self.assertEqual(learning_bridge["learned_assets"]["items"][0]["id"], "agent_combo_outcome_001")
                    self.assertEqual(
                        learning_bridge["learned_assets"]["items"][0]["advice"]["recommended_action"],
                        "maintain_active",
                    )
                    self.assertGreaterEqual(
                        learning_bridge["learned_assets"]["summary"]["maintain_active_count"],
                        1,
                    )
                    self.assertTrue(learning_bridge["registry_weights"]["available"])
                    self.assertIn("ashare-strategy", learning_bridge["registry_weights"]["expected_weights"])
                    self.assertIsInstance(learning_bridge["registry_weights"]["drift_agents"], list)
                    self.assertIn("nightly_sandbox", learning_bridge)
                    self.assertIn("available", learning_bridge["nightly_sandbox"])
                    self.assertIsInstance(learning_bridge["nightly_sandbox"]["matched_priorities"], list)

                    advice_ingest_payload = client.post(
                        "/runtime/learned-assets/advice/ingest",
                        json={
                            "trace_id": trace_id,
                            "operator": "ashare-audit",
                            "asset_id": "agent_combo_outcome_001",
                            "note": "盘后学习会收敛到待审事项",
                        },
                    ).json()
                    self.assertTrue(advice_ingest_payload["ok"])
                    self.assertEqual(advice_ingest_payload["queued_count"], 1)
                    self.assertEqual(
                        advice_ingest_payload["items"][0]["governance_route"],
                        "maintain_review",
                    )

                    advice_queue_payload = client.get("/runtime/learned-assets/advice?limit=5").json()
                    self.assertTrue(advice_queue_payload["ok"])
                    self.assertEqual(advice_queue_payload["items"][0]["asset_id"], "agent_combo_outcome_001")
                    self.assertEqual(advice_queue_payload["items"][0]["recommended_action"], "maintain_active")
                    queue_id = advice_queue_payload["items"][0]["queue_id"]

                    advice_resolve_payload = client.post(
                        "/runtime/learned-assets/advice/resolve",
                        json={
                            "queue_id": queue_id,
                            "resolution_status": "accepted",
                            "operator": "ashare-audit",
                            "note": "本轮只做治理确认，不自动改状态",
                        },
                    ).json()
                    self.assertTrue(advice_resolve_payload["ok"])
                    self.assertEqual(advice_resolve_payload["item"]["status"], "accepted")
                    self.assertFalse(advice_resolve_payload["item"]["transition_applied"])

                    advice_resolve_again_payload = client.post(
                        "/runtime/learned-assets/advice/resolve",
                        json={
                            "queue_id": queue_id,
                            "resolution_status": "applied",
                            "operator": "ashare-risk",
                            "apply_transition": True,
                            "target_status": "active",
                        },
                    ).json()
                    self.assertFalse(advice_resolve_again_payload["ok"])
                    self.assertIn("不可重复处理", advice_resolve_again_payload["error"])

                    panel_payload = client.get("/runtime/evaluations/panel?limit=5").json()
                    self.assertTrue(panel_payload["ok"])
                    panel_item = next(item for item in panel_payload["items"] if item["trace_id"] == trace_id)
                    self.assertEqual(panel_item["outcome"]["status"], "settled")
                    self.assertGreaterEqual(panel_payload["summary"]["settled_count"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_consumes_active_learned_asset_in_mainline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    compose_request = {
                        "request_id": "compose-learned-mainline-001",
                        "account_id": "sim-001",
                        "agent": {
                            "agent_id": "ashare-strategy",
                            "role": "strategy",
                            "session_id": "cycle-20260415-learned-mainline",
                            "proposal_id": "proposal-learned-mainline-001",
                        },
                        "intent": {
                            "mode": "opportunity_scan",
                            "objective": "验证 active learned asset 已进入 compose 主链",
                            "market_hypothesis": "已转正的学习产物可提升热度因子权重",
                            "trade_horizon": "intraday_to_overnight",
                        },
                        "universe": {
                            "scope": "custom",
                            "symbol_pool": ["600519.SH", "000001.SZ"],
                        },
                        "strategy": {
                            "playbooks": [
                                {"id": "trend_acceleration_test", "version": "v1", "weight": 0.7, "params": {}}
                            ],
                            "factors": [
                                {"id": "sector_heat_score_test", "group": "sentiment", "version": "v1", "weight": 0.1, "params": {}}
                            ],
                        },
                        "constraints": {},
                        "output": {"max_candidates": 2},
                        "learned_assets": [
                            {
                                "id": "agent_combo_mainline_001",
                                "name": "Mainline 学习组合",
                                "type": "learned_combo",
                                "version": "v1",
                                "source": "nightly_sandbox",
                                "content": {"weights": {"sector_heat_score_test": 0.4}, "score_bonus": 1.5},
                            }
                        ],
                    }
                    baseline = client.post("/runtime/jobs/compose", json=compose_request).json()
                    self.assertEqual(
                        baseline["explanations"]["learned_asset_summary"]["active_count"],
                        0,
                    )
                    baseline_score = baseline["candidates"][0]["composite_score"]

                    transition_review = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_mainline_001",
                            "version": "v1",
                            "target_status": "review_required",
                            "operator": "ashare-audit",
                            "note": "进入评审",
                        },
                    ).json()
                    self.assertTrue(transition_review["ok"], transition_review)

                    case_id = baseline["runtime_job"]["case_ids"][0]
                    promoted_case = self._promote_case_to_selected(client, case_id)
                    transition_active = client.post(
                        "/runtime/learned-assets/transition",
                        json={
                            "asset_id": "agent_combo_mainline_001",
                            "version": "v1",
                            "target_status": "active",
                            "operator": "ashare-risk",
                            "note": "正式纳入 compose 主链",
                            "approval_context": {
                                "discussion_passed": True,
                                "risk_passed": True,
                                "audit_passed": True,
                                "discussion_case_id": case_id,
                                "risk_gate": promoted_case["risk_gate"],
                                "audit_gate": promoted_case["audit_gate"],
                            },
                        },
                    ).json()
                    self.assertTrue(transition_active["ok"], transition_active)

                    activated = client.post("/runtime/jobs/compose", json=compose_request).json()
                    self.assertEqual(
                        activated["repository"]["active_learned_assets"][0]["id"],
                        "agent_combo_mainline_001",
                    )
                    self.assertEqual(
                        activated["explanations"]["learned_asset_summary"]["active_count"],
                        1,
                    )
                    self.assertEqual(
                        activated["candidates"][0]["learned_asset_adjustments"]["active_assets"],
                        ["agent_combo_mainline_001"],
                    )
                    self.assertGreater(activated["candidates"][0]["composite_score"], baseline_score)
                    self.assertGreaterEqual(
                        activated["candidates"][0]["learned_asset_adjustments"]["factor_weight_bias"]["sector_heat_score_test"],
                        0.4,
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_can_auto_select_active_learned_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    submit_payload = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-auto-learned-seed-001",
                            "account_id": "sim-001",
                            "agent": {"agent_id": "ashare-strategy", "role": "strategy"},
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "先种入一个 learned asset",
                                "market_hypothesis": "趋势轮动增强",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {"scope": "custom", "symbol_pool": ["600519.SH", "000001.SZ"]},
                            "strategy": {
                                "playbooks": [{"id": "trend_acceleration_test", "version": "v1", "weight": 0.7, "params": {}}],
                                "factors": [{"id": "sector_heat_score_test", "group": "sentiment", "version": "v1", "weight": 0.1, "params": {}}],
                            },
                            "learned_assets": [
                                {
                                    "id": "agent_combo_auto_001",
                                    "name": "自动吸附资产",
                                    "type": "learned_combo",
                                    "version": "v1",
                                    "source": "nightly_sandbox",
                                    "tags": ["trend", "rotation"],
                                    "content": {
                                        "playbooks": ["trend_acceleration_test"],
                                        "factor_weights": {"sector_heat_score_test": 0.35},
                                        "themes": ["机器人"],
                                        "match_keywords": ["趋势", "轮动"],
                                        "score_bonus": 1.0,
                                    },
                                }
                            ],
                            "output": {"max_candidates": 2},
                        },
                    ).json()
                    self.assertEqual(submit_payload["repository"]["learned_assets"][0]["id"], "agent_combo_auto_001")
                    case_id = submit_payload["runtime_job"]["case_ids"][0]
                    promoted_case = self._promote_case_to_selected(client, case_id)
                    self.assertTrue(
                        client.post(
                            "/runtime/learned-assets/transition",
                            json={
                                "asset_id": "agent_combo_auto_001",
                                "version": "v1",
                                "target_status": "review_required",
                                "operator": "ashare-audit",
                                "note": "进入评审",
                            },
                        ).json()["ok"]
                    )
                    self.assertTrue(
                        client.post(
                            "/runtime/learned-assets/transition",
                            json={
                                "asset_id": "agent_combo_auto_001",
                                "version": "v1",
                                "target_status": "active",
                                "operator": "ashare-risk",
                                "note": "允许自动吸附",
                                "approval_context": {
                                    "discussion_passed": True,
                                    "risk_passed": True,
                                    "audit_passed": True,
                                    "discussion_case_id": case_id,
                                    "risk_gate": promoted_case["risk_gate"],
                                    "audit_gate": promoted_case["audit_gate"],
                                },
                            },
                        ).json()["ok"]
                    )

                    auto_payload = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-auto-learned-apply-001",
                            "account_id": "sim-001",
                            "agent": {"agent_id": "ashare-strategy", "role": "strategy"},
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "寻找趋势轮动机会",
                                "market_hypothesis": "趋势轮动增强，热点扩散",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {
                                "scope": "custom",
                                "symbol_pool": ["600519.SH", "000001.SZ"],
                                "sector_whitelist": ["机器人"],
                            },
                            "strategy": {
                                "playbooks": [{"id": "trend_acceleration_test", "version": "v1", "weight": 0.7, "params": {}}],
                                "factors": [{"id": "sector_heat_score_test", "group": "sentiment", "version": "v1", "weight": 0.1, "params": {}}],
                            },
                            "market_context": {"focus_topics": ["机器人"]},
                            "learned_asset_options": {
                                "auto_apply_active": True,
                                "max_auto_apply": 1,
                                "preferred_tags": ["trend", "rotation"],
                            },
                            "output": {"max_candidates": 2},
                        },
                    ).json()
                    self.assertEqual(
                        auto_payload["repository"]["auto_selected_learned_assets"][0]["id"],
                        "agent_combo_auto_001",
                    )
                    self.assertEqual(
                        auto_payload["explanations"]["learned_asset_summary"]["auto_selected_asset_ids"],
                        ["agent_combo_auto_001"],
                    )
                    self.assertEqual(
                        auto_payload["candidates"][0]["learned_asset_adjustments"]["active_assets"],
                        ["agent_combo_auto_001"],
                    )
                    trade_date = str(auto_payload["generated_at"])[:10]
                    research_packet = client.get(
                        f"/system/discussions/agent-packets?trade_date={trade_date}&agent_id=ashare-research"
                    ).json()
                    self.assertTrue(research_packet["ok"])
                    self.assertTrue(research_packet["learned_asset_review_guidance"]["available"])
                    self.assertEqual(
                        research_packet["learned_asset_review_guidance"]["auto_selected_asset_ids"],
                        ["agent_combo_auto_001"],
                    )
                    self.assertTrue(research_packet["learned_asset_review_guidance"]["auto_apply_active"])
                    self.assertTrue(
                        any("催化" in item or "失配" in item for item in research_packet["learned_asset_review_guidance"]["agent_questions"])
                    )

                    risk_packet = client.get(
                        f"/system/discussions/agent-packets?trade_date={trade_date}&agent_id=ashare-risk"
                    ).json()
                    self.assertTrue(risk_packet["ok"])
                    self.assertTrue(
                        any("过拟合" in item or "拥挤" in item for item in risk_packet["learned_asset_review_guidance"]["agent_questions"])
                    )

                    audit_packet = client.get(
                        f"/system/discussions/agent-packets?trade_date={trade_date}&agent_id=ashare-audit"
                    ).json()
                    self.assertTrue(audit_packet["ok"])
                    self.assertIn(
                        "learned_asset_review_guidance",
                        audit_packet["protocol"]["required_round_2_context"],
                    )
                    self.assertTrue(
                        any("排序" in item or "证据链" in item for item in audit_packet["learned_asset_review_guidance"]["agent_questions"])
                    )

                    bootstrap_payload = client.post(
                        "/system/discussions/cycles/bootstrap",
                        json={"trade_date": trade_date},
                    ).json()
                    self.assertTrue(bootstrap_payload["ok"])
                    finalize_payload = client.post(
                        f"/system/discussions/cycles/{trade_date}/finalize"
                    ).json()
                    self.assertTrue(finalize_payload["ok"])

                    execution_precheck_payload = client.get(
                        f"/system/discussions/execution-precheck?trade_date={trade_date}&account_id=sim-001"
                    ).json()
                    self.assertTrue(execution_precheck_payload["ok"])
                    self.assertTrue(execution_precheck_payload["learned_asset_execution_guidance"]["available"])
                    self.assertTrue(
                        execution_precheck_payload["learned_asset_execution_guidance"]["requires_cautious_preview"]
                    )

                    execution_intents_payload = client.get(
                        f"/system/discussions/execution-intents?trade_date={trade_date}&account_id=sim-001"
                    ).json()
                    self.assertTrue(execution_intents_payload["ok"])
                    self.assertEqual(
                        execution_intents_payload["learned_asset_execution_guidance"]["auto_selected_asset_ids"],
                        ["agent_combo_auto_001"],
                    )
                    self.assertTrue(
                        any(
                            "预演" in item or "限额" in item
                            for item in execution_intents_payload["learned_asset_execution_guidance"]["summary_lines"]
                        )
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_from_brief_builds_compose_request_for_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    response = client.post(
                        "/runtime/jobs/compose-from-brief",
                        json={
                            "request_id": "compose-brief-test-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-research",
                                "role": "research",
                            },
                            "objective": "寻找盘中可讨论的新机会",
                            "market_hypothesis": "机器人扩散，回避银行",
                            "focus_sectors": ["机器人"],
                            "avoid_sectors": ["银行"],
                            "excluded_theme_keywords": ["银行"],
                            "playbooks": ["trend_acceleration", "sector_resonance"],
                            "factors": ["momentum_slope", "sector_heat_score"],
                            "weights": {
                                "playbooks": {"trend_acceleration": 0.7, "sector_resonance": 0.3},
                                "factors": {"momentum_slope": 0.6, "sector_heat_score": 0.4},
                            },
                            "auto_apply_active_learned_assets": True,
                            "max_auto_apply_learned_assets": 2,
                            "preferred_learned_asset_tags": ["trend", "rotation"],
                            "blocked_learned_asset_ids": ["deprecated_combo_x"],
                            "max_candidates": 5,
                            "max_single_amount": 20000,
                            "equity_position_limit": 0.3,
                            "max_total_position": 0.5,
                            "max_single_position": 0.2,
                            "daily_loss_limit": 0.03,
                            "blocked_symbols": ["600000.SH"],
                            "allowed_regimes": ["trend", "rotation"],
                            "blocked_regimes": ["defensive"],
                            "min_regime_score": 0.5,
                            "require_fresh_snapshot": True,
                            "max_snapshot_age_seconds": 180,
                            "max_price_deviation_pct": 0.02,
                            "notes": ["盘中优先找板块共振和趋势加速"],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["brief"]["intent_mode"], "opportunity_scan")
                    self.assertEqual(payload["brief"]["focus_sectors"], ["机器人"])
                    self.assertEqual(payload["compose_request"]["universe"]["sector_blacklist"], ["银行"])
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["user_preferences"]["equity_position_limit"],
                        0.3,
                    )
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["risk_rules"]["max_total_position"],
                        0.5,
                    )
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["market_rules"]["allowed_regimes"],
                        ["trend", "rotation"],
                    )
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["execution_barriers"]["max_snapshot_age_seconds"],
                        180,
                    )
                    self.assertEqual(payload["brief"]["blocked_symbols"], ["600000.SH"])
                    self.assertEqual(payload["brief"]["custom_constraints"]["hard_filters"], {})
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["playbooks"][0]["id"],
                        "trend_acceleration",
                    )
                    self.assertTrue(payload["brief_execution"]["custom_profile_independent"])
                    self.assertFalse(payload["brief_execution"]["used_reference_profile"])
                    self.assertEqual(payload["brief_execution"]["profile_dependency"], "none")
                    self.assertTrue(payload["brief_execution"]["validated_compose_request"])
                    self.assertIn("trend_acceleration", payload["brief_execution"]["selected_playbooks"])
                    self.assertIn("momentum_slope", payload["brief_execution"]["selected_factors"])
                    self.assertTrue(payload["compose_request"]["learned_asset_options"]["auto_apply_active"])
                    self.assertEqual(payload["compose_request"]["learned_asset_options"]["max_auto_apply"], 2)
                    self.assertEqual(
                        payload["compose_request"]["learned_asset_options"]["preferred_tags"],
                        ["trend", "rotation"],
                    )
                    self.assertEqual(
                        payload["compose_request"]["learned_asset_options"]["blocked_asset_ids"],
                        ["deprecated_combo_x"],
                    )
                    self.assertIn("candidates", payload)
                    self.assertIn("proposal_packet", payload)
                    self.assertIn("repository", payload)
                    self.assertIn("composition_manifest", payload)
                    self.assertEqual(payload["composition_manifest"]["playbooks"][0]["id"], "trend_acceleration")
                    self.assertEqual(payload["composition_manifest"]["factors"][0]["id"], "momentum_slope")
                    self.assertIn("filters", payload["composition_manifest"])
                    self.assertIn("evidence", payload["composition_manifest"])
                    self.assertIn("candidate_evidence_preview", payload["composition_manifest"]["evidence"])
                    used_assets = payload["repository"]["used_assets"]
                    self.assertTrue(used_assets)
                    self.assertEqual(used_assets[0]["runtime_policy"]["mode"], "default")
                    self.assertTrue(
                        any(case.runtime_snapshot.source == "sector_heat_scan" for case in candidate_case_service.list_cases(limit=20))
                    )

                    invalid_response = client.post(
                        "/runtime/jobs/compose-from-brief",
                        json={
                            "request_id": "compose-brief-test-bad",
                            "account_id": "sim-001",
                            "objective": "测试非法战法",
                            "playbooks": ["unknown_playbook"],
                            "factors": ["momentum_slope"],
                        },
                    ).json()
                    self.assertFalse(invalid_response["ok"])
                    self.assertIn("未注册的战法", invalid_response["error"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_from_brief_supports_custom_specs_constraints_and_market_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    response = client.post(
                        "/runtime/jobs/compose-from-brief",
                        json={
                            "request_id": "compose-brief-custom-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-strategy",
                                "role": "strategy",
                            },
                            "intent_mode": "replacement_review",
                            "objective": "比较持仓替换与增量机会",
                            "market_hypothesis": "主线轮动，但旧仓强度开始分化",
                            "holding_symbols": ["600000.SH"],
                            "playbook_specs": [
                                {
                                    "id": "position_replacement",
                                    "weight": 0.8,
                                    "params": {"replace_mode": "relative_strength"},
                                }
                            ],
                            "factor_specs": [
                                {
                                    "id": "momentum_slope",
                                    "group": "trend",
                                    "weight": 0.7,
                                    "params": {"window": 5},
                                },
                                {
                                    "id": "sector_heat_score",
                                    "group": "sector",
                                    "weight": 0.3,
                                    "params": {"focus_only": True},
                                },
                            ],
                            "ranking_primary_score": "selection_score",
                            "ranking_secondary_keys": ["momentum_slope"],
                            "custom_constraints": {
                                "hard_filters": {"blocked_symbols": ["300750.SZ"]},
                                "market_rules": {"allowed_regimes": ["rotation"]},
                                "risk_rules": {"daily_loss_limit": 0.02},
                                "execution_barriers": {"max_snapshot_age_seconds": 90},
                            },
                            "market_context": {
                                "desk_view": "replacement_first",
                                "operator_note": "优先比较旧仓和新票的相对性价比",
                            },
                            "notes": ["先做替换评估，再决定是否扩仓"],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["brief"]["intent_mode"], "replacement_review")
                    self.assertEqual(payload["compose_request"]["intent"]["mode"], "replacement_review")
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["playbooks"][0]["params"]["replace_mode"],
                        "relative_strength",
                    )
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["factors"][0]["params"]["window"],
                        5,
                    )
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["ranking"]["primary_score"],
                        "selection_score",
                    )
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["ranking"]["secondary_keys"],
                        ["momentum_slope"],
                    )
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["market_rules"]["allowed_regimes"],
                        ["rotation"],
                    )
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["risk_rules"]["daily_loss_limit"],
                        0.02,
                    )
                    self.assertEqual(
                        payload["compose_request"]["constraints"]["execution_barriers"]["max_snapshot_age_seconds"],
                        90,
                    )
                    self.assertEqual(
                        payload["compose_request"]["market_context"]["desk_view"],
                        "replacement_first",
                    )
                    self.assertEqual(
                        payload["compose_request"]["market_context"]["holding_symbols"],
                        ["600000.SH"],
                    )
                    self.assertTrue(
                        any(
                            case.runtime_snapshot.source == "position_monitor_scan"
                            for case in candidate_case_service.list_cases(limit=20)
                        )
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    factors_payload = client.get("/runtime/factors").json()
                    self.assertGreaterEqual(len(factors_payload["items"]), 48)
                    self.assertLessEqual(len(factors_payload["items"]), 64)
                    factor_groups = {item["group"] for item in factors_payload["items"]}
                    self.assertTrue(
                        {
                            "trend_momentum",
                            "reversal_repair",
                            "volume_liquidity",
                            "capital_behavior",
                            "sector_heat",
                            "event_catalyst",
                            "micro_structure",
                            "risk_penalty",
                            "valuation_filter",
                            "position_management",
                        }.issubset(factor_groups)
                    )
                    factor_ids = {item["id"] for item in factors_payload["items"]}
                    self.assertIn("momentum_slope", factor_ids)
                    self.assertIn("sector_heat_score", factor_ids)
                    self.assertIn("portfolio_fit_score", factor_ids)
                    factor_item = next(item for item in factors_payload["items"] if item["id"] == "momentum_slope")
                    self.assertIn("params_schema", factor_item)
                    self.assertIn("evidence_schema", factor_item)
                    self.assertEqual(factor_item["source"], "seed.factor_library.20260417")
                    self.assertEqual(factor_item["author"], "system.factor_library")
                    self.assertEqual(factor_item["status"], "active")

                    playbooks_payload = client.get("/runtime/playbooks").json()
                    self.assertGreaterEqual(len(playbooks_payload["items"]), 18)
                    self.assertLessEqual(len(playbooks_payload["items"]), 24)
                    playbook_ids = {item["id"] for item in playbooks_payload["items"]}
                    self.assertIn("sector_resonance", playbook_ids)
                    self.assertIn("trend_acceleration", playbook_ids)
                    self.assertIn("position_trim_redeploy", playbook_ids)
                    playbook_item = next(item for item in playbooks_payload["items"] if item["id"] == "sector_resonance")
                    self.assertIn("params_schema", playbook_item)
                    self.assertIn("evidence_schema", playbook_item)
                    self.assertEqual(playbook_item["source"], "seed.playbook_library.20260417")
                    self.assertEqual(playbook_item["author"], "system.playbook_library")
                    self.assertEqual(playbook_item["status"], "active")

                    capabilities_payload = client.get("/runtime/capabilities").json()
                    self.assertTrue(capabilities_payload["ok"])
                    self.assertTrue(capabilities_payload["compose_available"])
                    self.assertTrue(capabilities_payload["factors"][0]["executable"])
                    self.assertIn("runtime_role", capabilities_payload["factors"][0])
                    self.assertIn("agent_usage", capabilities_payload["playbooks"][0])
                    self.assertIn("compose_request_example", capabilities_payload)
                    self.assertIn("compose_brief_contract", capabilities_payload)
                    strategy_endpoints = next(
                        item["endpoints"]
                        for item in capabilities_payload["system_tools"]
                        if item["category"] == "strategy_and_runtime"
                    )
                    self.assertIn("/runtime/jobs/news-catalyst", strategy_endpoints)
                    self.assertIn("/runtime/jobs/tail-ambush", strategy_endpoints)
                    self.assertIn("compose_brief_example", capabilities_payload)
                    self.assertIn("task_profiles", capabilities_payload)
                    self.assertIn("leader_playbook", capabilities_payload["task_profiles"])
                    self.assertIn("oversold_rebound", capabilities_payload["task_profiles"])
                    self.assertIn("selection_rule", capabilities_payload["compose_brief_contract"])
                    self.assertIn("task_dimensions", capabilities_payload)
                    self.assertIn("composition_rules", capabilities_payload)
                    self.assertIn("anti_misuse_rules", capabilities_payload)
                    self.assertIn("profile_mix_examples", capabilities_payload)
                    self.assertEqual(
                        capabilities_payload["task_profiles"]["leader_playbook"]["binding_level"],
                        "advisory_only",
                    )
                    self.assertIn("compose_request_schema", capabilities_payload)
                    self.assertIn("compose_contract", capabilities_payload)
                    self.assertIn("compose_response_contract", capabilities_payload)
                    self.assertIn("compose_from_brief_response_contract", capabilities_payload)
                    self.assertIn("supplemental_skill_pack", capabilities_payload)
                    self.assertIn("skills", capabilities_payload["supplemental_skill_pack"])
                    self.assertIn("comparison_method", capabilities_payload["supplemental_skill_pack"])
                    self.assertIn("constraint_pack_notes", capabilities_payload)
                    self.assertIn("evaluation_ledger_notes", capabilities_payload)
                    self.assertIn("applied_constraints", capabilities_payload["compose_response_contract"])
                    self.assertIn("daily_loss_limit", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("allowed_regimes", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("max_price_deviation_pct", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("auto_apply_active_learned_assets", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("preferred_learned_asset_tags", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("playbook_versions", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("factor_versions", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("intent_mode", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("playbook_specs", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("factor_specs", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("custom_constraints", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertIn("market_context", capabilities_payload["compose_brief_contract"]["fields"])
                    self.assertTrue(capabilities_payload["compose_brief_contract"]["custom_first"])
                    self.assertIn("next_action", capabilities_payload["compose_brief_contract"]["orchestration_trace_fields"])
                    self.assertIn("learned_asset_options", capabilities_payload["compose_request_schema"])
                    self.assertIn("repository_query", capabilities_payload["compose_request_schema"])
                    self.assertIn("repository", capabilities_payload["compose_response_contract"])
                    self.assertIn("composition_manifest", capabilities_payload["compose_response_contract"])
                    self.assertIn("brief_execution", capabilities_payload["compose_from_brief_response_contract"])
                    self.assertIn("risk_rules", capabilities_payload["compose_request_example"]["constraints"])
                    self.assertIn("market_rules", capabilities_payload["compose_request_example"]["constraints"])
                    self.assertIn("execution_barriers", capabilities_payload["compose_request_example"]["constraints"])
                    self.assertIn("learned_asset_options", capabilities_payload["compose_request_example"])
                    self.assertIn("blocked_symbols", capabilities_payload["compose_brief_example"])
                    self.assertTrue(capabilities_payload["compose_brief_example"]["auto_apply_active_learned_assets"])
                    self.assertIn("preferred_learned_asset_tags", capabilities_payload["compose_brief_example"])
                    self.assertIn("playbook_versions", capabilities_payload["compose_brief_example"])
                    self.assertIn("factor_versions", capabilities_payload["compose_brief_example"])
                    self.assertEqual(
                        capabilities_payload["learned_asset_flow"]["active_requirements"]["discussion_passed"],
                        True,
                    )
                    self.assertIn("agent_usage_rules", capabilities_payload["learned_asset_flow"])
                    self.assertIn("repository_runtime_modes", capabilities_payload["learned_asset_flow"])
                    self.assertIn("explicit_only", capabilities_payload["learned_asset_flow"]["repository_runtime_modes"])
                    self.assertIn("version_selection_rules", capabilities_payload["learned_asset_flow"])
                    self.assertIn("真实结果覆盖纯状态推荐", "".join(capabilities_payload["learned_asset_flow"]["version_selection_rules"]))
                    self.assertIn("race_governance_actions", capabilities_payload["learned_asset_flow"])
                    self.assertIn("review_cutover", capabilities_payload["learned_asset_flow"]["race_governance_actions"])
                    self.assertIn("race_panel_usage", capabilities_payload["learned_asset_flow"])
                    self.assertIn("repository_panels", capabilities_payload["compose_request_schema"])
                    self.assertEqual(
                        capabilities_payload["learned_asset_flow"]["active_requirements"]["discussion_case_id"],
                        "required",
                    )
                    self.assertIn(
                        "vote_detail",
                        capabilities_payload["learned_asset_flow"]["discussion_binding_fields"],
                    )
                    self.assertIn(
                        "reply_pack_summary",
                        capabilities_payload["learned_asset_flow"]["discussion_binding_fields"],
                    )
                    self.assertIn(
                        "final_brief_summary",
                        capabilities_payload["learned_asset_flow"]["discussion_binding_fields"],
                    )
                    self.assertIn(
                        "finalize_packet_summary",
                        capabilities_payload["learned_asset_flow"]["discussion_binding_fields"],
                    )
                    self.assertIn("resolve", capabilities_payload["learned_asset_flow"]["advice_endpoints"])
                    self.assertIn("advice_governance_rules", capabilities_payload["learned_asset_flow"])
                    self.assertIn("playbook_notes", capabilities_payload)
                    self.assertIn("tail_close_ambush", capabilities_payload["playbook_notes"])
                    self.assertTrue(capabilities_payload["task_profile_usage"])
                    self.assertIn("recommended_playbooks", capabilities_payload["task_profiles"]["day_trading"])
                    self.assertTrue(capabilities_payload["profile_mix_examples"])
                    self.assertTrue(any(item["id"] == "backtest_engine" for item in capabilities_payload["supplemental_skill_pack"]["skills"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_evaluation_panel_exposes_factor_playbook_and_combo_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    compose_response = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-ledger-panel-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-strategy",
                                "role": "strategy",
                                "session_id": "cycle-20260417-ledger",
                                "proposal_id": "proposal-ledger-001",
                            },
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "验证评估面板已暴露 factor/playbook/combo ledger",
                                "market_hypothesis": "热点回暖，新能源链有政策和订单催化，适合趋势加速与板块共振",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {
                                "scope": "custom",
                                "symbol_pool": ["600519.SH", "000001.SZ"],
                            },
                            "strategy": {
                                "playbooks": [
                                    {"id": "trend_acceleration", "version": "v1", "weight": 0.7, "params": {}},
                                    {"id": "sector_resonance", "version": "v1", "weight": 0.3, "params": {}},
                                ],
                                "factors": [
                                    {"id": "sector_heat_score", "group": "sector_heat", "version": "v1", "weight": 0.4, "params": {}},
                                    {"id": "momentum_slope", "group": "trend_momentum", "version": "v1", "weight": 0.6, "params": {}},
                                ],
                            },
                            "constraints": {},
                            "output": {
                                "max_candidates": 2,
                                "include_filtered_reasons": True,
                                "include_score_breakdown": True,
                                "include_evidence": True,
                            },
                        },
                    )
                    self.assertEqual(compose_response.status_code, 200)
                    compose_payload = compose_response.json()
                    trace_id = compose_payload["evaluation_trace"]["trace_id"]
                    lead_symbol = compose_payload["candidates"][0]["symbol"]

                    feedback_payload = client.post(
                        "/runtime/evaluations/feedback",
                        json={
                            "trace_id": trace_id,
                            "adoption": {
                                "status": "adopted",
                                "adopted_symbols": [lead_symbol],
                                "selected_count": 1,
                                "trade_date": "2026-04-17",
                                "updated_by": "ashare-strategy",
                            },
                            "outcome": {
                                "status": "settled",
                                "posterior_metrics": {
                                    "avg_next_day_close_pct": 0.031,
                                    "max_drawdown": 0.012,
                                    "filled_symbol_count": 1,
                                },
                                "updated_by": "ashare-audit",
                            },
                        },
                    ).json()
                    self.assertTrue(feedback_payload["ok"])

                    panel_payload = client.get("/runtime/evaluations/panel?limit=5").json()
                    self.assertTrue(panel_payload["ok"])
                    self.assertGreaterEqual(panel_payload["summary"]["factor_ledger_count"], 2)
                    self.assertGreaterEqual(panel_payload["summary"]["playbook_ledger_count"], 2)
                    self.assertGreaterEqual(panel_payload["summary"]["compose_combo_ledger_count"], 1)

                    factor_map = {item["key"]: item for item in panel_payload["factor_ledger"]}
                    self.assertIn("sector_heat_score:v1", factor_map)
                    self.assertIn("momentum_slope:v1", factor_map)
                    self.assertEqual(factor_map["sector_heat_score:v1"]["usage_count"], 1)
                    self.assertIn("effectiveness", factor_map["sector_heat_score:v1"])
                    self.assertIn("last_5d", factor_map["sector_heat_score:v1"]["effectiveness"])

                    playbook_map = {item["key"]: item for item in panel_payload["playbook_ledger"]}
                    self.assertIn("trend_acceleration:v1", playbook_map)
                    self.assertIn("sector_resonance:v1", playbook_map)
                    self.assertAlmostEqual(playbook_map["trend_acceleration:v1"]["live_trigger_rate"], 1.0, places=6)
                    self.assertIn("recent_failure_modes", playbook_map["trend_acceleration:v1"])

                    combo_item = next(
                        item
                        for item in panel_payload["compose_combo_ledger"]
                        if {row["id"] for row in item["playbooks"]} == {"trend_acceleration", "sector_resonance"}
                    )
                    self.assertEqual(combo_item["use_count"], 1)
                    self.assertEqual(combo_item["adopted_count"], 1)
                    self.assertEqual(combo_item["settled_count"], 1)
                    self.assertEqual(combo_item["filled_count"], 1)
                    self.assertTrue(any(row["id"] == "momentum_slope" for row in combo_item["factors"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_from_brief_supports_version_override_and_blocks_archived_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = str(Path(tmp_dir) / "storage")
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            experimental_playbook_id = "gray_rotation_playbook_test"
            experimental_factor_id = "gray_rotation_factor_test"
            archived_factor_id = "archived_factor_gate_test"
            if playbook_registry.get(experimental_playbook_id, "v2") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=experimental_playbook_id,
                        name="灰度轮动战法",
                        version="v2",
                        description="用于验证显式试验版战法可由 brief 指定版本调用",
                        status="experimental",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.72, "evidence": ["灰度轮动战法", "测试执行器"]},
                )
            if factor_registry.get(experimental_factor_id, "v2") is None:
                factor_registry.register(
                    FactorDefinition(
                        id=experimental_factor_id,
                        name="灰度轮动因子",
                        version="v2",
                        group="rotation",
                        description="用于验证显式试验版因子可由 brief 指定版本调用",
                        status="experimental",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.41, "evidence": ["灰度轮动因子", "测试执行器"]},
                )
            if factor_registry.get(archived_factor_id, "v9") is None:
                factor_registry.register(
                    FactorDefinition(
                        id=archived_factor_id,
                        name="已归档测试因子",
                        version="v9",
                        group="risk",
                        description="用于验证 archived 资产会被入口拦截",
                        status="archived",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.0, "evidence": ["已归档测试因子"]},
                )

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    explicit_only_repo = client.get("/runtime/strategy-repository?runtime_mode=explicit_only").json()
                    explicit_only_ids = {item["id"] for item in explicit_only_repo["items"]}
                    self.assertIn(experimental_playbook_id, explicit_only_ids)
                    self.assertIn(experimental_factor_id, explicit_only_ids)

                    response = client.post(
                        "/runtime/jobs/compose-from-brief",
                        json={
                            "request_id": "compose-brief-gray-test-001",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-strategy",
                                "role": "strategy",
                            },
                            "objective": "验证实验版资产可显式调用",
                            "market_hypothesis": "轮动假设增强",
                            "focus_sectors": ["机器人"],
                            "playbooks": [experimental_playbook_id],
                            "factors": [experimental_factor_id],
                            "playbook_versions": {experimental_playbook_id: "v2"},
                            "factor_versions": {experimental_factor_id: "v2"},
                            "weights": {
                                "playbooks": {experimental_playbook_id: 1.0},
                                "factors": {experimental_factor_id: 1.0},
                            },
                            "max_candidates": 3,
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["brief"]["playbook_versions"][experimental_playbook_id], "v2")
                    self.assertEqual(payload["brief"]["factor_versions"][experimental_factor_id], "v2")
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["playbooks"][0]["version"],
                        "v2",
                    )
                    self.assertEqual(
                        payload["compose_request"]["strategy"]["factors"][0]["version"],
                        "v2",
                    )
                    self.assertEqual(
                        payload["repository"]["used_assets"][0]["runtime_policy"]["mode"],
                        "explicit_only",
                    )

                    blocked_payload = client.post(
                        "/runtime/jobs/compose-from-brief",
                        json={
                            "request_id": "compose-brief-gray-test-002",
                            "account_id": "sim-001",
                            "agent": {
                                "agent_id": "ashare-strategy",
                                "role": "strategy",
                            },
                            "objective": "验证 archived 资产阻断",
                            "playbooks": ["trend_acceleration"],
                            "factors": [archived_factor_id],
                            "factor_versions": {archived_factor_id: "v9"},
                        },
                    ).json()
                    self.assertFalse(blocked_payload["ok"])
                    self.assertIn("已被仓库阻断", blocked_payload["error"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_compose_direct_request_respects_runtime_policy_for_strategy_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = str(Path(tmp_dir) / "storage")
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            experimental_playbook_id = "direct_gray_playbook_test"
            archived_factor_id = "direct_archived_factor_test"
            if playbook_registry.get(experimental_playbook_id, "v3") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=experimental_playbook_id,
                        name="直连灰度战法",
                        version="v3",
                        description="用于验证 direct compose 可显式消费 experimental 资产",
                        status="experimental",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.68, "evidence": ["直连灰度战法", "测试执行器"]},
                )
            if factor_registry.get(archived_factor_id, "v5") is None:
                factor_registry.register(
                    FactorDefinition(
                        id=archived_factor_id,
                        name="直连归档因子",
                        version="v5",
                        group="risk",
                        description="用于验证 direct compose 会阻断 archived 资产",
                        status="archived",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.0, "evidence": ["直连归档因子"]},
                )

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    ok_response = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-direct-policy-ok-001",
                            "account_id": "sim-001",
                            "agent": {"agent_id": "ashare-strategy", "role": "strategy"},
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "验证 direct compose 显式试验资产",
                                "market_hypothesis": "测试灰度资产",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {"scope": "main-board", "symbol_pool": ["600519.SH", "000001.SZ"]},
                            "strategy": {
                                "playbooks": [
                                    {"id": experimental_playbook_id, "version": "v3", "weight": 1.0, "params": {}}
                                ],
                                "factors": [
                                    {"id": "momentum_slope", "group": "momentum", "version": "v1", "weight": 1.0, "params": {}}
                                ],
                                "ranking": {"primary_score": "composite_score", "secondary_keys": ["momentum_slope"]},
                            },
                            "constraints": {},
                            "output": {"max_candidates": 3},
                        },
                    )
                    self.assertEqual(ok_response.status_code, 200)
                    ok_payload = ok_response.json()
                    self.assertEqual(ok_payload["repository"]["used_assets"][0]["runtime_policy"]["mode"], "explicit_only")

                    blocked_response = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-direct-policy-block-001",
                            "account_id": "sim-001",
                            "agent": {"agent_id": "ashare-strategy", "role": "strategy"},
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "验证 direct compose 阻断 archived 资产",
                                "market_hypothesis": "测试阻断",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {"scope": "main-board", "symbol_pool": ["600519.SH", "000001.SZ"]},
                            "strategy": {
                                "playbooks": [
                                    {"id": "trend_acceleration", "version": "v1", "weight": 1.0, "params": {}}
                                ],
                                "factors": [
                                    {"id": archived_factor_id, "group": "risk", "version": "v5", "weight": 1.0, "params": {}}
                                ],
                                "ranking": {"primary_score": "composite_score", "secondary_keys": [archived_factor_id]},
                            },
                            "constraints": {},
                            "output": {"max_candidates": 3},
                        },
                    ).json()
                    self.assertFalse(blocked_response["ok"])
                    self.assertIn("已被仓库阻断", blocked_response["error"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_strategy_repository_exposes_version_view_and_recommended_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = str(Path(tmp_dir) / "storage")
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            playbook_id = "version_view_playbook_test"
            if playbook_registry.get(playbook_id, "v2") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=playbook_id,
                        name="版本视图战法",
                        version="v2",
                        description="实验版",
                        status="experimental",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.55, "evidence": ["版本视图战法", "实验版"]},
                )
            if playbook_registry.get(playbook_id, "v1") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=playbook_id,
                        name="版本视图战法",
                        version="v1",
                        description="正式版",
                        status="active",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.61, "evidence": ["版本视图战法", "正式版"]},
                )
            if playbook_registry.get(playbook_id, "v0") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=playbook_id,
                        name="版本视图战法",
                        version="v0",
                        description="归档版",
                        status="archived",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.0, "evidence": ["版本视图战法", "归档版"]},
                )

            try:
                with TestClient(create_app()) as client:
                    payload = client.get(f"/runtime/strategy-repository?asset_type=playbook&asset_id={playbook_id}").json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(len(payload["version_view"]), 1)
                    item = payload["version_view"][0]
                    self.assertEqual(item["id"], playbook_id)
                    self.assertEqual(item["recommended_version"], "v1")
                    self.assertEqual(item["recommended_runtime_mode"], "default")
                    self.assertIn("v2", item["explicit_candidate_versions"])
                    self.assertIn("v0", item["blocked_versions"])
                    self.assertIn("race_summary", item)
                    self.assertEqual(item["race_summary"]["recommended_version"], "v1")
                    self.assertFalse(item["race_summary"]["has_real_outcome"])
                    self.assertIn("governance_suggestion", item)
                    self.assertEqual(item["governance_suggestion"]["recommended_action"], "observe_only")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_strategy_repository_race_summary_prefers_real_outcome_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = str(Path(tmp_dir) / "storage")
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            playbook_id = "race_winner_playbook_test"
            if playbook_registry.get(playbook_id, "v1") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=playbook_id,
                        name="赛马战法",
                        version="v1",
                        description="默认正式版",
                        status="active",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.45, "evidence": ["赛马战法", "v1"]},
                )
            if playbook_registry.get(playbook_id, "v2") is None:
                playbook_registry.register(
                    PlaybookDefinition(
                        id=playbook_id,
                        name="赛马战法",
                        version="v2",
                        description="灰度试验版",
                        status="experimental",
                        source="unit_test",
                        author="test",
                    ),
                    executor=lambda *_args, **_kwargs: {"score": 0.72, "evidence": ["赛马战法", "v2"]},
                )

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    compose_payload = client.post(
                        "/runtime/jobs/compose",
                        json={
                            "request_id": "compose-race-summary-001",
                            "account_id": "sim-001",
                            "agent": {"agent_id": "ashare-strategy", "role": "strategy"},
                            "intent": {
                                "mode": "opportunity_scan",
                                "objective": "验证版本赛马摘要优先真实结果",
                                "market_hypothesis": "灰度版已有真实结果",
                                "trade_horizon": "intraday_to_overnight",
                            },
                            "universe": {"scope": "main-board", "symbol_pool": ["600519.SH", "000001.SZ"]},
                            "strategy": {
                                "playbooks": [{"id": playbook_id, "version": "v2", "weight": 1.0, "params": {}}],
                                "factors": [{"id": "momentum_slope", "group": "momentum", "version": "v1", "weight": 1.0, "params": {}}],
                                "ranking": {"primary_score": "composite_score", "secondary_keys": ["momentum_slope"]},
                            },
                            "constraints": {},
                            "output": {"max_candidates": 3},
                        },
                    ).json()
                    self.assertEqual(compose_payload["repository"]["used_assets"][0]["runtime_policy"]["mode"], "explicit_only")
                    trace_id = compose_payload["evaluation_trace"]["trace_id"]

                    feedback_payload = client.post(
                        "/runtime/evaluations/feedback",
                        json={
                            "trace_id": trace_id,
                            "adoption": {
                                "status": "adopted",
                                "adopted_symbols": ["600519.SH"],
                                "updated_by": "ashare-strategy",
                            },
                            "outcome": {
                                "status": "settled",
                                "posterior_metrics": {"hit_rate": 0.91},
                                "updated_by": "ashare-audit",
                            },
                        },
                    ).json()
                    self.assertTrue(feedback_payload["ok"])

                    repo_payload = client.get(
                        f"/runtime/strategy-repository?asset_type=playbook&asset_id={playbook_id}"
                    ).json()
                    item = repo_payload["version_view"][0]
                    self.assertEqual(item["recommended_version"], "v1")
                    self.assertEqual(item["race_summary"]["recommended_version"], "v2")
                    self.assertTrue(item["race_summary"]["has_real_outcome"])
                    self.assertIn("adopted=1", item["race_summary"]["recommended_reason"])
                    self.assertIn("settled=1", item["race_summary"]["recommended_reason"])
                    self.assertEqual(item["governance_suggestion"]["recommended_action"], "review_cutover")
                    self.assertEqual(repo_payload["governance_summary"]["review_cutover_count"], 1)

                    panel_payload = client.get(
                        f"/runtime/strategy-repository/panel?asset_type=playbook&asset_id={playbook_id}"
                    ).json()
                    self.assertTrue(panel_payload["ok"])
                    self.assertEqual(panel_payload["summary"]["review_cutover_count"], 1)
                    self.assertEqual(panel_payload["summary"]["high_attention_count"], 1)
                    self.assertEqual(len(panel_payload["high_attention_items"]), 1)
                    self.assertEqual(
                        panel_payload["high_attention_items"][0]["governance_suggestion"]["recommended_action"],
                        "review_cutover",
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_account_state_budget_respects_equity_position_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=100000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []
                    adjust_payload = client.post(
                        "/system/adjustments/natural-language",
                        json={
                            "instruction": "仓位调到3成，逆回购目标改成0，逆回购保留改成0",
                            "apply": True,
                        },
                    ).json()
                    self.assertTrue(adjust_payload["ok"])

                    payload = client.get("/system/account-state?account_id=sim-001&refresh=true").json()
                    self.assertTrue(payload["ok"])
                    metrics = payload["metrics"]
                    self.assertEqual(metrics["equity_position_limit"], 0.3)
                    self.assertEqual(metrics["reverse_repo_target_ratio"], 0.0)
                    self.assertEqual(metrics["reverse_repo_reserved_amount"], 0.0)
                    self.assertAlmostEqual(metrics["stock_test_budget_amount"], 30000.0, places=2)
                    self.assertAlmostEqual(metrics["available_test_trade_value"], 30000.0, places=2)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_account_state_defaults_to_cache_and_supports_explicit_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=120000.0, cash=90000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    miss_payload = client.get("/system/account-state?account_id=sim-001").json()
                    self.assertFalse(miss_payload["ok"])
                    self.assertEqual(miss_payload["status"], "cache_unavailable")
                    self.assertEqual(miss_payload["cache_mode"], "miss")

                    refresh_payload = client.get("/system/account-state?account_id=sim-001&refresh=true").json()
                    self.assertTrue(refresh_payload["ok"])
                    self.assertEqual(refresh_payload["cache_mode"], "refreshed")
                    self.assertFalse(refresh_payload["trade_count_included"])

                    cached_payload = client.get("/system/account-state?account_id=sim-001").json()
                    self.assertTrue(cached_payload["ok"])
                    self.assertEqual(cached_payload["cache_mode"], "cached")
                    self.assertIn("age_seconds", cached_payload)
                    self.assertIn("metrics", cached_payload)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_account_state_cached_endpoint_does_not_requery_execution_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adapter = get_execution_adapter()
                    adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=88000.0)
                    adapter.positions["sim-001"] = []
                    adapter.trades["sim-001"] = []

                    warm_payload = client.get("/system/account-state?account_id=sim-001&refresh=true").json()
                    self.assertTrue(warm_payload["ok"])

                    with patch.object(adapter, "get_balance", side_effect=AssertionError("should not requery balance")), patch.object(
                        adapter,
                        "get_positions",
                        side_effect=AssertionError("should not requery positions"),
                    ), patch.object(
                        adapter,
                        "get_trades",
                        side_effect=AssertionError("should not requery trades"),
                    ):
                        cached_payload = client.get("/system/account-state?account_id=sim-001").json()

                    self.assertTrue(cached_payload["ok"])
                    self.assertEqual(cached_payload["cache_mode"], "cached")
                    self.assertEqual(cached_payload["metrics"]["cash"], 88000.0)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_event_bus_publish_sync_dispatches_without_running_loop(self) -> None:
        bus = EventBus()
        received: list[tuple[str | None, str]] = []

        def handler(event) -> None:
            received.append((event.symbol, event.event_type))

        bus.subscribe("NEGATIVE_NEWS", handler)
        event = {
            "event_type": "NEGATIVE_NEWS",
            "symbol": "000001.SZ",
            "payload": {"title": "test"},
            "priority": 2,
            "source": "unit-test",
        }
        from ashare_system.contracts import MarketEvent

        bus.publish_sync(MarketEvent.model_validate(event))
        self.assertEqual(received, [("000001.SZ", "NEGATIVE_NEWS")])

    def test_event_fetcher_incremental_emits_negative_events(self) -> None:
        fetcher = EventFetcher()
        bus = EventBus()
        received: list[str] = []

        def handler(event) -> None:
            received.append(str(event.symbol))

        bus.subscribe("NEGATIVE_NEWS", handler)
        sample_event = StructuredEvent(
            symbol="000001.SZ",
            event_type="earnings_warning",
            impact="negative",
            severity="high",
            title="业绩预警",
            published_at="2026-04-12T08:00:00",
            source="unit-test",
            tags=["negative"],
        )

        fetcher.fetch_today_events = lambda symbols, trade_date=None: EventFetchResult(  # type: ignore[method-assign]
            trade_date="2026-04-12",
            generated_at="2026-04-12T08:01:00",
            events=[sample_event],
            summary_lines=[],
        )

        result = fetcher.fetch_incremental(
            ["000001.SZ"],
            since="2026-04-12T07:59:00",
            trade_date="2026-04-12",
            event_bus=bus,
        )
        self.assertEqual(len(result.events), 1)
        self.assertEqual(received, ["000001.SZ"])

    def test_scheduler_normalizes_crontab_weekday_to_apscheduler_weekday(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        self.assertEqual(_normalize_crontab_day_of_week("1-5"), "0-4")
        self.assertEqual(_normalize_crontab_day_of_week("0,6"), "6,5")
        self.assertEqual(_normalize_crontab_day_of_week("*"), "*")

        tz = ZoneInfo("Asia/Shanghai")
        trigger = CronTrigger(minute="0", hour="13", day="*", month="*", day_of_week=_normalize_crontab_day_of_week("1-5"), timezone=tz)
        now = datetime(2026, 4, 13, 12, 59, 0, tzinfo=tz)  # Monday
        next_fire = trigger.get_next_fire_time(None, now)
        self.assertEqual(next_fire, datetime(2026, 4, 13, 13, 0, 0, tzinfo=tz))

    def test_nightly_sandbox_promotes_symbols_from_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = NightlySandbox(
                Path(tmp_dir),
                replay_packet_builder=lambda symbol: {"summary_lines": [f"{symbol} 回放完成"]},
            )
            result = sandbox.run_simulation(
                trade_date="2026-04-12",
                finalize_bundle={
                    "watchlist": [
                        {"symbol": "000001.SZ", "score": 1.0, "final_status": "watchlist"},
                    ]
                },
                attribution_report={"items": [{"symbol": "000001.SZ", "next_day_close_pct": -0.03}]},
                parameter_hints=[{"symbol": "000001.SZ", "action": "promote", "weight": 1.2, "reason": "低吸漏掉"}],
            )
            self.assertIn("000001.SZ", result.tomorrow_priorities)
            self.assertTrue(any("讨论回放" in line for line in result.simulation_log))

    def test_serving_store_as_of_time_blocks_future_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            layout = ensure_storage_layout(root)
            payload = {"trade_date": "2026-04-12", "generated_at": "2026-04-12T10:00:00", "value": 1}
            (layout.serving_root / "latest_runtime_context.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            store = ServingStore(root)
            self.assertIsNone(store.get_latest_runtime_context(as_of_time="2026-04-12T09:59:59"))
            self.assertEqual(store.get_latest_runtime_context(as_of_time="2026-04-12T10:00:00")["value"], 1)

    def test_review_board_pending_count_only_counts_approved_gateway_intents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                state_store = StateStore(Path(tmp_dir) / "meeting_state.json")
                state_store.set(
                    "pending_execution_intents",
                    [
                        {
                            "intent_id": "already-submitted",
                            "account_id": "test-account",
                            "symbol": "600519.SH",
                            "side": "BUY",
                            "quantity": 100,
                            "price": 1500.0,
                            "status": "submitted",
                            "claim": {"gateway_source_id": "windows-vm-a"},
                        }
                    ],
                )

                with TestClient(create_app()) as client:
                    payload = client.get("/system/reports/review-board").json()

                gateway = payload["sections"]["control_plane_gateway"]
                self.assertEqual(gateway["pending_intent_count"], 0)
                self.assertEqual(gateway["queued_for_gateway_count"], 0)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_discussion_blockers_ignore_selected_count_zero_when_execution_pool_exists(self) -> None:
        blockers = DiscussionCycleService._derive_blockers(
            {"selected": [], "selected_count": 0},
            execution_pool_case_ids=["case-1"],
        )
        self.assertEqual(blockers, [])

    def test_round_start_and_execution_intents_routes_refresh_monitoring_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]

                    bootstrap_response = client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
                    self.assertEqual(bootstrap_response.status_code, 200)

                    start_response = client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")
                    self.assertEqual(start_response.status_code, 200)

                    cadence_after_round_start = client.get("/system/monitoring/cadence").json()["polling_status"]
                    self.assertFalse(cadence_after_round_start["focus"]["due_now"])
                    self.assertEqual(cadence_after_round_start["focus"]["last_trigger"], "discussion_round_1_start")

                    intents_response = client.get(
                        f"/system/discussions/execution-intents?trade_date={trade_date}&account_id=test-account"
                    )
                    self.assertEqual(intents_response.status_code, 200)
                    intents_payload = intents_response.json()
                    self.assertTrue(intents_payload["ok"])

                    cadence_after_intents = client.get("/system/monitoring/cadence").json()["polling_status"]
                    self.assertFalse(cadence_after_intents["execution"]["due_now"])
                    self.assertEqual(cadence_after_intents["execution"]["last_trigger"], "execution_intents_read")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_cadence_display_suppresses_due_when_discussion_and_execution_are_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = "2026-04-13"
                discussion_cycle_service = get_discussion_cycle_service()
                discussion_cycle_service._upsert(  # noqa: SLF001 - 测试直接构造稳定 cycle 场景
                    DiscussionCycle(
                        cycle_id="cycle-20260413",
                        trade_date=trade_date,
                        pool_state="focus_pool_building",
                        discussion_state="round_1_running",
                        base_pool_case_ids=["case-1", "case-2"],
                        focus_pool_case_ids=["case-1", "case-2"],
                        execution_pool_case_ids=["case-1"],
                        blockers=[],
                        summary_snapshot={},
                        started_at=datetime.now().isoformat(),
                        round_1_started_at=datetime.now().isoformat(),
                        updated_at=datetime.now().isoformat(),
                        current_round=1,
                    )
                )

                stale_at = (datetime.now() - timedelta(minutes=5)).isoformat()
                monitor_state_store = get_monitor_state_store()
                monitor_state_store.set(
                    "latest_pool_snapshot",
                    {
                        "snapshot_id": "pool-snapshot-test",
                        "generated_at": datetime.now().isoformat(),
                        "trade_date": trade_date,
                        "candidate_pool": [],
                        "counts": {"candidate_pool": 0},
                    },
                )
                monitor_state_store.set(
                    "polling_state",
                    {
                        "focus": {
                            "layer": "focus",
                            "trigger": "discussion_round_1_start",
                            "last_polled_at": stale_at,
                            "next_due_at": (datetime.fromisoformat(stale_at) + timedelta(seconds=60)).isoformat(),
                            "interval_seconds": 60,
                        },
                        "execution": {
                            "layer": "execution",
                            "trigger": "execution_intents_read",
                            "last_polled_at": stale_at,
                            "next_due_at": (datetime.fromisoformat(stale_at) + timedelta(seconds=30)).isoformat(),
                            "interval_seconds": 30,
                        },
                    },
                )

                with TestClient(create_app()) as client:
                    cadence_payload = client.get(f"/system/monitoring/cadence?trade_date={trade_date}").json()
                    focus = cadence_payload["polling_status"]["focus"]
                    execution = cadence_payload["polling_status"]["execution"]
                    self.assertTrue(focus["raw_due_now"])
                    self.assertFalse(focus["due_now"])
                    self.assertEqual(focus["display_state"], "active")
                    self.assertEqual(focus["suppressed_due_reason"], "discussion_active:round_1_running")
                    self.assertTrue(execution["raw_due_now"])
                    self.assertFalse(execution["due_now"])
                    self.assertEqual(execution["display_state"], "active")
                    self.assertEqual(execution["suppressed_due_reason"], "execution_intents_ready")

                    monitor_state_payload = client.get("/monitor/state").json()
                    self.assertFalse(monitor_state_payload["polling_status"]["focus"]["due_now"])
                    self.assertFalse(monitor_state_payload["polling_status"]["execution"]["due_now"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_execution_bridge_health_ingress_defaults_component_reachable_from_top_level_flags(self) -> None:
        payload = build_execution_bridge_health_ingress_payload(
            {
                "gateway_online": True,
                "qmt_connected": True,
                "windows_execution_gateway": {"status": "healthy"},
                "qmt_vm": {"status": "healthy"},
            }
        )["health"]

        self.assertTrue(payload["windows_execution_gateway"]["reachable"])
        self.assertTrue(payload["qmt_vm"]["reachable"])
        self.assertTrue(payload["component_health"][0]["reachable"])
        self.assertTrue(payload["component_health"][1]["reachable"])

    def test_controlled_apply_readiness_ready_for_single_whitelisted_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                    "ASHARE_LIVE_ENABLE",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_LIVE_ENABLE"] = "false"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []
                longconn_store = get_feishu_longconn_state_store()
                longconn_store.set("status", "connected")
                longconn_store.set("pid", os.getpid())
                longconn_store.set("last_connected_at", datetime.now().isoformat())
                longconn_store.set("last_event_at", datetime.now().isoformat())
                longconn_store.set("last_heartbeat_at", datetime.now().isoformat())
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": True,
                            "qmt_connected": True,
                            "overall_status": "healthy",
                            "windows_execution_gateway": {"status": "healthy", "reachable": True},
                            "qmt_vm": {"status": "healthy", "reachable": True},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )

                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)

                    payload = client.get(
                        f"/system/deployment/controlled-apply-readiness?trade_date={trade_date}"
                        "&account_id=test-account"
                        "&require_live=false"
                        "&require_trading_session=false"
                        "&allowed_symbols=600519.SH"
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "ready")
                    self.assertEqual((payload.get("first_intent") or {}).get("symbol"), "600519.SH")
                    check_map = {item["name"]: item for item in payload["checks"]}
                    self.assertEqual(check_map["execution_bridge"]["status"], "ok")
                    self.assertEqual(check_map["apply_symbol_whitelist"]["status"], "ok")
                    self.assertEqual(check_map["apply_intent_limit"]["status"], "ok")

                    summary_payload = client.get(
                        f"/system/deployment/controlled-apply-readiness?trade_date={trade_date}"
                        "&account_id=test-account"
                        "&require_live=false"
                        "&require_trading_session=false"
                        "&allowed_symbols=600519.SH"
                        "&include_details=false"
                    ).json()
                    self.assertTrue(summary_payload["ok"])
                    self.assertEqual(summary_payload["detail_mode"], "summary")
                    self.assertNotIn("execution_precheck", summary_payload)
                    self.assertNotIn("execution_intents", summary_payload)

                    scoped_payload = client.get(
                        f"/system/deployment/controlled-apply-readiness?trade_date={trade_date}"
                        "&account_id=test-account"
                        "&require_live=false"
                        "&require_trading_session=false"
                        "&max_apply_intents=1"
                        "&allowed_symbols=600519.SH"
                        "&intent_ids=intent-"
                        f"{trade_date}-600519.SH"
                        "&include_details=false"
                    ).json()
                    self.assertTrue(scoped_payload["ok"])
                    self.assertEqual(scoped_payload["status"], "ready")
                    scoped_checks = {item["name"]: item for item in scoped_payload["checks"]}
                    self.assertEqual(scoped_checks["selected_intent_scope"]["status"], "ok")
                    self.assertEqual(scoped_checks["apply_intent_limit"]["status"], "ok")
                    self.assertEqual((scoped_payload.get("first_intent") or {}).get("symbol"), "600519.SH")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_controlled_apply_readiness_blocks_when_live_required_but_run_mode_is_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                    "ASHARE_LIVE_ENABLE",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_LIVE_ENABLE"] = "false"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []
                longconn_store = get_feishu_longconn_state_store()
                longconn_store.set("status", "connected")
                longconn_store.set("pid", os.getpid())
                longconn_store.set("last_connected_at", datetime.now().isoformat())
                longconn_store.set("last_event_at", datetime.now().isoformat())
                longconn_store.set("last_heartbeat_at", datetime.now().isoformat())
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": True,
                            "qmt_connected": True,
                            "overall_status": "healthy",
                            "windows_execution_gateway": {"status": "healthy", "reachable": True},
                            "qmt_vm": {"status": "healthy", "reachable": True},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )

                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)

                    payload = client.get(
                        f"/system/deployment/controlled-apply-readiness?trade_date={trade_date}"
                        "&account_id=test-account"
                        "&require_trading_session=false"
                        "&allowed_symbols=600519.SH"
                    ).json()
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    blocked_checks = {item["name"] for item in payload["checks"] if item["status"] == "blocked"}
                    self.assertIn("run_mode_live", blocked_checks)
                    self.assertIn("live_trade_enabled", blocked_checks)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_controlled_apply_readiness_blocks_inside_apply_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                    "ASHARE_LIVE_ENABLE",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_LIVE_ENABLE"] = "false"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []
                now = datetime.now()
                longconn_store = get_feishu_longconn_state_store()
                longconn_store.set("status", "connected")
                longconn_store.set("pid", os.getpid())
                longconn_store.set("last_connected_at", now.isoformat())
                longconn_store.set("last_event_at", now.isoformat())
                longconn_store.set("last_heartbeat_at", now.isoformat())
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": True,
                            "qmt_connected": True,
                            "overall_status": "healthy",
                            "windows_execution_gateway": {"status": "healthy", "reachable": True},
                            "qmt_vm": {"status": "healthy", "reachable": True},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )

                blocked_window = f"{now.strftime('%H:%M')}-{(now + timedelta(minutes=1)).strftime('%H:%M')}"

                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)

                    payload = client.get(
                        f"/system/deployment/controlled-apply-readiness?trade_date={trade_date}"
                        "&account_id=test-account"
                        "&require_live=false"
                        "&require_trading_session=false"
                        "&allowed_symbols=600519.SH"
                        f"&blocked_time_windows={blocked_window}"
                    ).json()
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    check_map = {item["name"]: item for item in payload["checks"]}
                    self.assertEqual(check_map["apply_time_window"]["status"], "blocked")
                    self.assertIn(blocked_window, check_map["apply_time_window"]["detail"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_controlled_apply_readiness_blocks_when_daily_apply_submission_limit_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                    "ASHARE_LIVE_ENABLE",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_LIVE_ENABLE"] = "false"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []
                now = datetime.now()
                longconn_store = get_feishu_longconn_state_store()
                longconn_store.set("status", "connected")
                longconn_store.set("pid", os.getpid())
                longconn_store.set("last_connected_at", now.isoformat())
                longconn_store.set("last_event_at", now.isoformat())
                longconn_store.set("last_heartbeat_at", now.isoformat())
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": True,
                            "qmt_connected": True,
                            "overall_status": "healthy",
                            "windows_execution_gateway": {"status": "healthy", "reachable": True},
                            "qmt_vm": {"status": "healthy", "reachable": True},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )
                meeting_state_store = get_meeting_state_store()
                meeting_state_store.set(
                    "execution_dispatch_history",
                    [
                        {
                            "trade_date": now.date().isoformat(),
                            "account_id": "test-account",
                            "status": "queued_for_gateway",
                            "queued_count": 1,
                            "submitted_count": 0,
                            "preview_count": 0,
                            "blocked_count": 0,
                            "generated_at": now.isoformat(),
                        }
                    ],
                )

                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)

                    payload = client.get(
                        f"/system/deployment/controlled-apply-readiness?trade_date={trade_date}"
                        "&account_id=test-account"
                        "&require_live=false"
                        "&require_trading_session=false"
                        "&allowed_symbols=600519.SH"
                        "&max_apply_submissions_per_day=1"
                    ).json()
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    check_map = {item["name"]: item for item in payload["checks"]}
                    self.assertEqual(check_map["daily_apply_submission_limit"]["status"], "blocked")
                    self.assertIn("current=1", check_map["daily_apply_submission_limit"]["detail"])
                    self.assertEqual(payload["apply_submission_snapshot"]["queued_count"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_service_recovery_readiness_ready_when_workspace_and_bridges_are_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                now = datetime.now().isoformat()
                trade_date = now[:10]
                layout = ensure_storage_layout(Path(tmp_dir))
                (layout.serving_root / "latest_workspace_context.json").write_text(
                    json.dumps(
                        {
                            "available": True,
                            "resource": "workspace_context",
                            "trade_date": trade_date,
                            "generated_at": now,
                            "status": "ready",
                            "summary_lines": ["workspace ready"],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                longconn_store = get_feishu_longconn_state_store()
                longconn_store.set("status", "connected")
                longconn_store.set("pid", os.getpid())
                longconn_store.set("last_connected_at", now)
                longconn_store.set("last_event_at", now)
                longconn_store.set("last_heartbeat_at", now)
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": True,
                            "qmt_connected": True,
                            "overall_status": "healthy",
                            "windows_execution_gateway": {"status": "healthy", "reachable": True},
                            "qmt_vm": {"status": "healthy", "reachable": True},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )

                with TestClient(create_app()) as client:
                    payload = client.get(
                        f"/system/deployment/service-recovery-readiness?trade_date={trade_date}&account_id=test-account"
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "ready")
                    check_map = {item["name"]: item for item in payload["checks"]}
                    self.assertEqual(check_map["feishu_longconn"]["status"], "ok")
                    self.assertEqual(check_map["execution_bridge"]["status"], "ok")
                    self.assertEqual(check_map["workspace_context"]["status"], "ok")
                    self.assertEqual(check_map["recovery_signal_freshness"]["status"], "ok")

                    summary_payload = client.get(
                        f"/system/deployment/service-recovery-readiness?trade_date={trade_date}&account_id=test-account&include_details=false"
                    ).json()
                    self.assertTrue(summary_payload["ok"])
                    self.assertEqual(summary_payload["detail_mode"], "summary")
                    self.assertNotIn("workspace_context", summary_payload)
                    self.assertNotIn("briefing", summary_payload)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_service_recovery_readiness_blocks_when_workspace_context_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                stale_at = (datetime.now() - timedelta(hours=2)).isoformat()
                trade_date = stale_at[:10]
                layout = ensure_storage_layout(Path(tmp_dir))
                (layout.serving_root / "latest_workspace_context.json").write_text(
                    json.dumps(
                        {
                            "available": True,
                            "resource": "workspace_context",
                            "trade_date": trade_date,
                            "generated_at": stale_at,
                            "status": "ready",
                            "summary_lines": ["workspace stale"],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                longconn_store = get_feishu_longconn_state_store()
                longconn_store.set("status", "connected")
                longconn_store.set("pid", os.getpid())
                longconn_store.set("last_connected_at", datetime.now().isoformat())
                longconn_store.set("last_event_at", datetime.now().isoformat())
                longconn_store.set("last_heartbeat_at", datetime.now().isoformat())
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": True,
                            "qmt_connected": True,
                            "overall_status": "healthy",
                            "windows_execution_gateway": {"status": "healthy", "reachable": True},
                            "qmt_vm": {"status": "healthy", "reachable": True},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )

                with TestClient(create_app()) as client:
                    payload = client.get(
                        f"/system/deployment/service-recovery-readiness?trade_date={trade_date}&account_id=test-account&max_workspace_age_seconds=60&max_signal_age_seconds=60"
                    ).json()
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    blocked_checks = {item["name"] for item in payload["checks"] if item["status"] == "blocked"}
                    self.assertIn("workspace_context", blocked_checks)
                    self.assertIn("recovery_signal_freshness", blocked_checks)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_state_store_remains_readable_under_concurrent_read_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "state.json"
            writer_store = StateStore(path)
            reader_store = StateStore(path)
            exceptions: list[Exception] = []

            def writer() -> None:
                try:
                    for index in range(80):
                        writer_store.set("value", index)
                except Exception as exc:  # pragma: no cover - 用于捕获并发异常
                    exceptions.append(exc)

            def reader() -> None:
                try:
                    for _ in range(80):
                        reader_store.get("value")
                except Exception as exc:  # pragma: no cover - 用于捕获并发异常
                    exceptions.append(exc)

            writer_thread = threading.Thread(target=writer)
            reader_thread = threading.Thread(target=reader)
            writer_thread.start()
            reader_thread.start()
            writer_thread.join()
            reader_thread.join()

            self.assertEqual(exceptions, [])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("value", payload)

    def test_windows_proxy_execution_adapter_maps_balance_payload(self) -> None:
        settings = AppSettings(
            execution_mode="windows_proxy",
            windows_gateway=WindowsGatewaySettings(
                base_url="http://127.0.0.1:18791",
                token="token-for-test",
                timeout_sec=3.0,
            ),
        )
        adapter = WindowsProxyExecutionAdapter(settings)

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Ashare-Token"], "token-for-test")
            self.assertEqual(str(request.url), "http://127.0.0.1:18791/qmt/account/asset")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "account_id": "8890130545",
                    "asset": {
                        "account_id": "8890130545",
                        "cash": 101028.55,
                        "frozen_cash": 0.0,
                        "total_asset": 101028.55,
                    },
                },
            )

        adapter._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=3.0)  # type: ignore[assignment]
        balance = adapter.get_balance("8890130545")
        self.assertEqual(balance.account_id, "8890130545")
        self.assertEqual(balance.cash, 101028.55)
        self.assertEqual(balance.total_asset, 101028.55)

    def test_windows_proxy_market_adapter_caches_symbol_name_from_instruments(self) -> None:
        settings = AppSettings(
            market_mode="windows_proxy",
            windows_gateway=WindowsGatewaySettings(
                base_url="http://127.0.0.1:18791",
                token="token-for-test",
                timeout_sec=3.0,
            ),
        )
        adapter = WindowsProxyMarketDataAdapter(settings)
        call_count = {"instruments": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Ashare-Token"], "token-for-test")
            if request.url.path == "/qmt/quote/instruments":
                call_count["instruments"] += 1
                self.assertEqual(request.url.params.get("codes"), "600000.SH")
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "instruments": [
                            {
                                "symbol": "600000.SH",
                                "name": "浦发银行",
                                "market": "SH",
                                "security_type": "stock",
                                "board": "main",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected path: {request.url.path}")

        adapter._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=3.0)  # type: ignore[assignment]
        self.assertEqual(adapter.get_symbol_name("600000.SH"), "浦发银行")
        self.assertEqual(adapter.get_symbol_name("600000.SH"), "浦发银行")
        self.assertEqual(call_count["instruments"], 1)

    def test_windows_proxy_market_adapter_search_symbols_by_name(self) -> None:
        settings = AppSettings(
            market_mode="windows_proxy",
            windows_gateway=WindowsGatewaySettings(
                base_url="http://127.0.0.1:18791",
                token="token-for-test",
                timeout_sec=3.0,
            ),
        )
        adapter = WindowsProxyMarketDataAdapter(settings)
        call_count = {"universe": 0, "instruments": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Ashare-Token"], "token-for-test")
            if request.url.path == "/qmt/quote/universe":
                call_count["universe"] += 1
                return httpx.Response(
                    200,
                    json={"ok": True, "symbols": ["002882.SZ", "002202.SZ", "600000.SH"]},
                )
            if request.url.path == "/qmt/quote/instruments":
                call_count["instruments"] += 1
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "instruments": [
                            {"symbol": "002882.SZ", "name": "金龙羽"},
                            {"symbol": "002202.SZ", "name": "金风科技"},
                            {"symbol": "600000.SH", "name": "浦发银行"},
                        ],
                    },
                )
            raise AssertionError(f"unexpected path: {request.url.path}")

        adapter._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=3.0)  # type: ignore[assignment]
        results = adapter.search_symbols("金龙羽")
        self.assertEqual(results[0]["symbol"], "002882.SZ")
        self.assertEqual(results[0]["name"], "金龙羽")
        self.assertEqual(call_count["universe"], 1)
        self.assertEqual(call_count["instruments"], 1)

        cached_results = adapter.search_symbols("金风")
        self.assertEqual(cached_results[0]["symbol"], "002202.SZ")
        self.assertEqual(call_count["universe"], 1)
        self.assertEqual(call_count["instruments"], 1)

    def test_windows_proxy_market_adapter_maps_tick_payload_to_quote_snapshot(self) -> None:
        settings = AppSettings(
            market_mode="windows_proxy",
            windows_gateway=WindowsGatewaySettings(
                base_url="http://127.0.0.1:18791",
                token="token-for-test",
                timeout_sec=3.0,
            ),
        )
        adapter = WindowsProxyMarketDataAdapter(settings)

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Ashare-Token"], "token-for-test")
            if request.url.path == "/qmt/quote/instruments":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "instruments": [{"symbol": "600000.SH", "name": "浦发银行"}],
                    },
                )
            if request.url.path == "/qmt/quote/tick":
                self.assertEqual(request.url.params.get("codes"), "600000.SH")
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "data": {
                            "600000.SH": {
                                "symbol": "600000.SH",
                                "lastPrice": 10.5,
                                "lastClose": 10.0,
                                "bidPrice": [10.49],
                                "askPrice": [10.51],
                                "volume": 123456,
                            }
                        },
                    },
                )
            raise AssertionError(f"unexpected path: {request.url.path}")

        adapter._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=3.0)  # type: ignore[assignment]
        snapshots = adapter.get_snapshots(["600000.SH"])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].symbol, "600000.SH")
        self.assertEqual(snapshots[0].name, "浦发银行")
        self.assertEqual(snapshots[0].last_price, 10.5)
        self.assertEqual(snapshots[0].bid_price, 10.49)
        self.assertEqual(snapshots[0].ask_price, 10.51)
        self.assertEqual(snapshots[0].pre_close, 10.0)
        self.assertEqual(snapshots[0].volume, 123456.0)

    def test_windows_proxy_market_adapter_maps_kline_payload_to_bar_snapshots(self) -> None:
        settings = AppSettings(
            market_mode="windows_proxy",
            windows_gateway=WindowsGatewaySettings(
                base_url="http://127.0.0.1:18791",
                token="token-for-test",
                timeout_sec=3.0,
            ),
        )
        adapter = WindowsProxyMarketDataAdapter(settings)

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Ashare-Token"], "token-for-test")
            self.assertEqual(request.url.path, "/qmt/quote/kline")
            self.assertEqual(request.url.params.get("codes"), "600000.SH")
            self.assertEqual(request.url.params.get("period"), "15m")
            self.assertEqual(request.url.params.get("count"), "2")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "bars": {
                            "600000.SH": [
                                {
                                    "time": "2026-04-11 14:45:00",
                                    "trade_time": "2026-04-11 14:45:00",
                                    "open": 10.1,
                                    "high": 10.4,
                                    "low": 10.0,
                                    "close": 10.3,
                                    "volume": 1000,
                                    "amount": 10300,
                                    "preClose": 10.0,
                                },
                                {
                                    "time": "2026-04-11 15:00:00",
                                    "trade_time": "2026-04-11 15:00:00",
                                    "open": 10.3,
                                    "high": 10.6,
                                    "low": 10.2,
                                    "close": 10.5,
                                    "volume": 1200,
                                    "amount": 12600,
                                    "preClose": 10.3,
                                },
                            ]
                        }
                    },
                },
            )

        adapter._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=3.0)  # type: ignore[assignment]
        bars = adapter.get_bars(["600000.SH"], "15m", count=2)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].symbol, "600000.SH")
        self.assertEqual(bars[0].period, "15m")
        self.assertEqual(bars[0].open, 10.1)
        self.assertEqual(bars[0].close, 10.3)
        self.assertEqual(bars[0].pre_close, 10.0)
        self.assertEqual(bars[1].trade_time, "2026-04-11 15:00:00")
        self.assertEqual(bars[1].amount, 12600.0)

    def test_windows_proxy_market_adapter_maps_universe_and_sector_payloads(self) -> None:
        settings = AppSettings(
            market_mode="windows_proxy",
            windows_gateway=WindowsGatewaySettings(
                base_url="http://127.0.0.1:18791",
                token="token-for-test",
                timeout_sec=3.0,
            ),
        )
        adapter = WindowsProxyMarketDataAdapter(settings)

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Ashare-Token"], "token-for-test")
            if request.url.path == "/qmt/quote/universe":
                if request.url.params.get("scope") == "main_board":
                    return httpx.Response(
                        200,
                        json={"ok": True, "scope": "main_board", "symbols": ["600000.SH", "000001.SZ", "300750.SZ"]},
                    )
                self.assertEqual(request.url.params.get("a_share_only"), "true")
                return httpx.Response(
                    200,
                    json={"ok": True, "symbols": ["600000.SH", "000001.SZ", "300750.SZ", "511880.SH"]},
                )
            if request.url.path == "/qmt/quote/sectors":
                return httpx.Response(200, json={"ok": True, "sectors": ["银行", "人工智能", "银行"]})
            if request.url.path == "/qmt/quote/sector-members":
                self.assertEqual(request.url.params.get("sector"), "银行")
                return httpx.Response(
                    200,
                    json={"ok": True, "sector": "银行", "symbols": ["600000.SH", "000001.SZ", "511880.SH"]},
                )
            raise AssertionError(f"unexpected path: {request.url.path}")

        adapter._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=3.0)  # type: ignore[assignment]
        self.assertEqual(adapter.get_main_board_universe(), ["600000.SH", "000001.SZ"])
        self.assertEqual(adapter.get_a_share_universe(), ["600000.SH", "000001.SZ", "300750.SZ"])
        self.assertEqual(adapter.get_sectors(), ["银行", "人工智能"])
        self.assertEqual(adapter.get_sector_symbols("银行"), ["600000.SH", "000001.SZ"])

    def test_scheduler_research_and_learning_tasks_run_daily(self) -> None:
        from ashare_system.scheduler import ALL_TASKS

        cron_map = {task.handler: task.cron for task in ALL_TASKS}
        self.assertEqual(cron_map["learning.score_state:daily_settlement"], "30 16 * * *")
        self.assertEqual(cron_map["learning.prompt_patcher:daily_patch"], "45 16 * * *")
        self.assertEqual(cron_map["monitor.dragon_tiger:analyze"], "0 17 * * *")
        self.assertEqual(cron_map["learning.registry_updater:update_weights"], "0 17 * * *")
        self.assertEqual(cron_map["learning.self_evolve:suggest"], "15 17 * * *")
        self.assertEqual(cron_map["strategy.stock_profile:refresh"], "30 17 * * *")
        self.assertEqual(cron_map["learning.continuous:validate"], "30 17 * * *")
        self.assertEqual(cron_map["governance.parameter_hints:inspection"], "0 18 * * *")
        self.assertEqual(cron_map["supervision.agent:check"], "*/3 9-15 * * 1-5")
        self.assertEqual(cron_map["data.fetcher:fetch_news"], "0 20 * * *")
        self.assertEqual(cron_map["strategy.nightly_sandbox:run"], "0 23 * * *")
        self.assertEqual(cron_map["strategy.screener:run_pipeline"], "0 21 * * 1-5")
        self.assertEqual(cron_map["strategy.buy_decision:pre_confirm"], "0 22 * * 1-5")

    def test_feishu_rights_briefing_ask_and_adjustment_alias_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    rights_payload = client.get("/system/feishu/rights").json()
                    self.assertTrue(rights_payload["ok"])
                    self.assertIn("know", rights_payload["rights"])
                    self.assertIn("adjust", rights_payload["rights"])
                    self.assertIn("ask", rights_payload["rights"])

                    briefing_payload = client.get("/system/feishu/briefing").json()
                    self.assertTrue(briefing_payload["ok"])
                    self.assertIn("summary_lines", briefing_payload)
                    self.assertIn("data_refs", briefing_payload)

                    rights_briefing_payload = client.get("/system/feishu/rights-briefing").json()
                    self.assertTrue(rights_briefing_payload["ok"])
                    self.assertEqual(
                        rights_briefing_payload["summary_lines"],
                        briefing_payload["summary_lines"],
                    )

                    capability_payload = client.get("/system/agents/capability-map").json()
                    self.assertTrue(capability_payload["ok"])
                    self.assertIn("ashare", capability_payload["roles"])
                    self.assertIn("/system/workspace-context", capability_payload["global_entrypoints"].values())
                    self.assertIn("when_to_call", capability_payload["how_to_use"])
                    self.assertTrue(capability_payload["how_to_use"]["example_calls"])
                    self.assertIn("prompt_template", capability_payload["roles"]["ashare-runtime"])
                    self.assertEqual(capability_payload["competition_mechanism"]["name"], "积分赛马")
                    self.assertTrue(any(item["state"] == "fired" for item in capability_payload["competition_mechanism"]["score_actions"]))

                    robot_layout_payload = client.get("/system/robot/console-layout").json()
                    self.assertTrue(robot_layout_payload["ok"])
                    self.assertTrue(any(item["id"] == "status_overview" for item in robot_layout_payload["sections"]))

                    workflow_payload = client.get("/system/workflow/mainline").json()
                    self.assertTrue(workflow_payload["ok"])
                    self.assertTrue(any(item["stage"] == "execution" for item in workflow_payload["stages"]))
                    self.assertIn("when_to_call", workflow_payload["how_to_use"])
                    self.assertTrue(workflow_payload["autonomy_loops"])
                    self.assertTrue(workflow_payload["governance_pressure"])

                    autonomy_payload = client.get("/system/agents/autonomy-spec").json()
                    self.assertTrue(autonomy_payload["ok"])
                    self.assertIn("when_to_call", autonomy_payload["how_to_use"])
                    self.assertIn("/runtime/capabilities", autonomy_payload["data_refs"])

                    ask_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "现在系统状态怎么样"},
                    ).json()
                    self.assertTrue(ask_payload["ok"])
                    self.assertEqual(ask_payload["topic"], "status")
                    self.assertTrue(ask_payload["answer_lines"])

                    adjust_payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={
                            "instruction": "股票池改到30只",
                            "apply": False,
                            "notify": False,
                        },
                    ).json()
                    self.assertTrue(adjust_payload["ok"])
                    self.assertEqual(adjust_payload["status"], "preview")
                    self.assertGreaterEqual(adjust_payload["matched_count"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_flags_discussion_agents_when_round_started_without_opinions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180"
                    ).json()
                    self.assertTrue(board_payload["ok"])
                    self.assertTrue(board_payload["attention_items"])
                    attention_agent_ids = {item["agent_id"] for item in board_payload["attention_items"]}
                    self.assertTrue(
                        {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}.issubset(attention_agent_ids)
                    )

                    check_payload = client.post(
                        "/system/agents/supervision/check",
                        json={
                            "trade_date": trade_date,
                            "overdue_after_seconds": 180,
                            "notify": False,
                        },
                    ).json()
                    self.assertTrue(check_payload["ok"])
                    self.assertTrue(check_payload["notify_recommended"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_does_not_chase_runtime_frequency_when_activity_is_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ"],
                            "max_candidates": 2,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=30"
                    ).json()
                    self.assertTrue(board_payload["ok"])
                    runtime_item = next(item for item in board_payload["items"] if item["agent_id"] == "ashare-runtime")
                    self.assertEqual(runtime_item["status"], "working")
                    self.assertEqual(runtime_item["activity_label"], "运行事实产出")
                    self.assertGreaterEqual(runtime_item["activity_signal_count"], 1)
                    self.assertTrue(any(signal["source"] == "runtime_report" for signal in runtime_item["activity_signals"]))
                    self.assertNotIn("ashare-runtime", {item["agent_id"] for item in board_payload["attention_items"]})
                    self.assertTrue(any("活动痕迹" in line for line in board_payload["summary_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_recognizes_recent_agent_activity_outside_discussion_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                now = datetime.now().isoformat()
                research_state_store = get_research_state_store()
                meeting_state_store = get_meeting_state_store()
                research_state_store.set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": now,
                        "symbols": ["600519.SH"],
                        "news_count": 2,
                        "announcement_count": 1,
                        "event_titles": ["盘中催化验证"],
                    },
                )
                meeting_state_store.set(
                    "latest_execution_precheck",
                    {
                        "trade_date": trade_date,
                        "generated_at": now,
                        "status": "ready",
                    },
                )
                meeting_state_store.set(
                    "latest",
                    {
                        "trade_date": trade_date,
                        "recorded_at": now,
                        "title": "盘中审计复核",
                    },
                )

                with TestClient(create_app()) as client:
                    proposal_payload = client.post(
                        "/system/params/proposals",
                        json={
                            "param_key": "focus_pool_capacity",
                            "new_value": 12,
                            "proposed_by": "ashare-strategy",
                            "structured_by": "ashare-strategy",
                            "reason": "盘中热度变化，扩大策略观察池",
                        },
                    ).json()
                    self.assertTrue(proposal_payload["ok"])

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=30"
                    ).json()
                    self.assertTrue(board_payload["ok"])

                    item_map = {item["agent_id"]: item for item in board_payload["items"]}
                    self.assertEqual(item_map["ashare-research"]["status"], "working")
                    self.assertEqual(item_map["ashare-strategy"]["status"], "working")
                    self.assertEqual(item_map["ashare-risk"]["status"], "working")
                    self.assertEqual(item_map["ashare-audit"]["status"], "working")
                    self.assertEqual(item_map["ashare-strategy"]["activity_label"], "策略/调参提案")
                    self.assertGreaterEqual(item_map["ashare-strategy"]["activity_signal_count"], 1)
                    self.assertTrue(
                        any(signal["source"] == "param_proposals" for signal in item_map["ashare-strategy"]["activity_signals"])
                    )
                    self.assertEqual(item_map["ashare-research"]["activity_label"], "研究/事件产出")
                    self.assertGreaterEqual(item_map["ashare-research"]["activity_signal_count"], 1)
                    attention_agent_ids = {item["agent_id"] for item in board_payload["attention_items"]}
                    self.assertTrue(
                        {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}.isdisjoint(attention_agent_ids)
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_ack_suppresses_notify_recommendation_for_same_attention_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)

                    before_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180"
                    ).json()
                    self.assertTrue(before_payload["notify_recommended"])
                    self.assertTrue(before_payload["notify_items"])

                    ack_payload = client.post(
                        "/system/agents/supervision/ack",
                        json={
                            "trade_date": trade_date,
                            "actor": "feishu-bot",
                            "note": "已收到催办，转入处理",
                        },
                    ).json()
                    self.assertTrue(ack_payload["ok"])
                    self.assertGreaterEqual(ack_payload["acked_count"], 4)
                    self.assertFalse(ack_payload["supervision"]["notify_recommended"])
                    self.assertEqual(len(ack_payload["supervision"]["notify_items"]), 0)

                    after_payload = client.post(
                        "/system/agents/supervision/check",
                        json={
                            "trade_date": trade_date,
                            "overdue_after_seconds": 180,
                            "notify": False,
                        },
                    ).json()
                    self.assertTrue(after_payload["ok"])
                    self.assertFalse(after_payload["notify_recommended"])
                    self.assertTrue(after_payload["attention_items"])
                    self.assertTrue(all(item["acknowledged"] for item in after_payload["attention_items"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_avoids_live_balance_and_quote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    trade_date = str(runtime_response.json()["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)

                    execution_adapter = get_execution_adapter()
                    market_adapter = get_market_adapter()

                    def _fail(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试用显式兜底
                        raise AssertionError("supervision route should not trigger live execution/quote calls")

                    execution_adapter.get_balance = _fail  # type: ignore[method-assign]
                    execution_adapter.get_positions = _fail  # type: ignore[method-assign]
                    market_adapter.get_snapshots = _fail  # type: ignore[method-assign]

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180"
                    ).json()
                    self.assertTrue(board_payload["ok"])
                    self.assertTrue(board_payload["attention_items"])

                    check_payload = client.post(
                        "/system/agents/supervision/check",
                        json={
                            "trade_date": trade_date,
                            "overdue_after_seconds": 180,
                            "notify": False,
                        },
                    ).json()
                    self.assertTrue(check_payload["ok"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_marks_activity_without_round_progress_as_low_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    trade_date = str(runtime_response.json()["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    proposal_payload = client.post(
                        "/system/params/proposals",
                        json={
                            "param_key": "focus_pool_capacity",
                            "new_value": 12,
                            "proposed_by": "ashare-strategy",
                            "structured_by": "ashare-strategy",
                            "reason": "盘中热度变化，先调观察池，但本轮观点还没写回",
                        },
                    ).json()
                    self.assertTrue(proposal_payload["ok"])

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180"
                    ).json()
                    self.assertTrue(board_payload["ok"])
                    strategy_item = next(item for item in board_payload["items"] if item["agent_id"] == "ashare-strategy")
                    self.assertEqual(strategy_item["status"], "needs_work")
                    self.assertGreaterEqual(strategy_item["activity_signal_count"], 1)
                    self.assertEqual(strategy_item["quality_state"], "low")
                    self.assertIn("有活动痕迹", strategy_item["quality_reason"])
                    self.assertTrue(any("有活动但未推进主线" in line for line in board_payload["summary_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_check_preserves_prioritized_notify_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    trade_date = str(runtime_response.json()["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    proposal_payload = client.post(
                        "/system/params/proposals",
                        json={
                            "param_key": "focus_pool_capacity",
                            "new_value": 12,
                            "proposed_by": "ashare-strategy",
                            "structured_by": "ashare-strategy",
                            "reason": "盘中热度变化，先调观察池，但本轮观点还没写回",
                        },
                    ).json()
                    self.assertTrue(proposal_payload["ok"])

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=30"
                    ).json()
                    self.assertTrue(board_payload["ok"])
                    board_notify_ids = [item["agent_id"] for item in board_payload["notify_items"]]
                    self.assertTrue(board_notify_ids)

                    check_payload = client.post(
                        "/system/agents/supervision/check",
                        json={
                            "trade_date": trade_date,
                            "overdue_after_seconds": 30,
                            "notify": False,
                        },
                    ).json()
                    self.assertTrue(check_payload["ok"])
                    notify_ids = [item["agent_id"] for item in check_payload["notify_items"]]
                    self.assertTrue(notify_ids)
                    self.assertEqual(notify_ids[:4], board_notify_ids[:4])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_task_plan_generates_phase_based_natural_language_tasks(self) -> None:
        now = datetime.now().replace(hour=14, minute=36, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "reasons": ["Round 1 仅覆盖 0/5"],
                    "last_active_at": None,
                },
                {
                    "agent_id": "ashare-strategy",
                    "status": "working",
                    "reasons": ["最近策略/调参提案=2026-04-17T14:20:00"],
                    "last_active_at": f"{trade_date}T14:20:00",
                },
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "reasons": ["Round 1 仅覆盖 0/5"],
                    "last_active_at": None,
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "reasons": ["Round 1 仅覆盖 0/5"],
                    "last_active_at": None,
                }
            ],
        }

        plan = build_agent_task_plan(
            payload,
            execution_summary={"intent_count": 2, "dispatch_status": "preview"},
            latest_market_change_at=f"{trade_date}T14:28:00",
            now=now,
        )
        self.assertEqual(plan["phase"]["code"], "tail_session")
        self.assertGreaterEqual(len(plan["recommended_tasks"]), 2)
        research_task = next(item for item in plan["recommended_tasks"] if item["agent_id"] == "ashare-research")
        strategy_task = next(item for item in plan["recommended_tasks"] if item["agent_id"] == "ashare-strategy")
        self.assertIn("尾盘", research_task["task_prompt"])
        self.assertIn("自己组织参数", strategy_task["task_prompt"])
        self.assertIn("runtime", strategy_task["task_prompt"])

    def test_agent_task_plan_includes_quality_gap_in_reason_prompt_and_summary(self) -> None:
        now = datetime.now().replace(hour=10, minute=18, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "quality_summary_lines": ["推进质量: good=0 partial=0 low=1 blocked=0 observe=0。"],
            "progress_blockers": ["ashare-strategy 本轮仍缺 2 份主线材料，已有活动但未写回观点。"],
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "reasons": ["Round 1 仅覆盖 0/2", "最近策略/调参提案=2026-04-17T10:12:00，但尚未写回本轮主线"],
                    "last_active_at": f"{trade_date}T10:12:00",
                    "activity_label": "策略/调参提案",
                    "activity_signal_count": 1,
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "有活动痕迹，但还没推进到本轮主线产物。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "有活动痕迹，但还没推进到本轮主线产物。",
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "有活动痕迹，但还没推进到本轮主线产物。",
                }
            ],
        }
        plan = build_agent_task_plan(payload, now=now)
        strategy_task = next(item for item in plan["tasks"] if item["agent_id"] == "ashare-strategy")
        self.assertIn("推进质量=low", strategy_task["task_reason"])
        self.assertIn("本轮主线当前只覆盖了 0/2", strategy_task["task_reason"])
        self.assertIn("必须把可消费结论写回主线", strategy_task["task_prompt"])
        self.assertTrue(any("推进质量摘要=" in line for line in plan["summary_lines"]))
        self.assertTrue(any("当前主线卡点=" in line for line in plan["summary_lines"]))

    def test_agent_task_plan_sorts_recommended_tasks_by_status_quality_and_gap(self) -> None:
        now = datetime.now().replace(hour=10, minute=22, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 3,
                    "quality_state": "low",
                    "quality_reason": "有活动痕迹，但还没推进到本轮主线产物。",
                    "activity_signal_count": 1,
                    "last_active_at": f"{trade_date}T10:18:00",
                },
                {
                    "agent_id": "ashare-risk",
                    "status": "overdue",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "blocked",
                    "quality_reason": "当前岗位已超时，主线推进出现卡点。",
                    "activity_signal_count": 0,
                    "last_active_at": None,
                },
                {
                    "agent_id": "ashare-audit",
                    "status": "working",
                    "covered_case_count": 1,
                    "expected_case_count": 1,
                    "quality_state": "good",
                    "quality_reason": "本轮主线材料已覆盖齐。",
                    "activity_signal_count": 1,
                    "last_active_at": f"{trade_date}T10:20:00",
                },
            ],
            "attention_items": [
                {"agent_id": "ashare-research", "status": "needs_work", "covered_case_count": 0, "expected_case_count": 3, "quality_state": "low", "quality_reason": "有活动痕迹，但还没推进到本轮主线产物。"},
                {"agent_id": "ashare-risk", "status": "overdue", "covered_case_count": 0, "expected_case_count": 1, "quality_state": "blocked", "quality_reason": "当前岗位已超时，主线推进出现卡点。"},
            ],
            "notify_items": [
                {"agent_id": "ashare-research", "status": "needs_work", "covered_case_count": 0, "expected_case_count": 3, "quality_state": "low", "quality_reason": "有活动痕迹，但还没推进到本轮主线产物。"},
                {"agent_id": "ashare-risk", "status": "overdue", "covered_case_count": 0, "expected_case_count": 1, "quality_state": "blocked", "quality_reason": "当前岗位已超时，主线推进出现卡点。"},
            ],
        }
        plan = build_agent_task_plan(payload, now=now)
        recommended_ids = [item["agent_id"] for item in plan["recommended_tasks"]]
        self.assertEqual(recommended_ids[:2], ["ashare-risk", "ashare-research"])
        self.assertEqual([item["agent_id"] for item in plan["notify_items"]][:2], ["ashare-risk", "ashare-research"])

    def test_agent_task_plan_builds_position_aware_action_reason_for_strategy_with_room(self) -> None:
        now = datetime.now().replace(hour=14, minute=8, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": None,
            "round": 0,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
        }
        plan = build_agent_task_plan(
            payload,
            position_context={
                "position_count": 1,
                "current_total_ratio": 0.12,
                "equity_position_limit": 0.30,
                "available_test_trade_value": 18000.0,
                "stock_test_budget_amount": 30000.0,
            },
            now=now,
        )
        strategy_item = next(item for item in plan["notify_items"] if item["agent_id"] == "ashare-strategy")
        self.assertIn("还有仓位空间", strategy_item["supervision_action_reason"])
        self.assertIn("优先判断补仓、扩新机会还是重组 runtime", strategy_item["supervision_action_reason"])

    def test_agent_task_plan_builds_position_aware_action_reason_for_strategy_near_full(self) -> None:
        now = datetime.now().replace(hour=14, minute=18, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": None,
            "round": 0,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
        }
        plan = build_agent_task_plan(
            payload,
            position_context={
                "position_count": 3,
                "current_total_ratio": 0.29,
                "equity_position_limit": 0.30,
                "available_test_trade_value": 3000.0,
                "stock_test_budget_amount": 30000.0,
            },
            now=now,
        )
        strategy_item = next(item for item in plan["notify_items"] if item["agent_id"] == "ashare-strategy")
        self.assertIn("已接近满仓", strategy_item["supervision_action_reason"])
        self.assertIn("优先做替换仓位和去弱留强", strategy_item["supervision_action_reason"])

    def test_agent_task_plan_builds_market_event_aware_action_reason(self) -> None:
        now = datetime.now().replace(hour=10, minute=18, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "本轮主线产物仍未补齐。",
                }
            ],
        }
        plan = build_agent_task_plan(
            payload,
            latest_market_change_at=f"{trade_date}T10:05:00",
            now=now,
        )
        research_item = next(item for item in plan["notify_items"] if item["agent_id"] == "ashare-research")
        self.assertIn("刚出现新的市场变化", research_item["supervision_action_reason"])
        self.assertIn("研究侧应优先把新异动、新热点和消息催化落成正式判断", research_item["supervision_action_reason"])

    def test_agent_task_plan_marks_market_response_lag_when_market_changes_without_new_artifact(self) -> None:
        now = datetime.now().replace(hour=10, minute=18, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "working",
                    "reasons": ["最近策略/调参提案=10:10，但仍停留在内部讨论"],
                    "last_active_at": f"{trade_date}T10:10:00",
                    "activity_label": "策略/调参提案",
                }
            ],
            "attention_items": [],
            "notify_items": [],
        }
        plan = build_agent_task_plan(
            payload,
            latest_market_change_at=f"{trade_date}T10:06:00",
            now=now,
        )
        strategy_task = next(item for item in plan["tasks"] if item["agent_id"] == "ashare-strategy")
        self.assertEqual(strategy_task["market_response_state"], "lagging")
        self.assertIn("市场响应迟滞", strategy_task["task_reason"])
        self.assertTrue(strategy_task["dispatch_recommended"])
        self.assertIn("不要只汇报是否调用了哪些工具", strategy_task["task_prompt"])
        self.assertTrue(any("市场响应迟滞=" in line for line in plan["summary_lines"]))

    def test_agent_task_plan_exposes_market_response_targets_for_strategy(self) -> None:
        now = datetime.now().replace(hour=10, minute=26, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "当前还缺正式策略产物。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "quality_state": "low",
                    "quality_reason": "当前还缺正式策略产物。",
                }
            ],
            "notify_items": [],
        }
        plan = build_agent_task_plan(
            payload,
            latest_market_change_at=f"{trade_date}T10:12:00",
            now=now,
        )
        strategy_task = next(item for item in plan["tasks"] if item["agent_id"] == "ashare-strategy")
        self.assertEqual(strategy_task["response_lane"], "盘中机会响应")
        self.assertIn("新compose", strategy_task["market_response_targets"])
        self.assertIn("打法切换结论", strategy_task["market_response_targets"])
        self.assertIn("市场响应产物", strategy_task["task_prompt"])

    def test_agent_task_plan_sets_response_lane_for_tail_positions_and_post_close_learning(self) -> None:
        tail_now = datetime.now().replace(hour=14, minute=48, second=0, microsecond=0)
        trade_date = tail_now.date().isoformat()
        tail_payload = {
            "trade_date": trade_date,
            "cycle_state": None,
            "round": 0,
            "items": [
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T14:40:00"}
            ],
            "attention_items": [],
            "notify_items": [],
        }
        tail_plan = build_agent_task_plan(
            tail_payload,
            position_context={
                "position_count": 2,
                "current_total_ratio": 0.24,
                "equity_position_limit": 0.30,
                "available_test_trade_value": 6000.0,
                "stock_test_budget_amount": 30000.0,
            },
            now=tail_now,
        )
        tail_task = next(item for item in tail_plan["tasks"] if item["agent_id"] == "ashare-strategy")
        self.assertEqual(tail_task["response_lane"], "尾盘持仓收口")
        self.assertIn("做T/换仓方案", tail_task["market_response_targets"])

        post_close_now = datetime.now().replace(hour=16, minute=18, second=0, microsecond=0)
        post_close_payload = {
            "trade_date": trade_date,
            "cycle_state": None,
            "round": 0,
            "items": [
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T16:10:00"}
            ],
            "attention_items": [],
            "notify_items": [],
        }
        post_close_plan = build_agent_task_plan(post_close_payload, now=post_close_now)
        post_close_task = next(item for item in post_close_plan["tasks"] if item["agent_id"] == "ashare-strategy")
        self.assertEqual(post_close_task["response_lane"], "盘后学习")
        self.assertIn("盘后学习", post_close_task["market_response_targets"])
        self.assertIn("次日策略预案", post_close_task["market_response_targets"])

    def test_agent_task_plan_builds_execution_pending_aware_action_reason(self) -> None:
        now = datetime.now().replace(hour=10, minute=28, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-audit",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "low",
                    "quality_reason": "当前缺少可确认的新产出。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-audit",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "low",
                    "quality_reason": "当前缺少可确认的新产出。",
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-audit",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "low",
                    "quality_reason": "当前缺少可确认的新产出。",
                }
            ],
        }
        plan = build_agent_task_plan(
            payload,
            execution_summary={
                "intent_count": 2,
                "dispatch_status": "preview",
                "submitted_count": 0,
                "preview_count": 2,
                "blocked_count": 0,
            },
            now=now,
        )
        audit_item = next(item for item in plan["notify_items"] if item["agent_id"] == "ashare-audit")
        self.assertIn("执行链当前处于 preview", audit_item["supervision_action_reason"])
        self.assertIn("审计侧应优先核对执行与讨论是否一致，并更新纪要", audit_item["supervision_action_reason"])

    def test_agent_task_plan_builds_real_trade_aware_action_reason(self) -> None:
        now = datetime.now().replace(hour=16, minute=18, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "final_review_ready",
            "round": 2,
            "items": [
                {
                    "agent_id": "ashare-risk",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "low",
                    "quality_reason": "当前缺少可确认的新产出。",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-risk",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "low",
                    "quality_reason": "当前缺少可确认的新产出。",
                }
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-risk",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "quality_state": "low",
                    "quality_reason": "当前缺少可确认的新产出。",
                }
            ],
        }
        plan = build_agent_task_plan(
            payload,
            execution_summary={
                "latest_execution_reconciliation": {
                    "status": "ok",
                    "trade_count": 2,
                }
            },
            now=now,
        )
        risk_item = next(item for item in plan["notify_items"] if item["agent_id"] == "ashare-risk")
        self.assertIn("已出现真实成交/对账结果", risk_item["supervision_action_reason"])
        self.assertIn("风控侧应优先做成交后仓位与风险复核", risk_item["supervision_action_reason"])

    def test_agent_task_plan_throttles_repeated_dispatch_in_same_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(Path(tmp_dir) / "task_dispatch_state.json")
            now = datetime.now().replace(hour=10, minute=15, second=0, microsecond=0)
            trade_date = now.date().isoformat()
            payload = {
                "trade_date": trade_date,
                "cycle_state": None,
                "round": 0,
                "items": [
                    {
                        "agent_id": "ashare-strategy",
                        "status": "working",
                        "reasons": ["最近策略/调参提案=2026-04-17T10:10:00"],
                        "last_active_at": f"{trade_date}T10:10:00",
                    }
                ],
                "attention_items": [],
                "notify_items": [],
            }
            first_plan = build_agent_task_plan(payload, meeting_state_store=store, now=now)
            self.assertEqual(first_plan["phase"]["code"], "morning_session")
            self.assertEqual(len(first_plan["recommended_tasks"]), 1)
            first_task = first_plan["recommended_tasks"][0]
            record_agent_task_dispatch(
                store,
                trade_date,
                agent_id=first_task["agent_id"],
                dispatch_key=first_task["dispatch_key"],
                task_payload=first_task,
                sent_at=f"{trade_date}T10:15:00",
            )

            second_plan = build_agent_task_plan(
                payload,
                meeting_state_store=store,
                now=now.replace(minute=18),
            )
            self.assertEqual(len(second_plan["recommended_tasks"]), 0)

    def test_agent_task_plan_auto_completes_dispatched_task_when_new_activity_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(Path(tmp_dir) / "task_dispatch_state.json")
            now = datetime.now().replace(hour=16, minute=10, second=0, microsecond=0)
            trade_date = now.date().isoformat()
            initial_payload = {
                "trade_date": trade_date,
                "cycle_state": None,
                "round": 0,
                "items": [
                    {
                        "agent_id": "ashare-audit",
                        "status": "working",
                        "reasons": ["最近审计/纪要复核=旧时间"],
                        "last_active_at": f"{trade_date}T15:40:00",
                        "activity_label": "审计/纪要复核",
                    }
                ],
                "attention_items": [],
                "notify_items": [],
            }
            first_plan = build_agent_task_plan(initial_payload, meeting_state_store=store, now=now)
            first_task = first_plan["recommended_tasks"][0]
            record_agent_task_dispatch(
                store,
                trade_date,
                agent_id=first_task["agent_id"],
                dispatch_key=first_task["dispatch_key"],
                task_payload=first_task,
                sent_at=f"{trade_date}T16:10:00",
            )

            updated_payload = {
                **initial_payload,
                "items": [
                    {
                        "agent_id": "ashare-audit",
                        "status": "working",
                        "reasons": ["最近审计/纪要复核=新时间"],
                        "last_active_at": f"{trade_date}T16:16:00",
                        "activity_label": "审计/纪要复核",
                    }
                ],
            }
            second_plan = build_agent_task_plan(
                updated_payload,
                meeting_state_store=store,
                now=now.replace(minute=18),
            )
            self.assertEqual(len(second_plan["recommended_tasks"]), 0)
            audit_task = next(item for item in second_plan["tasks"] if item["agent_id"] == "ashare-audit")
            self.assertEqual(audit_task["last_completed_at"], f"{trade_date}T16:16:00")

    def test_agent_task_plan_generates_follow_up_for_coordinator_when_four_roles_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(Path(tmp_dir) / "task_dispatch_state.json")
            now = datetime.now().replace(hour=10, minute=28, second=0, microsecond=0)
            trade_date = now.date().isoformat()
            for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
                record_agent_task_completion(
                    store,
                    trade_date,
                    agent_id=agent_id,
                    completion_type="activity_observed",
                    completion_payload={"source": "test"},
                    completed_at=f"{trade_date}T10:26:00",
                )
            payload = {
                "trade_date": trade_date,
                "cycle_state": "round_1_running",
                "round": 1,
                "items": [
                    {"agent_id": "ashare", "status": "working", "reasons": ["当前讨论态=round_1_running"], "last_active_at": f"{trade_date}T10:20:00"},
                    {"agent_id": "ashare-research", "status": "working", "reasons": ["最近研究/事件产出"], "last_active_at": f"{trade_date}T10:26:00"},
                    {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略/调参提案"], "last_active_at": f"{trade_date}T10:26:00"},
                    {"agent_id": "ashare-risk", "status": "working", "reasons": ["最近风控/执行预检"], "last_active_at": f"{trade_date}T10:26:00"},
                    {"agent_id": "ashare-audit", "status": "working", "reasons": ["最近审计/纪要复核"], "last_active_at": f"{trade_date}T10:26:00"},
                ],
                "attention_items": [],
                "notify_items": [],
            }
            plan = build_agent_task_plan(payload, meeting_state_store=store, now=now)
            ashare_task = next(item for item in plan["tasks"] if item["agent_id"] == "ashare")
            self.assertEqual(ashare_task["task_mode"], "follow_up")
            self.assertTrue(ashare_task["dispatch_recommended"])
            self.assertIn("立即刷新 discussion cycle", ashare_task["task_prompt"])
            self.assertTrue(any("续发任务=" in line for line in plan["summary_lines"]))

    def test_agent_task_plan_pushes_strategy_to_compose_when_cycle_idle(self) -> None:
        now = datetime.now().replace(hour=10, minute=12, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "idle",
            "round": 0,
            "items": [
                {"agent_id": "ashare", "status": "working", "reasons": ["当前讨论态=idle"], "last_active_at": f"{trade_date}T10:10:00"},
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T10:10:00"},
            ],
            "attention_items": [],
            "notify_items": [],
        }
        plan = build_agent_task_plan(payload, now=now)
        task_map = {item["agent_id"]: item for item in plan["tasks"]}
        self.assertEqual(task_map["ashare"]["task_mode"], "follow_up")
        self.assertIn("bootstrap/round 1", task_map["ashare"]["task_prompt"])
        self.assertEqual(task_map["ashare-strategy"]["task_mode"], "follow_up")
        self.assertIn("compose 草案", task_map["ashare-strategy"]["task_prompt"])
        self.assertIn("/runtime/jobs/compose-from-brief", task_map["ashare-strategy"]["task_prompt"])

    def test_agent_task_plan_generates_position_driven_follow_up_tasks(self) -> None:
        now = datetime.now().replace(hour=14, minute=8, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": None,
            "round": 0,
            "items": [
                {"agent_id": "ashare-research", "status": "working", "reasons": ["最近研究产出"], "last_active_at": f"{trade_date}T14:05:00"},
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T14:05:00"},
                {"agent_id": "ashare-risk", "status": "working", "reasons": ["最近风控产出"], "last_active_at": f"{trade_date}T14:05:00"},
                {"agent_id": "ashare-executor", "status": "working", "reasons": ["最近执行产出"], "last_active_at": f"{trade_date}T14:05:00"},
            ],
            "attention_items": [],
            "notify_items": [],
        }
        plan = build_agent_task_plan(
            payload,
            position_context={
                "position_count": 2,
                "current_total_ratio": 0.12,
                "equity_position_limit": 0.30,
                "available_test_trade_value": 18000.0,
                "stock_test_budget_amount": 30000.0,
            },
            now=now,
        )
        task_map = {item["agent_id"]: item for item in plan["tasks"]}
        self.assertEqual(task_map["ashare-strategy"]["task_mode"], "follow_up")
        self.assertIn("仓位还没打满", task_map["ashare-strategy"]["task_prompt"])
        self.assertEqual(task_map["ashare-research"]["task_mode"], "follow_up")
        self.assertIn("还有可用仓位", task_map["ashare-research"]["task_prompt"])
        self.assertEqual(task_map["ashare-risk"]["task_mode"], "follow_up")
        self.assertIn("已经有持仓在场", task_map["ashare-risk"]["task_prompt"])
        self.assertEqual(task_map["ashare-executor"]["task_mode"], "follow_up")
        self.assertIn("做 T", task_map["ashare-executor"]["task_prompt"])

    def test_agent_task_plan_switches_to_replacement_and_execution_follow_up_when_near_full_and_submitted(self) -> None:
        now = datetime.now().replace(hour=10, minute=42, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {"agent_id": "ashare", "status": "working", "reasons": ["当前讨论态=round_1_running"], "last_active_at": f"{trade_date}T10:40:00"},
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T10:40:00"},
                {"agent_id": "ashare-risk", "status": "working", "reasons": ["最近风控产出"], "last_active_at": f"{trade_date}T10:40:00"},
                {"agent_id": "ashare-audit", "status": "working", "reasons": ["最近审计产出"], "last_active_at": f"{trade_date}T10:40:00"},
                {"agent_id": "ashare-executor", "status": "working", "reasons": ["最近执行产出"], "last_active_at": f"{trade_date}T10:40:00"},
            ],
            "attention_items": [],
            "notify_items": [],
        }
        plan = build_agent_task_plan(
            payload,
            execution_summary={
                "intent_count": 2,
                "dispatch_status": "submitted",
                "submitted_count": 1,
                "preview_count": 0,
                "blocked_count": 0,
            },
            position_context={
                "position_count": 3,
                "current_total_ratio": 0.29,
                "equity_position_limit": 0.30,
                "available_test_trade_value": 3000.0,
                "stock_test_budget_amount": 30000.0,
            },
            now=now,
        )
        task_map = {item["agent_id"]: item for item in plan["tasks"]}
        self.assertEqual(task_map["ashare-strategy"]["task_mode"], "follow_up")
        self.assertIn("替换仓位评估", task_map["ashare-strategy"]["task_prompt"])
        self.assertEqual(task_map["ashare-risk"]["task_mode"], "follow_up")
        self.assertIn("已提交 1 笔", task_map["ashare-risk"]["task_prompt"])
        self.assertEqual(task_map["ashare"]["task_mode"], "follow_up")
        self.assertIn("执行侧 submitted=1", task_map["ashare"]["task_prompt"])

    def test_agent_task_plan_generates_learning_follow_up_after_real_trades(self) -> None:
        now = datetime.now().replace(hour=16, minute=18, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": "final_review_ready",
            "round": 2,
            "items": [
                {"agent_id": "ashare", "status": "working", "reasons": ["总协调在线"], "last_active_at": f"{trade_date}T16:10:00"},
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T16:10:00"},
                {"agent_id": "ashare-risk", "status": "working", "reasons": ["最近风控产出"], "last_active_at": f"{trade_date}T16:10:00"},
                {"agent_id": "ashare-audit", "status": "working", "reasons": ["最近审计产出"], "last_active_at": f"{trade_date}T16:10:00"},
            ],
            "attention_items": [],
            "notify_items": [],
        }
        plan = build_agent_task_plan(
            payload,
            execution_summary={
                "dispatch_status": "submitted",
                "submitted_count": 2,
                "latest_execution_reconciliation": {
                    "status": "ok",
                    "trade_count": 3,
                    "matched_order_count": 2,
                    "summary_lines": ["执行对账完成: matched_orders=2 filled_orders=2 trades=3。"],
                },
                "latest_review_board_summary_lines": ["复盘看板已生成: 风险偏高战法需降权。"],
                "latest_nightly_sandbox": {
                    "trade_date": trade_date,
                    "summary_lines": ["夜间沙盘尚未产出。"],
                },
            },
            now=now,
        )
        task_map = {item["agent_id"]: item for item in plan["tasks"]}
        self.assertEqual(task_map["ashare"]["task_mode"], "follow_up")
        self.assertIn("学习归因", task_map["ashare"]["task_prompt"])
        self.assertEqual(task_map["ashare-audit"]["task_mode"], "follow_up")
        self.assertIn("真实成交", task_map["ashare-audit"]["task_prompt"])
        self.assertEqual(task_map["ashare-risk"]["task_mode"], "follow_up")
        self.assertIn("成交后", task_map["ashare-risk"]["task_prompt"])
        self.assertEqual(task_map["ashare-strategy"]["task_mode"], "follow_up")
        self.assertIn("参数", task_map["ashare-strategy"]["task_prompt"])

    def test_agent_task_plan_auto_completes_strategy_follow_up_when_nightly_sandbox_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(Path(tmp_dir) / "task_dispatch_state.json")
            now = datetime.now().replace(hour=20, minute=10, second=0, microsecond=0)
            trade_date = now.date().isoformat()
            initial_payload = {
                "trade_date": trade_date,
                "cycle_state": None,
                "round": 0,
                "items": [
                    {
                        "agent_id": "ashare-strategy",
                        "status": "working",
                        "reasons": ["最近策略/调参提案=旧时间"],
                        "last_active_at": f"{trade_date}T19:30:00",
                        "activity_label": "策略/调参提案",
                    }
                ],
                "attention_items": [],
                "notify_items": [],
            }
            first_plan = build_agent_task_plan(initial_payload, meeting_state_store=store, now=now)
            first_task = first_plan["recommended_tasks"][0]
            record_agent_task_dispatch(
                store,
                trade_date,
                agent_id=first_task["agent_id"],
                dispatch_key=first_task["dispatch_key"],
                task_payload=first_task,
                sent_at=f"{trade_date}T20:10:00",
            )
            updated_payload = {
                **initial_payload,
                "items": [
                    {
                        "agent_id": "ashare-strategy",
                        "status": "working",
                        "reasons": ["最近夜间沙盘=新时间"],
                        "last_active_at": f"{trade_date}T20:16:00",
                        "activity_label": "策略/调参提案",
                    }
                ],
            }
            second_plan = build_agent_task_plan(
                updated_payload,
                meeting_state_store=store,
                now=now.replace(minute=18),
            )
            self.assertEqual(len(second_plan["recommended_tasks"]), 0)
            strategy_task = next(item for item in second_plan["tasks"] if item["agent_id"] == "ashare-strategy")
            self.assertEqual(strategy_task["last_completed_at"], f"{trade_date}T20:16:00")

    def test_agent_task_plan_does_not_auto_complete_when_new_activity_is_not_aligned_with_dispatched_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(Path(tmp_dir) / "task_dispatch_state.json")
            now = datetime.now().replace(hour=20, minute=10, second=0, microsecond=0)
            trade_date = now.date().isoformat()
            initial_payload = {
                "trade_date": trade_date,
                "cycle_state": None,
                "round": 0,
                "items": [
                    {
                        "agent_id": "ashare-strategy",
                        "status": "working",
                        "reasons": ["最近策略/调参提案=旧时间"],
                        "last_active_at": f"{trade_date}T19:30:00",
                        "activity_label": "策略/调参提案",
                    }
                ],
                "attention_items": [],
                "notify_items": [],
            }
            first_plan = build_agent_task_plan(
                initial_payload,
                execution_summary={
                    "latest_review_board_summary_lines": ["复盘看板已生成: 执行偏追高，需修正。"],
                },
                meeting_state_store=store,
                now=now,
            )
            first_task = first_plan["recommended_tasks"][0]
            self.assertIn("夜间沙盘", first_task["task_prompt"])
            record_agent_task_dispatch(
                store,
                trade_date,
                agent_id=first_task["agent_id"],
                dispatch_key=first_task["dispatch_key"],
                task_payload=first_task,
                sent_at=f"{trade_date}T20:10:00",
            )

            updated_payload = {
                **initial_payload,
                "items": [
                    {
                        "agent_id": "ashare-strategy",
                        "status": "working",
                        "reasons": ["最近策略/调参提案=新时间，但夜间沙盘仍未形成"],
                        "last_active_at": f"{trade_date}T20:16:00",
                        "activity_label": "策略/调参提案",
                    }
                ],
            }
            second_plan = build_agent_task_plan(
                updated_payload,
                execution_summary={
                    "latest_review_board_summary_lines": ["复盘看板已生成: 执行偏追高，需修正。"],
                },
                meeting_state_store=store,
                now=now.replace(minute=18),
            )
            strategy_task = next(item for item in second_plan["tasks"] if item["agent_id"] == "ashare-strategy")
            self.assertIsNone(strategy_task["last_completed_at"])
            self.assertTrue(any(item["agent_id"] == "ashare-strategy" for item in second_plan["recommended_tasks"]))

    def test_agent_task_plan_generates_review_board_and_nightly_sandbox_follow_up(self) -> None:
        now = datetime.now().replace(hour=20, minute=36, second=0, microsecond=0)
        trade_date = now.date().isoformat()
        payload = {
            "trade_date": trade_date,
            "cycle_state": None,
            "round": 0,
            "items": [
                {"agent_id": "ashare-strategy", "status": "working", "reasons": ["最近策略产出"], "last_active_at": f"{trade_date}T20:10:00"},
                {"agent_id": "ashare-audit", "status": "working", "reasons": ["最近审计产出"], "last_active_at": f"{trade_date}T20:12:00"},
            ],
            "attention_items": [],
            "notify_items": [],
        }
        plan = build_agent_task_plan(
            payload,
            execution_summary={
                "latest_review_board_summary_lines": ["复盘看板已生成: 执行偏追高，需修正。"],
            },
            now=now,
        )
        task_map = {item["agent_id"]: item for item in plan["tasks"]}
        self.assertEqual(task_map["ashare-strategy"]["task_mode"], "follow_up")
        self.assertIn("夜间沙盘", task_map["ashare-strategy"]["task_prompt"])

        plan_after_sandbox = build_agent_task_plan(
            payload,
            execution_summary={
                "latest_review_board_summary_lines": ["复盘看板已生成: 执行偏追高，需修正。"],
                "latest_nightly_sandbox": {
                    "trade_date": trade_date,
                    "summary_lines": ["夜间沙盘完成: 次日优先看分歧转一致。"],
                },
            },
            now=now,
        )
        task_map = {item["agent_id"]: item for item in plan_after_sandbox["tasks"]}
        self.assertEqual(task_map["ashare-audit"]["task_mode"], "follow_up")
        self.assertIn("次日预案", task_map["ashare-audit"]["task_prompt"])

    def test_agent_supervision_template_renders_dispatch_style_content(self) -> None:
        content = agent_supervision_template(
            "Agent 自动催办",
            ["trade_date=2026-04-17 supervision_items=2 attention=1", "trade_date=2026-04-17 supervision_items=2 attention=1"],
            [
                {
                    "agent_id": "ashare-strategy",
                    "status": "working",
                    "phase_label": "盘后复盘",
                    "task_mode": "follow_up",
                    "quality_state": "partial",
                    "quality_reason": "盘后已经有动作，但夜间沙盘和次日预案还没补齐。",
                    "last_active_at": "2026-04-17T20:10:00",
                    "reasons": ["最近策略产出=2026-04-17T20:10:00"],
                    "task_reason": "盘后 review board 已落地；策略侧应继续推进夜间沙盘与次日预案",
                    "supervision_tier": "remind",
                    "supervision_tier_reason": "已有任务但缺少本阶段新产出，应发普通催办。",
                    "supervision_action_reason": "ashare-strategy 现在必须优先处理：仍缺 1 份主线材料；夜间沙盘还没落地。",
                    "task_prompt": "当前阶段是盘后复盘。复盘看板已经出来，但夜间沙盘还没形成。请把盘后事实、风控结论和执行反馈转成次日打法推演。",
                    "expected_outputs": ["夜间沙盘推演", "次日核心假设", "战法/参数优先级"],
                }
            ],
        )
        self.assertEqual(content.count("trade_date=2026-04-17 supervision_items=2 attention=1"), 1)
        self.assertIn("阶段=盘后复盘", content)
        self.assertIn("模式=follow_up", content)
        self.assertIn("质量=partial", content)
        self.assertIn("推进质量", content)
        self.assertIn("派工缘由", content)
        self.assertIn("催办等级", content)
        self.assertIn("优先原因", content)
        self.assertIn("预期产物", content)

    def test_annotate_supervision_payload_escalates_repeated_overdue_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(Path(tmp_dir) / "supervision_state.json")
            trade_date = "2026-04-17"
            payload = {
                "trade_date": trade_date,
                "cycle_state": "round_1_running",
                "round": 1,
                "attention_items": [
                    {
                        "agent_id": "ashare-strategy",
                        "status": "overdue",
                        "covered_case_count": 0,
                        "expected_case_count": 2,
                    }
                ],
                "summary_lines": ["trade_date=2026-04-17 supervision_items=1 attention=1"],
            }
            first = annotate_supervision_payload(payload, store)
            self.assertFalse(first["escalated"])
            self.assertEqual(first["notification_title"], "Agent 自动催办")
            record_supervision_notification(
                store,
                trade_date,
                signature=str(first.get("attention_signature") or ""),
                level="warning",
                item_count=1,
            )
            control_state = store.get("agent_supervision_control:2026-04-17", {})
            control_state["last_notification"]["sent_at"] = "2026-04-17T00:00:00"
            store.set("agent_supervision_control:2026-04-17", control_state)
            second = annotate_supervision_payload(payload, store)
            self.assertTrue(second["escalated"])
            self.assertEqual(second["notification_title"], "Agent 升级催办")
            self.assertEqual(second["notification_level"], "critical")
            notify_item = second["notify_items"][0]
            self.assertEqual(notify_item["supervision_tier"], "escalate")
            self.assertIn("重复催办未解除", notify_item["supervision_action_reason"])
            self.assertTrue(any("升级原因:" in line for line in second["summary_lines"]))

    def test_annotate_supervision_payload_marks_low_quality_when_activity_exists_but_round_coverage_missing(self) -> None:
        payload = {
            "trade_date": "2026-04-17",
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 3,
                    "activity_label": "策略/调参提案",
                    "activity_signal_count": 1,
                    "last_active_at": "2026-04-17T10:05:00",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 3,
                }
            ],
            "summary_lines": ["trade_date=2026-04-17 supervision_items=1 attention=1"],
        }
        annotated = annotate_supervision_payload(payload, None)
        item = annotated["items"][0]
        self.assertEqual(item["quality_state"], "low")
        self.assertIn("有活动痕迹", item["quality_reason"])
        attention_item = annotated["attention_items"][0]
        self.assertEqual(attention_item["quality_state"], "low")
        self.assertTrue(any("主线卡点" in line for line in annotated["summary_lines"]))
        self.assertTrue(any("有活动但未推进主线" in line for line in annotated["summary_lines"]))

    def test_annotate_supervision_payload_builds_role_specific_action_reasons(self) -> None:
        payload = {
            "trade_date": "2026-04-17",
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-research",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "phase_label": "上午盘中",
                    "last_active_at": "2026-04-17T10:05:00",
                },
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "phase_label": "上午盘中",
                    "last_active_at": "2026-04-17T10:06:00",
                },
                {
                    "agent_id": "ashare-risk",
                    "status": "overdue",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "phase_label": "上午盘中",
                },
                {
                    "agent_id": "ashare-audit",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 1,
                    "phase_label": "上午盘中",
                },
            ],
            "attention_items": [
                {"agent_id": "ashare-research", "status": "needs_work", "covered_case_count": 0, "expected_case_count": 2, "phase_label": "上午盘中", "last_active_at": "2026-04-17T10:05:00"},
                {"agent_id": "ashare-strategy", "status": "needs_work", "covered_case_count": 0, "expected_case_count": 2, "phase_label": "上午盘中", "last_active_at": "2026-04-17T10:06:00"},
                {"agent_id": "ashare-risk", "status": "overdue", "covered_case_count": 0, "expected_case_count": 1, "phase_label": "上午盘中"},
                {"agent_id": "ashare-audit", "status": "needs_work", "covered_case_count": 0, "expected_case_count": 1, "phase_label": "上午盘中"},
            ],
            "summary_lines": ["trade_date=2026-04-17 supervision_items=4 attention=4"],
        }
        annotated = annotate_supervision_payload(payload, None)
        action_reason_map = {
            item["agent_id"]: item["supervision_action_reason"]
            for item in annotated["notify_items"]
        }
        self.assertIn("市场事实、热点变化和机会票", action_reason_map["ashare-research"])
        self.assertIn("打法、参数和 runtime 组织结论", action_reason_map["ashare-strategy"])
        self.assertIn("阻断/放行判断和风险边界", action_reason_map["ashare-risk"])
        self.assertIn("证据链、反证和纪要结论", action_reason_map["ashare-audit"])

    def test_annotate_supervision_payload_builds_phase_specific_action_reasons_for_same_role(self) -> None:
        auction_payload = {
            "trade_date": "2026-04-17",
            "cycle_state": "round_1_running",
            "round": 1,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "phase_label": "集合竞价",
                    "last_active_at": "2026-04-17T09:24:00",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "phase_label": "集合竞价",
                    "last_active_at": "2026-04-17T09:24:00",
                }
            ],
            "summary_lines": ["trade_date=2026-04-17 supervision_items=1 attention=1"],
        }
        post_close_payload = {
            "trade_date": "2026-04-17",
            "cycle_state": "final_review_ready",
            "round": 2,
            "items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "phase_label": "盘后复盘",
                    "last_active_at": "2026-04-17T16:10:00",
                }
            ],
            "attention_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "needs_work",
                    "covered_case_count": 0,
                    "expected_case_count": 2,
                    "phase_label": "盘后复盘",
                    "last_active_at": "2026-04-17T16:10:00",
                }
            ],
            "summary_lines": ["trade_date=2026-04-17 supervision_items=1 attention=1"],
        }
        auction_reason = annotate_supervision_payload(auction_payload, None)["notify_items"][0]["supervision_action_reason"]
        post_close_reason = annotate_supervision_payload(post_close_payload, None)["notify_items"][0]["supervision_action_reason"]
        self.assertIn("竞价阶段先决定是否切换打法", auction_reason)
        self.assertIn("盘后先把打法归因、参数得失和次日预案沉淀下来", post_close_reason)

    def test_feishu_supervision_answer_includes_escalation_reason(self) -> None:
        payload = {
            "trade_date": "2026-04-17",
            "task_dispatch_plan": {"phase": {"label": "上午盘中"}},
            "attention_items": [
                {"agent_id": "ashare-strategy", "status": "overdue"},
            ],
            "notify_items": [
                {
                    "agent_id": "ashare-strategy",
                    "status": "overdue",
                    "task_reason": "阶段=上午盘中；状态=overdue；推进质量=blocked",
                    "quality_state": "blocked",
                    "quality_reason": "当前岗位已超时，主线推进出现卡点。",
                    "supervision_tier": "escalate",
                    "supervision_action_reason": "ashare-strategy 现在必须优先处理：同一阻塞已持续 300 秒且重复催办未解除；仍缺 2 份主线材料。",
                    "expected_outputs": ["正式观点", "参数结论"],
                }
            ],
            "acknowledged_items": [],
            "items": [],
            "quality_summary_lines": ["推进质量: good=0 partial=0 low=0 blocked=1 observe=0。"],
            "progress_blockers": ["ashare-strategy 本轮仍缺 2 份主线材料，已有活动但未写回观点。"],
            "escalated": True,
        }
        lines = self._build_supervision_answer_lines_for_test(payload)
        self.assertTrue(any("升级原因:" in line for line in lines))
        self.assertTrue(any("优先原因=" in line for line in lines))

    def test_supervision_board_exposes_position_context_for_follow_up_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=100000.0,
                    cash=82000.0,
                )
                execution_adapter.positions["test-account"] = [
                    PositionSnapshot(
                        account_id="test-account",
                        symbol="600519.SH",
                        quantity=100,
                        available=100,
                        cost_price=1800.0,
                        last_price=1805.0,
                    )
                ]
                with TestClient(create_app()) as client:
                    account_payload = client.get("/system/account-state?account_id=test-account&refresh=true").json()
                    self.assertTrue(account_payload["ok"])
                    payload = client.get("/system/agents/supervision-board").json()
                    self.assertTrue(payload["ok"])
                    self.assertIn("position_context", payload["task_dispatch_plan"])
                    self.assertEqual(payload["task_dispatch_plan"]["position_context"]["position_count"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_exposes_task_dispatch_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                now = datetime.now().isoformat()
                research_state_store = get_research_state_store()
                research_state_store.set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": now,
                        "symbols": ["600519.SH"],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=60"
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertIn("task_dispatch_plan", payload)
                    self.assertIn("phase", payload["task_dispatch_plan"])
                    self.assertIn("tasks", payload["task_dispatch_plan"])
                    self.assertTrue(payload["task_dispatch_plan"]["summary_lines"])
                    self.assertTrue(
                        any("task_prompt" in item for item in payload["items"])
                    )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_supervision_board_marks_strategy_param_activity_without_compose_as_needs_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                with patch("ashare_system.apps.system_api.is_trading_session", return_value=True):
                    with TestClient(create_app()) as client:
                        runtime_payload = client.post(
                            "/runtime/jobs/pipeline",
                            json={
                                "symbols": ["600519.SH"],
                                "max_candidates": 1,
                                "account_id": "test-account",
                            },
                        ).json()
                        self.assertTrue(runtime_payload["case_ids"])
                        proposal_payload = client.post(
                            "/system/params/proposals",
                            json={
                                "param_key": "focus_pool_capacity",
                                "new_value": 12,
                                "proposed_by": "ashare-strategy",
                                "structured_by": "ashare-strategy",
                                "reason": "仅测试策略活动痕迹",
                                "status": "proposed",
                            },
                        ).json()
                        self.assertTrue(proposal_payload["ok"])
                        self.assertEqual(proposal_payload["event"]["scope"], "strategy")
                        payload = client.get(
                            f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=60"
                        ).json()
                    self.assertTrue(payload["ok"])
                    strategy_item = next(item for item in payload["items"] if item["agent_id"] == "ashare-strategy")
                    self.assertEqual(strategy_item["status"], "needs_work")
                    self.assertIn("今日尚无 compose 评估账本", "；".join(strategy_item["reasons"]))
                    self.assertEqual(strategy_item["mainline_output_count"], 0)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_discussion_refresh_auto_starts_round_1_when_cycle_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ"],
                            "max_candidates": 2,
                            "account_id": "test-account",
                        },
                    ).json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    bootstrap_payload = client.post(
                        "/system/discussions/cycles/bootstrap",
                        json={"trade_date": trade_date},
                    ).json()
                    self.assertTrue(bootstrap_payload["ok"])
                    self.assertEqual(bootstrap_payload["cycle"]["discussion_state"], "idle")

                    refresh_payload = client.post(
                        f"/system/discussions/cycles/{trade_date}/refresh"
                    ).json()
                    self.assertTrue(refresh_payload["ok"])
                    self.assertEqual(refresh_payload["cycle"]["discussion_state"], "round_1_running")
                    self.assertEqual(refresh_payload["cycle"]["current_round"], 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_batch_opinions_mark_task_completion_and_advance_discussion_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    ).json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180"
                    ).json()
                    self.assertTrue(board_payload["ok"])
                    self.assertIn("task_dispatch_plan", board_payload)

                    response = client.post(
                        "/system/discussions/opinions/batch",
                        json={
                            "auto_rebuild": True,
                            "items": [
                                {
                                    "case_id": case_id,
                                    "round": 1,
                                    "agent_id": "ashare-research",
                                    "stance": "support",
                                    "confidence": "high",
                                    "reasons": ["板块热度提升"],
                                    "evidence_refs": ["event:hot-sector"],
                                },
                                {
                                    "case_id": case_id,
                                    "round": 1,
                                    "agent_id": "ashare-strategy",
                                    "stance": "support",
                                    "confidence": "high",
                                    "reasons": ["走势与打法匹配"],
                                    "evidence_refs": ["runtime:compose"],
                                },
                                {
                                    "case_id": case_id,
                                    "round": 1,
                                    "agent_id": "ashare-risk",
                                    "stance": "support",
                                    "confidence": "medium",
                                    "reasons": ["风险可控"],
                                    "evidence_refs": ["risk:precheck"],
                                },
                                {
                                    "case_id": case_id,
                                    "round": 1,
                                    "agent_id": "ashare-audit",
                                    "stance": "support",
                                    "confidence": "medium",
                                    "reasons": ["证据链完整"],
                                    "evidence_refs": ["audit:review"],
                                },
                            ],
                        },
                    ).json()
                    self.assertTrue(response["ok"])
                    self.assertIn("discussion_advance", response)
                    self.assertTrue(response["discussion_advance"]["available"])
                    self.assertTrue(response["discussion_advance"]["advanced"])
                    self.assertNotEqual(
                        response["discussion_advance"]["cycle_after"]["discussion_state"],
                        "round_1_running",
                    )
                    self.assertEqual(
                        set(response["discussion_advance"]["completed_agents"]),
                        {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"},
                    )

                    meeting_state_store = get_meeting_state_store()
                    dispatch_state = meeting_state_store.get(task_dispatch_control_key(trade_date), {})
                    agents_state = dict(dispatch_state.get("agents") or {})
                    self.assertEqual(
                        set(agents_state.keys()) & {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"},
                        {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"},
                    )
                    for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
                        self.assertTrue(agents_state[agent_id].get("completed_at"))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_supervision_ack_route_parses_text_and_updates_target_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    trade_date = str(runtime_response.json()["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)

                    payload = client.post(
                        "/system/feishu/supervision/ack",
                        json={
                            "trade_date": trade_date,
                            "text": "研究已收到催办，转入处理",
                            "actor": "feishu-user/u_test",
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["matched"])
                    self.assertEqual(payload["parsed_agent_ids"], ["ashare-research"])
                    self.assertEqual(payload["acked_count"], 1)

                    board_payload = client.get(
                        f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180"
                    ).json()
                    research_item = next(item for item in board_payload["attention_items"] if item["agent_id"] == "ashare-research")
                    self.assertTrue(research_item["acknowledged"])
                    self.assertEqual(research_item["acknowledged_by"], "feishu-user/u_test")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_supervision_ack_route_rejects_unrecognized_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/supervision/ack",
                        json={
                            "text": "今天天气不错",
                            "actor": "feishu-user/u_test",
                        },
                    ).json()
                    self.assertFalse(payload["ok"])
                    self.assertFalse(payload["matched"])
                    self.assertEqual(payload["unmatched_reason"], "text_not_recognized_as_supervision_ack")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_supports_url_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "type": "url_verification",
                            "token": "verify-token",
                            "challenge": "challenge-value",
                        },
                    ).json()
                    self.assertEqual(payload["challenge"], "challenge-value")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_processes_message_receive_event_for_supervision_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    trade_date = str(runtime_response.json()["generated_at"])[:10]

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)

                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "content": "{\"text\":\"研究已收到催办，转入处理\"}",
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["parsed_agent_ids"], ["ashare-research"])
                    self.assertEqual(payload["acked_count"], 1)
                    self.assertEqual(payload["chat_id"], "oc_test_chat")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_supports_link_preview_with_header_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "schema": "2.0",
                            "header": {
                                "event_type": "url.preview.get",
                                "token": "verify-token",
                            },
                            "event": {
                                "context": {
                                    "url": "https://yxzmini.com/pach/system/agents/supervision-board",
                                }
                            },
                        },
                    ).json()
                    self.assertEqual(payload["inline"]["title"], "Agent 监督看板")
                    self.assertIn("是否超时", payload["inline"]["summary"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_explains_control_plane_link_from_group_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "content": "{\"text\":\"@_user_1 https://yxzmini.com/pach/system/workflow/mainline\"}",
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["reason"], "control_plane_link_explained")
                    self.assertEqual(payload["endpoint"], "/system/workflow/mainline")
                    self.assertEqual(payload["reply_to_chat_id"], "oc_test_chat")
                    self.assertTrue(any("交易主线流程" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("盘前预热" in line for line in payload["reply_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_addressed_natural_language_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 现在状态怎么样\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["reason"], "natural_language_question_answered")
                    self.assertEqual(payload["topic"], "status")
                    self.assertEqual(payload["reply_to_chat_id"], "oc_test_chat")
                    self.assertEqual(payload["question"], "现在状态怎么样")
                    self.assertTrue(payload["reply_lines"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_symbol_question_with_trade_advice_card_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_meeting_state_store().set(
                    "latest_execution_reconciliation",
                    {
                        "status": "ok",
                        "trade_date": trade_date,
                        "reconciled_at": datetime.now().isoformat(),
                        "positions": [
                            {
                                "symbol": "600519.SH",
                                "quantity": 500,
                                "cost_price": 100.0,
                                "last_price": 103.0,
                            }
                        ],
                    },
                )
                get_meeting_state_store().set(
                    "latest_tail_market_scan",
                    {
                        "trade_date": trade_date,
                        "scanned_at": datetime.now().isoformat(),
                        "items": [
                            {
                                "symbol": "600519.SH",
                                "name": "贵州茅台",
                                "exit_reason": "intraday_t_signal",
                                "review_tags": ["day_trading", "intraday"],
                            }
                        ],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 帮我分析一下贵州茅台\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["reason"], "natural_language_question_answered")
                    self.assertEqual(payload["topic"], "symbol_analysis")
                    self.assertTrue(any("建议级别=" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("结论:" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("风险提示:" in line for line in payload["reply_lines"]))
                    self.assertIn("reply_card", payload)
                    self.assertIn("markdown", payload["reply_card"])
                    self.assertIn("问题", payload["reply_card"]["markdown"])
                    self.assertIn("card", payload["reply_card"])
                    self.assertTrue(payload["reply_card"]["card"]["elements"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_opportunity_question_with_compact_board_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    trade_date = str(runtime_response.json()["generated_at"])[:10]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 现在有哪些机会票\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["topic"], "opportunity")
                    self.assertTrue(any("机会票概览:" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("优先机会:" in line or "备选观察:" in line for line in payload["reply_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_status_question_with_compact_status_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 现在状态怎么样\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["topic"], "status")
                    self.assertTrue(any("状态卡:" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("节奏:" in line or "执行概况:" in line for line in payload["reply_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_research_question_with_compact_research_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_research_state_store().set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": datetime.now().isoformat(),
                        "symbols": ["600519.SH", "002202.SZ"],
                        "news_count": 2,
                        "announcement_count": 1,
                        "event_titles": ["政策催化", "板块共振"],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 最近研究结论有什么\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["topic"], "research")
                    self.assertTrue(any("研究卡:" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("最近催化:" in line for line in payload["reply_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_risk_question_with_compact_risk_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adjust_payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={"instruction": "今天不买银行股", "apply": True},
                    ).json()
                    self.assertTrue(adjust_payload["ok"])
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["000001.SZ"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    trade_date = str(runtime_response.json()["generated_at"])[:10]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 当前有哪些风控阻断\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["topic"], "risk")
                    self.assertTrue(any("风控卡:" in line for line in payload["reply_lines"]))
                    self.assertTrue(any("首要阻断:" in line or "建议动作:" in line for line in payload["reply_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_applies_natural_language_adjustment_when_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 今天先不买白酒股\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["reason"], "natural_language_adjustment_applied")
                    self.assertEqual(payload["question"], "今天先不买白酒股")
                    self.assertTrue(payload["adjustment"]["ok"])
                    self.assertEqual(payload["adjustment"]["items"][0]["param_key"], "excluded_theme_keywords")
                    self.assertEqual(payload["adjustment"]["items"][0]["new_value"], "白酒")
                    self.assertTrue(payload["reply_lines"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_answers_group_question_with_literal_bot_name_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@南风·量化 今天最终推荐什么\"}",
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["reason"], "natural_language_question_answered")
                    self.assertEqual(payload["topic"], "discussion")
                    self.assertEqual(payload["question"], "今天最终推荐什么")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_ignores_unaddressed_group_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"现在状态怎么样\"}",
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertFalse(payload["processed"])
                    self.assertEqual(payload["reason"], "text_not_recognized_as_supervision_ack")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_config_exposes_callback_url_and_expected_event_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_PUBLIC_BASE_URL",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_PUBLIC_BASE_URL"] = "https://ashare.example.com"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.get("/system/feishu/events/config").json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["callback_url"], "https://ashare.example.com/system/feishu/events")
                    self.assertIn("im.message.receive_v1", payload["expected_event_types"])
                    self.assertIn("url.preview.get", payload["expected_event_types"])
                    self.assertTrue(payload["verification_token_configured"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_longconn_status_exposes_latest_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                state_path = Path(tmp_dir) / "feishu_longconn_state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "status": "connected",
                            "pid": os.getpid(),
                            "last_connected_at": datetime.now().isoformat(),
                            "last_event_at": datetime.now().isoformat(),
                            "last_heartbeat_at": datetime.now().isoformat(),
                            "control_plane_base_url": "http://127.0.0.1:8100",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with TestClient(create_app()) as client:
                    payload = client.get("/system/feishu/longconn/status").json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "connected")
                    self.assertEqual(payload["reported_status"], "connected")
                    self.assertEqual(payload["pid"], os.getpid())
                    self.assertTrue(payload["pid_alive"])
                    self.assertTrue(payload["is_fresh"])
                    self.assertEqual(payload["control_plane_base_url"], "http://127.0.0.1:8100")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_longconn_status_marks_stale_when_heartbeat_is_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                stale_time = (datetime.now() - timedelta(minutes=5)).isoformat()
                state_path = Path(tmp_dir) / "feishu_longconn_state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "status": "connected",
                            "pid": os.getpid(),
                            "last_connected_at": stale_time,
                            "last_event_at": stale_time,
                            "last_heartbeat_at": stale_time,
                            "control_plane_base_url": "http://127.0.0.1:8100",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with TestClient(create_app()) as client:
                    payload = client.get("/system/feishu/longconn/status").json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["reported_status"], "connected")
                    self.assertEqual(payload["status"], "stale")
                    self.assertFalse(payload["is_fresh"])
                    self.assertGreaterEqual(payload["freshness_age_seconds"], 300)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_operations_components_include_feishu_longconn_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                state_path = Path(tmp_dir) / "feishu_longconn_state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "status": "connected",
                            "pid": os.getpid(),
                            "last_connected_at": datetime.now().isoformat(),
                            "last_event_at": datetime.now().isoformat(),
                            "last_heartbeat_at": datetime.now().isoformat(),
                            "control_plane_base_url": "http://127.0.0.1:8100",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with TestClient(create_app()) as client:
                    payload = client.get("/system/operations/components").json()
                    feishu_component = next(item for item in payload["components"] if item["name"] == "feishu_longconn")
                    self.assertEqual(feishu_component["status"], "connected")
                    self.assertIn("reported=connected", feishu_component["detail"])
                    self.assertIn("pid_alive=True", feishu_component["detail"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_operations_components_mark_feishu_longconn_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                stale_time = (datetime.now() - timedelta(minutes=5)).isoformat()
                state_path = Path(tmp_dir) / "feishu_longconn_state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "status": "connected",
                            "pid": os.getpid(),
                            "last_connected_at": stale_time,
                            "last_event_at": stale_time,
                            "last_heartbeat_at": stale_time,
                            "control_plane_base_url": "http://127.0.0.1:8100",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with TestClient(create_app()) as client:
                    payload = client.get("/system/operations/components").json()
                    feishu_component = next(item for item in payload["components"] if item["name"] == "feishu_longconn")
                    self.assertEqual(feishu_component["status"], "stale")
                    self.assertIn("reported=connected", feishu_component["detail"])
                    self.assertIn("age=", feishu_component["detail"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_execution_precheck_blocks_new_symbol_when_max_hold_count_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                get_runtime_config_manager().update(max_hold_count=1)
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=100_000.0,
                )
                execution_adapter.positions["test-account"] = [
                    PositionSnapshot(
                        account_id="test-account",
                        symbol="600000.SH",
                        quantity=1000,
                        available=1000,
                        cost_price=10.0,
                        last_price=10.0,
                    )
                ]

                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)
                    cycle_payload = client.get(f"/system/discussions/cycles/{trade_date}").json()
                    self.assertIn(case_id, cycle_payload["execution_pool_case_ids"])

                    payload = client.get(
                        f"/system/discussions/execution-precheck?trade_date={trade_date}&account_id=test-account"
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    self.assertEqual(payload["current_equity_position_count"], 1)
                    self.assertEqual(payload["max_hold_count"], 1)
                    self.assertEqual(payload["blocked_count"], 1)
                    self.assertEqual(payload["items"][0]["primary_blocker"], "max_hold_count_reached")
                    self.assertIn("max_hold_count_reached", payload["items"][0]["blockers"])
                    self.assertEqual(payload["items"][0]["projected_equity_position_count"], 2)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_execution_dispatch_apply_blocks_when_windows_gateway_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []
                monitor_state_service = get_monitor_state_service()
                monitor_state_service.save_execution_bridge_health(
                    build_execution_bridge_health_ingress_payload(
                        {
                            "gateway_online": False,
                            "qmt_connected": False,
                            "overall_status": "down",
                            "windows_execution_gateway": {"status": "down", "reachable": False},
                            "qmt_vm": {"status": "down", "reachable": False},
                        },
                        trigger="unit_test",
                    )["health"],
                    trigger="unit_test",
                )

                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    )
                    self.assertEqual(runtime_response.status_code, 200)
                    runtime_payload = runtime_response.json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)
                    cycle_payload = client.get(f"/system/discussions/cycles/{trade_date}").json()
                    self.assertIn(case_id, cycle_payload["execution_pool_case_ids"])

                    payload = client.post(
                        "/system/discussions/execution-intents/dispatch",
                        json={"trade_date": trade_date, "account_id": "test-account", "apply": True},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    self.assertEqual(payload["degrade_reason"], "execution_bridge_unavailable")
                    self.assertEqual(payload["queued_count"], 0)
                    self.assertGreaterEqual(payload["blocked_count"], 1)
                    self.assertEqual(payload["submit_guard_detail"]["detail"], "gateway_unreachable")
                    self.assertTrue(all(item["reason"] == "execution_bridge_unavailable" for item in payload["receipts"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_natural_language_adjustment_supports_excluded_theme_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={
                            "instruction": "今天先不买银行股、白酒股",
                            "apply": False,
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "preview")
                    self.assertEqual(payload["matched_count"], 1)
                    self.assertEqual(payload["items"][0]["param_key"], "excluded_theme_keywords")
                    self.assertEqual(payload["items"][0]["new_value"], "银行,白酒")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_natural_language_adjustment_supports_multi_rule_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={
                            "instruction": "仓位调到3成，个股最多不能超过2万，暂时不买银行股",
                            "apply": True,
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["matched_count"], 3)
                    item_map = {item["param_key"]: item["new_value"] for item in payload["items"]}
                    self.assertEqual(item_map["equity_position_limit"], 0.3)
                    self.assertEqual(item_map["max_single_amount"], 20000.0)
                    self.assertEqual(item_map["excluded_theme_keywords"], "银行")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_natural_language_adjustment_preview_flag_does_not_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={
                            "instruction": "把测试仓位改到三成，暂时不买银行股，只做预览不要落地",
                            "preview": True,
                            "notify": False,
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertFalse(payload["applied"])
                    self.assertEqual(payload["status"], "preview")
                    self.assertEqual(payload["matched_count"], 2)

                    params_payload = client.get("/system/params/proposals").json()
                    self.assertIn("items", params_payload)
                    self.assertEqual(params_payload["count"], 0)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_execution_precheck_and_dispatch_alias_support_default_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    precheck_payload = client.get("/system/discussions/execution-precheck").json()
                    self.assertTrue(precheck_payload["ok"])
                    self.assertIn("trade_date", precheck_payload)
                    self.assertIn("account_id", precheck_payload)

                    dispatch_payload = client.get("/system/execution/dispatch/latest").json()
                    self.assertTrue(dispatch_payload["ok"])
                    self.assertEqual(dispatch_payload["status"], "not_found")
                    self.assertFalse(dispatch_payload["available"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_pipeline_filters_symbols_hit_by_excluded_theme_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    adjust_payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={
                            "instruction": "今天不买银行股",
                            "apply": True,
                        },
                    ).json()
                    self.assertTrue(adjust_payload["ok"])
                    self.assertTrue(adjust_payload["applied"])

                    payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600036.SH", "000001.SZ", "002594.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    ).json()
                    self.assertEqual(payload["case_count"], 1)
                    self.assertEqual([item["symbol"] for item in payload["top_picks"]], ["002594.SZ"])
                    selection_preferences = payload.get("selection_preferences") or {}
                    self.assertEqual(selection_preferences.get("excluded_theme_keywords"), ["银行"])
                    excluded_symbols = {item["symbol"] for item in selection_preferences.get("excluded_candidates", [])}
                    self.assertEqual(excluded_symbols, {"600036.SH", "000001.SZ"})
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_execution_precheck_blocks_symbol_hit_by_excluded_theme_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []

                with TestClient(create_app()) as client:
                    runtime_payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["000001.SZ"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    ).json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)

                    adjust_payload = client.post(
                        "/system/feishu/adjustments/natural-language",
                        json={
                            "instruction": "今天不买银行股",
                            "apply": True,
                        },
                    ).json()
                    self.assertTrue(adjust_payload["ok"])

                    payload = client.get(
                        f"/system/discussions/execution-precheck?trade_date={trade_date}&account_id=test-account"
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    self.assertEqual(payload["blocked_count"], 1)
                    self.assertEqual(payload["items"][0]["primary_blocker"], "excluded_by_selection_preferences")
                    self.assertIn("excluded_by_selection_preferences", payload["items"][0]["blockers"])
                    self.assertEqual(payload["items"][0]["selection_preference_match"]["keyword"], "银行")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_execution_precheck_blocks_when_positions_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=200_000.0,
                    cash=120_000.0,
                )
                execution_adapter.positions["test-account"] = []

                def _raise_positions_error(account_id: str) -> list[PositionSnapshot]:
                    raise RuntimeError("positions backend unavailable")

                execution_adapter.get_positions = _raise_positions_error  # type: ignore[method-assign]

                with TestClient(create_app()) as client:
                    runtime_payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["002594.SZ"],
                            "max_candidates": 1,
                            "account_id": "test-account",
                        },
                    ).json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    case_id = runtime_payload["case_ids"][0]
                    self._promote_case_to_selected(client, case_id)

                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start").status_code, 200)
                    self.assertEqual(client.post(f"/system/discussions/cycles/{trade_date}/refresh").status_code, 200)

                    payload = client.get(
                        f"/system/discussions/execution-precheck?trade_date={trade_date}&account_id=test-account"
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], "blocked")
                    self.assertEqual(payload["degrade_reason"], "positions_unavailable")
                    self.assertEqual(payload["positions_available"], False)
                    self.assertIn("positions backend unavailable", payload["positions_error"])
                    self.assertEqual(payload["items"][0]["primary_blocker"], "positions_unavailable")
                    self.assertIn("positions_unavailable", payload["items"][0]["blockers"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_supports_symbol_analysis_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "金风科技这支股票怎么样"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "symbol_analysis")
                    self.assertEqual(payload["symbol"], "002202.SZ")
                    self.assertTrue(any("金风科技" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_supports_symbol_analysis_for_jinlongyu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "帮我分析一下金龙羽这支股票"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "symbol_analysis")
                    self.assertEqual(payload["symbol"], "002882.SZ")
                    self.assertTrue(any("金龙羽" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_supports_symbol_analysis_for_company_name_with_punctuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "金风科技。"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "symbol_analysis")
                    self.assertEqual(payload["symbol"], "002202.SZ")
                    self.assertTrue(any("金风科技" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_supports_symbol_analysis_for_followup_style_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "金龙羽这支股票帮我分析了吗？"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "symbol_analysis")
                    self.assertEqual(payload["symbol"], "002882.SZ")
                    self.assertTrue(any("交易台临时体检" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_agent_activity_question_to_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                now = datetime.now().isoformat()
                get_research_state_store().set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": now,
                        "symbols": ["600519.SH"],
                        "news_count": 1,
                        "announcement_count": 0,
                        "event_titles": ["盘中催化确认"],
                    },
                )
                get_meeting_state_store().set(
                    "latest_execution_precheck",
                    {
                        "trade_date": trade_date,
                        "generated_at": now,
                        "status": "ready",
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "现在各 agent 活跃度怎么样", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "supervision")
                    self.assertIn("supervision", payload)
                    self.assertTrue(any("监督席位" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("正在值班" in line or "需处理" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("phase=" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_risk_and_research_questions_to_structured_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                now = datetime.now().isoformat()
                get_research_state_store().set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": now,
                        "symbols": ["600519.SH", "002202.SZ"],
                        "news_count": 2,
                        "announcement_count": 1,
                        "event_titles": ["政策催化", "板块共振"],
                    },
                )
                with TestClient(create_app()) as client:
                    risk_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "当前有哪些风控阻断", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(risk_payload["ok"])
                    self.assertEqual(risk_payload["topic"], "risk")
                    self.assertIn("execution_precheck", risk_payload)
                    self.assertTrue(any("风控席位" in line for line in risk_payload["answer_lines"]))

                    research_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "最近研究结论有什么", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(research_payload["ok"])
                    self.assertEqual(research_payload["topic"], "research")
                    self.assertIn("research_summary", research_payload)
                    self.assertTrue(any("研究席位" in line for line in research_payload["answer_lines"]))
                    self.assertTrue(any("政策催化" in line or "板块共振" in line for line in research_payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_week_ahead_questions_to_sandbox_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_research_state_store().set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": datetime.now().isoformat(),
                        "symbols": ["600519.SH", "002202.SZ"],
                        "news_count": 2,
                        "announcement_count": 1,
                        "event_titles": ["政策催化", "板块轮动增强"],
                    },
                )
                get_research_state_store().set(
                    "latest_nightly_sandbox",
                    {
                        "trade_date": trade_date,
                        "generated_at": datetime.now().isoformat(),
                        "tomorrow_priorities": ["600519.SH", "002202.SZ"],
                        "summary_lines": ["沙盘推演完成: 模拟 2 支，次日优先 2 支。", "次日优先标的: 600519.SH, 002202.SZ"],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={
                            "question": "根据昨日盘面，看下周有什么好的股票推荐，结合历史走向，板块轮动，以及当下热点和事件催化",
                            "trade_date": trade_date,
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "sandbox")
                    self.assertIn("nightly_sandbox", payload)
                    self.assertIn("600519.SH", "".join(payload["answer_lines"]))
                    self.assertTrue(any("沙盘推演" in line or "次日优先" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_events_route_uses_absolute_urls_for_card_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
                "ASHARE_PUBLIC_BASE_URL",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
            os.environ["ASHARE_PUBLIC_BASE_URL"] = "https://example.com/pach"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_research_state_store().set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": datetime.now().isoformat(),
                        "symbols": ["600519.SH"],
                        "news_count": 1,
                        "announcement_count": 0,
                        "event_titles": ["政策催化"],
                    },
                )
                get_research_state_store().set(
                    "latest_nightly_sandbox",
                    {
                        "trade_date": trade_date,
                        "generated_at": datetime.now().isoformat(),
                        "tomorrow_priorities": ["600519.SH"],
                        "summary_lines": ["沙盘推演完成: 模拟 1 支，次日优先 1 支。"],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/events",
                        json={
                            "token": "verify-token",
                            "schema": "2.0",
                            "header": {"event_type": "im.message.receive_v1"},
                            "event": {
                                "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                                "message": {
                                    "chat_id": "oc_test_chat",
                                    "chat_type": "group",
                                    "content": "{\"text\":\"@_user_1 沙盘推演有什么成果？\"}",
                                    "mentions": [
                                        {
                                            "key": "@_user_1",
                                            "name": "南风·量化",
                                            "mentioned_type": "bot",
                                        }
                                    ],
                                },
                            },
                        },
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["processed"])
                    self.assertEqual(payload["topic"], "sandbox")
                    action_block = next(
                        item for item in payload["reply_card"]["card"]["elements"] if item.get("tag") == "action"
                    )
                    urls = [item.get("url") for item in action_block.get("actions", [])]
                    self.assertTrue(urls)
                    self.assertTrue(all(str(url).startswith("https://example.com/pach/") for url in urls))
                    self.assertTrue(any("/system/nightly-sandbox/latest" in str(url) for url in urls))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_params_and_scores_to_trading_desk_style_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                with TestClient(create_app()) as client:
                    proposal_payload = client.post(
                        "/system/params/proposals",
                        json={
                            "param_key": "focus_pool_capacity",
                            "new_value": 15,
                            "proposed_by": "ashare-strategy",
                            "structured_by": "ashare-strategy",
                            "reason": "提高观察池容量",
                        },
                    ).json()
                    self.assertTrue(proposal_payload["ok"])

                    params_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "最近参数提案有哪些", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(params_payload["ok"])
                    self.assertEqual(params_payload["topic"], "params")
                    self.assertTrue(any("参数席位" in line for line in params_payload["answer_lines"]))

                    scores_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "各 agent 评分多少", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(scores_payload["ok"])
                    self.assertEqual(scores_payload["topic"], "scores")
                    self.assertTrue(any("评分席位" in line for line in scores_payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_status_question_to_trading_desk_style_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "现在状态怎样"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "status")
                    self.assertTrue(any("交易台总览" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_discussion_and_execution_to_trading_desk_style_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=100000.0,
                    cash=100000.0,
                )
                execution_adapter.positions["test-account"] = []
                with TestClient(create_app()) as client:
                    runtime_payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ"],
                            "max_candidates": 2,
                            "account_id": "test-account",
                        },
                    ).json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )
                    discussion_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "今天推荐什么", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(discussion_payload["ok"])
                    self.assertEqual(discussion_payload["topic"], "discussion")
                    self.assertTrue(any("讨论席位" in line for line in discussion_payload["answer_lines"]))
                    self.assertTrue(
                        any(
                            "不等于最终推荐" in line
                            or "当前候选" in line
                            or "还没有形成可执行结论" in line
                            for line in discussion_payload["answer_lines"]
                        )
                    )

                    execution_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "有没有执行回执", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(execution_payload["ok"])
                    self.assertEqual(execution_payload["topic"], "execution")
                    self.assertTrue(any("执行席位" in line for line in execution_payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_cycle_question_to_structured_cycle_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                execution_adapter = get_execution_adapter()
                execution_adapter.balances["test-account"] = BalanceSnapshot(
                    account_id="test-account",
                    total_asset=100000.0,
                    cash=100000.0,
                )
                execution_adapter.positions["test-account"] = []
                with TestClient(create_app()) as client:
                    runtime_payload = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    ).json()
                    trade_date = str(runtime_payload["generated_at"])[:10]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )

                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "CYCLE是什么？", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "cycle")
                    self.assertIn("cycle", payload)
                    self.assertTrue(any("主线讨论周期" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("trade_date=" in line and "discussion_state=" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_opportunity_and_replacement_questions_to_client_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    trade_date = str(runtime_response.json()["generated_at"])[:10]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )

                    opportunity_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "现在有哪些机会票", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(opportunity_payload["ok"])
                    self.assertEqual(opportunity_payload["topic"], "opportunity")
                    self.assertIn("client_brief", opportunity_payload)
                    self.assertTrue(any("机会席位" in line or "优先机会" in line or "备选观察" in line for line in opportunity_payload["answer_lines"]))

                    replacement_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "有没有更好的替换建议", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(replacement_payload["ok"])
                    self.assertEqual(replacement_payload["topic"], "replacement")
                    self.assertIn("client_brief", replacement_payload)
                    self.assertTrue(any("换仓席位" in line or "替换候选" in line for line in replacement_payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_position_question_to_precheck_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ"],
                            "max_candidates": 2,
                            "account_id": "test-account",
                        },
                    )
                    trade_date = str(runtime_response.json()["generated_at"])[:10]

                    position_payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "当前仓位为什么这样配", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(position_payload["ok"])
                    self.assertEqual(position_payload["topic"], "position")
                    self.assertIn("execution_precheck", position_payload)
                    self.assertTrue(any("仓位席位" in line for line in position_payload["answer_lines"]))
                    self.assertTrue(any("剩余预算" in line or "持仓/占位" in line for line in position_payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_holding_review_to_account_and_reconciliation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_meeting_state_store().set(
                    "latest_execution_reconciliation",
                    {
                        "status": "ok",
                        "trade_date": trade_date,
                        "reconciled_at": datetime.now().isoformat(),
                        "positions": [
                            {
                                "symbol": "600519.SH",
                                "quantity": 500,
                                "cost_price": 100.0,
                                "last_price": 103.0,
                            }
                        ],
                        "summary_lines": ["执行对账完成: positions=1。"],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "当前持仓复核一下", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "holding_review")
                    self.assertIn("account_state", payload)
                    self.assertIn("execution_reconciliation", payload)
                    self.assertTrue(any("持仓复核席位" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("持仓明细" in line or "600519.SH" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_day_trading_question_to_tail_market_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_meeting_state_store().set(
                    "latest_tail_market_scan",
                    {
                        "trade_date": trade_date,
                        "scanned_at": datetime.now().isoformat(),
                        "summary_lines": ["尾盘卖出扫描完成: positions=1 signals=1。"],
                        "items": [
                            {
                                "symbol": "600519.SH",
                                "name": "贵州茅台",
                                "exit_reason": "intraday_t_signal",
                                "review_tags": ["day_trading", "intraday"],
                            }
                        ],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "今天有没有做T机会", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "day_trading")
                    self.assertIn("tail_market_review", payload)
                    self.assertTrue(any("日内T席位" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("600519.SH" in line or "可复核信号" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_symbol_holding_review_to_symbol_focused_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_meeting_state_store().set(
                    "latest_execution_reconciliation",
                    {
                        "status": "ok",
                        "trade_date": trade_date,
                        "reconciled_at": datetime.now().isoformat(),
                        "positions": [
                            {
                                "symbol": "600519.SH",
                                "quantity": 500,
                                "cost_price": 100.0,
                                "last_price": 103.0,
                            }
                        ],
                        "summary_lines": ["执行对账完成: positions=1。"],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "贵州茅台这只持仓怎么样", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "position")
                    self.assertEqual(payload["symbol"], "600519.SH")
                    self.assertTrue(any("持仓状态" in line for line in payload["answer_lines"]))
                    self.assertIn("trade_advice", payload)
                    self.assertTrue(any("交易台建议" in line for line in payload["answer_lines"]))
                    self.assertIn("recommendation_level", payload["trade_advice"])
                    self.assertIn("trigger_conditions", payload["trade_advice"])
                    self.assertIn("risk_notes", payload["trade_advice"])
                    self.assertTrue(any("风险提示：" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_symbol_day_trading_question_to_symbol_focused_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_meeting_state_store().set(
                    "latest_tail_market_scan",
                    {
                        "trade_date": trade_date,
                        "scanned_at": datetime.now().isoformat(),
                        "summary_lines": ["尾盘卖出扫描完成: positions=1 signals=1。"],
                        "items": [
                            {
                                "symbol": "600519.SH",
                                "name": "贵州茅台",
                                "exit_reason": "intraday_t_signal",
                                "review_tags": ["day_trading", "intraday"],
                            }
                        ],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "贵州茅台今天有做T机会吗", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "day_trading")
                    self.assertEqual(payload["symbol"], "600519.SH")
                    self.assertTrue(any("信号:" in line or "日内 T" in line for line in payload["answer_lines"]))
                    self.assertEqual(payload["trade_advice"]["stance"], "t_signal_active")
                    self.assertTrue(any("下一步：" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("触发条件：" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_routes_symbol_replacement_question_to_symbol_focused_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    runtime_response = client.post(
                        "/runtime/jobs/pipeline",
                        json={
                            "symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                            "max_candidates": 3,
                            "account_id": "test-account",
                        },
                    )
                    trade_date = str(runtime_response.json()["generated_at"])[:10]
                    self.assertEqual(
                        client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date}).status_code,
                        200,
                    )

                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "贵州茅台有没有更好的替换建议", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "replacement")
                    self.assertEqual(payload["symbol"], "600519.SH")
                    self.assertIn("execution_precheck", payload)
                    self.assertTrue(any("执行视角" in line or "替换时应先与这些观察候选比较" in line for line in payload["answer_lines"]))
                    self.assertIn("trade_advice", payload)
                    self.assertTrue(any("交易台建议" in line for line in payload["answer_lines"]))
                    self.assertIn("recommendation_level", payload["trade_advice"])
                    self.assertTrue(any("交易台建议级别" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_symbol_analysis_combines_research_position_and_execution_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                trade_date = datetime.now().date().isoformat()
                get_research_state_store().set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": datetime.now().isoformat(),
                        "symbols": ["600519.SH"],
                        "news_count": 1,
                        "announcement_count": 0,
                        "event_titles": ["白酒板块修复"],
                    },
                )
                get_meeting_state_store().set(
                    "latest_execution_reconciliation",
                    {
                        "status": "ok",
                        "trade_date": trade_date,
                        "reconciled_at": datetime.now().isoformat(),
                        "positions": [
                            {
                                "symbol": "600519.SH",
                                "quantity": 500,
                                "cost_price": 100.0,
                                "last_price": 103.0,
                            }
                        ],
                    },
                )
                get_meeting_state_store().set(
                    "latest_tail_market_scan",
                    {
                        "trade_date": trade_date,
                        "scanned_at": datetime.now().isoformat(),
                        "items": [
                            {
                                "symbol": "600519.SH",
                                "name": "贵州茅台",
                                "exit_reason": "intraday_t_signal",
                                "review_tags": ["day_trading", "intraday"],
                            }
                        ],
                    },
                )
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "帮我分析一下贵州茅台", "trade_date": trade_date},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "symbol_analysis")
                    self.assertEqual(payload["symbol"], "600519.SH")
                    self.assertEqual(payload["analysis_mode"], "ad_hoc")
                    self.assertTrue(any("持仓状态" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("日内/尾盘信号" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("研究面最近在跟踪" in line or "最近相关标题" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("策略视角：" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("风控视角：" in line for line in payload["answer_lines"]))
                    self.assertIn("trade_advice", payload)
                    self.assertTrue(any("交易台结论" in line for line in payload["answer_lines"]))
                    self.assertIn("recommendation_level", payload["trade_advice"])
                    self.assertTrue(any("触发条件：" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_help_mentions_symbol_level_capability_instead_of_fixed_topics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "你能做什么"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "help")
                    self.assertTrue(any("固定五类主题问答器" in line or "点名一只股票" in line for line in payload["answer_lines"]))
                    self.assertTrue(any("真实工具和协作链" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_feishu_ask_supports_casual_chat_without_falling_back_to_help_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {key: os.environ.get(key) for key in [
                "ASHARE_STORAGE_ROOT",
                "ASHARE_LOGS_DIR",
                "ASHARE_EXECUTION_MODE",
                "ASHARE_MARKET_MODE",
                "ASHARE_RUN_MODE",
                "ASHARE_EXECUTION_PLANE",
                "ASHARE_ACCOUNT_ID",
            ]}
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/feishu/ask",
                        json={"question": "你好"},
                    ).json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["topic"], "casual_chat")
                    self.assertTrue(any("我在" in line for line in payload["answer_lines"]))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_discussion_cycle_service_can_write_compose_result_into_strategy_opinions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = "2026-04-17"
                candidate_case_service = get_candidate_case_service()
                discussion_cycle_service = get_discussion_cycle_service()
                candidate_case_service.sync_from_runtime_report(
                    {
                        "job_id": "runtime-test-001",
                        "generated_at": f"{trade_date}T09:35:00",
                        "top_picks": [
                            {
                                "symbol": "600001.SH",
                                "name": "测试一号",
                                "rank": 1,
                                "selection_score": 9.2,
                                "action": "BUY",
                                "summary": "量价共振，板块热度抬升",
                                "resolved_sector": "机器人",
                            },
                            {
                                "symbol": "000002.SZ",
                                "name": "测试二号",
                                "rank": 2,
                                "selection_score": 7.1,
                                "action": "HOLD",
                                "summary": "趋势延续但仍需确认承接",
                                "resolved_sector": "AI应用",
                            },
                        ],
                    },
                    focus_pool_capacity=10,
                    execution_pool_capacity=3,
                )
                discussion_cycle_service.bootstrap_cycle(trade_date)
                discussion_cycle_service.start_round(trade_date, 1)

                response = discussion_cycle_service.write_compose_strategy_opinions(
                    {
                        "intent": {
                            "objective": "盘中寻找强势机会",
                            "market_hypothesis": "热点切回机器人与 AI 应用",
                        },
                        "market_summary": {"market_regime": "trend"},
                        "explanations": {
                            "weight_summary": ["momentum 权重 0.4", "sector_heat 权重 0.3"],
                            "market_driver_summary": ["正在追踪热点板块: 机器人,AI应用"],
                        },
                        "candidates": [
                            {
                                "symbol": "600001.SH",
                                "name": "测试一号",
                                "rank": 1,
                                "composite_score": 10.3,
                                "evidence": ["量价共振，板块热度抬升", "命中热点板块: 机器人"],
                                "counter_evidence": ["需确认封单质量是否持续"],
                                "market_drivers": {"tags": ["hot_sector_hit", "momentum_boost"]},
                                "risk_flags": ["动作=BUY"],
                            },
                            {
                                "symbol": "000002.SZ",
                                "name": "测试二号",
                                "rank": 2,
                                "composite_score": 6.4,
                                "evidence": ["趋势延续但仍需确认承接"],
                                "counter_evidence": ["午后若量能衰减需降级观察"],
                                "market_drivers": {"tags": ["hot_sector_hit"]},
                                "risk_flags": ["动作=HOLD"],
                            },
                        ],
                        "proposal_packet": {
                            "selected_symbols": ["600001.SH"],
                            "watchlist_symbols": ["000002.SZ"],
                            "discussion_focus": ["这批候选是否贴合当前市场假设", "是否需要风控进一步挑反证"],
                        },
                        "evaluation_trace": {"trace_id": "compose-trace-001"},
                        "runtime_job": {"pipeline_job_id": "runtime-test-001"},
                    },
                    trade_date=trade_date,
                )

                self.assertTrue(response["ok"])
                self.assertEqual(response["written_count"], 2)
                self.assertEqual(response["trace_id"], "compose-trace-001")
                case_one = candidate_case_service.get_case("case-20260417-600001-SH")
                case_two = candidate_case_service.get_case("case-20260417-000002-SZ")
                self.assertIsNotNone(case_one)
                self.assertIsNotNone(case_two)
                latest_one = [item for item in list(case_one.opinions) if item.agent_id == "ashare-strategy"][-1]
                latest_two = [item for item in list(case_two.opinions) if item.agent_id == "ashare-strategy"][-1]
                self.assertEqual(latest_one.stance, "support")
                self.assertEqual(latest_two.stance, "watch")
                self.assertTrue(any("compose_trace:compose-trace-001" == ref for ref in latest_one.evidence_refs))
                self.assertTrue(any("继续质询" in gap for gap in latest_one.evidence_gaps))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_system_api_compose_writeback_uses_latest_compose_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = "2026-04-17"
                candidate_case_service = get_candidate_case_service()
                discussion_cycle_service = get_discussion_cycle_service()
                runtime_state_store = get_runtime_state_store()
                candidate_case_service.sync_from_runtime_report(
                    {
                        "job_id": "runtime-test-002",
                        "generated_at": f"{trade_date}T10:05:00",
                        "top_picks": [
                            {
                                "symbol": "300001.SZ",
                                "name": "候选甲",
                                "rank": 1,
                                "selection_score": 8.8,
                                "action": "BUY",
                                "summary": "盘中异动放量突破",
                                "resolved_sector": "半导体",
                            }
                        ],
                    },
                    focus_pool_capacity=10,
                    execution_pool_capacity=3,
                )
                discussion_cycle_service.bootstrap_cycle(trade_date)
                discussion_cycle_service.start_round(trade_date, 1)
                runtime_state_store.set(
                    "compose_evaluations",
                    [
                        {
                            "trace_id": "compose-trace-002",
                            "generated_at": f"{trade_date}T10:06:00",
                            "intent": {
                                "objective": "捕捉盘中热点",
                                "market_hypothesis": "半导体出现资金回流",
                            },
                            "market_summary": {"market_regime": "rotation"},
                            "explanations": {
                                "weight_summary": ["flow 权重 0.5"],
                                "market_driver_summary": ["正在追踪热点板块: 半导体"],
                            },
                            "candidates": [
                                {
                                    "symbol": "300001.SZ",
                                    "name": "候选甲",
                                    "rank": 1,
                                    "composite_score": 9.6,
                                    "evidence": ["盘中异动放量突破", "命中热点板块: 半导体"],
                                    "counter_evidence": ["需确认午后持续性"],
                                    "market_drivers": {"tags": ["hot_sector_hit", "event_catalyst"]},
                                    "risk_flags": ["动作=BUY"],
                                }
                            ],
                            "proposal_packet": {
                                "selected_symbols": ["300001.SZ"],
                                "watchlist_symbols": [],
                                "discussion_focus": ["是否优于现有持仓"],
                            },
                            "runtime_job": {"pipeline_job_id": "runtime-test-002"},
                            "adoption": {"trade_date": trade_date},
                        }
                    ],
                )

                with TestClient(create_app()) as client:
                    response = client.post(
                        "/system/discussions/opinions/compose-writeback",
                        json={"trade_date": trade_date},
                    )
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["written_count"], 1)
                    self.assertEqual(payload["trace_id"], "compose-trace-002")
                    self.assertTrue(payload["discussion_advance"]["available"])
                    self.assertIn("ashare-strategy", payload["discussion_advance"]["completed_agents"])

                case = candidate_case_service.get_case("case-20260417-300001-SZ")
                self.assertIsNotNone(case)
                latest = [item for item in list(case.opinions) if item.agent_id == "ashare-strategy"][-1]
                self.assertEqual(latest.round, 1)
                self.assertEqual(latest.stance, "support")
                self.assertTrue(any(ref == "runtime_job:runtime-test-002" for ref in latest.evidence_refs))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_agent_runtime_work_packets_exposes_strategy_task_and_compose_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = "2026-04-17"
                candidate_case_service = get_candidate_case_service()
                discussion_cycle_service = get_discussion_cycle_service()
                candidate_case_service.sync_from_runtime_report(
                    {
                        "job_id": "runtime-packet-001",
                        "generated_at": f"{trade_date}T10:15:00",
                        "top_picks": [
                            {
                                "symbol": "600001.SH",
                                "name": "测试甲",
                                "rank": 1,
                                "selection_score": 9.0,
                                "action": "BUY",
                                "summary": "趋势与量能共振",
                                "resolved_sector": "机器人",
                            },
                            {
                                "symbol": "000002.SZ",
                                "name": "测试乙",
                                "rank": 2,
                                "selection_score": 8.1,
                                "action": "BUY",
                                "summary": "板块分歧后回流",
                                "resolved_sector": "AI应用",
                            },
                        ],
                    },
                    focus_pool_capacity=10,
                    execution_pool_capacity=3,
                )
                discussion_cycle_service.bootstrap_cycle(trade_date)
                discussion_cycle_service.start_round(trade_date, 1)

                with TestClient(create_app()) as client:
                    payload = client.get(
                        f"/system/agents/runtime-work-packets?trade_date={trade_date}&agent_id=ashare-strategy"
                    ).json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["count"], 1)
                packet = payload["packets"][0]
                self.assertEqual(packet["agent_id"], "ashare-strategy")
                self.assertTrue(packet["compose_brief_hint"]["available"])
                self.assertEqual(packet["compose_brief_hint"]["endpoint"], "/runtime/jobs/compose-from-brief")
                self.assertTrue(packet["compose_brief_hint"]["sample_payload"]["symbol_pool"])
                self.assertTrue(packet["compose_brief_hint"]["custom_compose_enabled"])
                self.assertEqual(packet["compose_brief_hint"]["entry_mode"], "custom_first")
                self.assertTrue(packet["compose_brief_hint"]["reference_templates"])
                self.assertEqual(packet["compose_brief_hint"]["selected_profile_is_reference_only"], True)
                self.assertIn("next_action", packet["compose_brief_hint"]["orchestration_trace_contract"]["required_fields"])
                self.assertEqual(packet["compose_brief_hint"]["custom_payload_template"]["playbooks"], [])
                self.assertIn("/runtime/jobs/compose", packet["write_endpoints"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_opportunity_tickets_enter_discussion_with_agent_proposed_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = "2026-04-17"
                candidate_case_service = get_candidate_case_service()
                discussion_cycle_service = get_discussion_cycle_service()

                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/system/discussions/opportunity-tickets",
                        json={
                            "trade_date": trade_date,
                            "items": [
                                {
                                    "symbol": "301001.SZ",
                                    "name": "自提样本",
                                    "source": "agent_proposed",
                                    "source_role": "ashare-research",
                                    "market_logic": "事件催化与板块扩散共振，值得进入正式讨论",
                                    "core_evidence": ["午间公告强化预期", "同题材前排出现扩散"],
                                    "risk_points": ["流动性仍需复核", "尾盘承接尚未验证"],
                                    "why_now": "当前是第一批结构化自提票，需要尽快进入讨论链",
                                    "recommended_action": "promote_to_focus",
                                    "evidence_refs": ["event-context", "runtime-scout"],
                                }
                            ],
                        },
                    ).json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["count"], 1)
                self.assertEqual(payload["ingress_summary"]["entered_discussion_count"], 1)
                self.assertTrue(payload["ingress_summary"]["non_default_entry"])
                self.assertEqual(payload["ingress_summary"]["source_counts"]["agent_proposed"], 1)
                item = payload["items"][0]
                self.assertEqual(item["runtime_snapshot"]["source"], "agent_proposed")
                self.assertIn("agent_proposed", item["runtime_snapshot"]["source_tags"])
                self.assertEqual(
                    item["runtime_snapshot"]["source_detail"]["latest_agent_proposal"]["market_logic"],
                    "事件催化与板块扩散共振，值得进入正式讨论",
                )
                case_id = item["case_id"]
                case = candidate_case_service.get_case(case_id)
                self.assertIsNotNone(case)
                self.assertEqual(case.runtime_snapshot.source, "agent_proposed")
                self.assertIn("proposal_market_logic", case.bull_case)
                pool_snapshot = candidate_case_service.build_pool_snapshot(trade_date)
                self.assertIn("base_sample_pool", pool_snapshot)
                self.assertIn("deep_validation_pool", pool_snapshot)
                self.assertIn("execution_candidate_pool", pool_snapshot)
                self.assertEqual(pool_snapshot["counts"]["base_sample_pool"], pool_snapshot["counts"]["candidate_pool"])
                self.assertEqual(pool_snapshot["source_counts"]["agent_proposed"], 1)
                cycle = discussion_cycle_service.get_cycle(trade_date)
                self.assertIsNotNone(cycle)
                self.assertIn(case_id, list(cycle.base_pool_case_ids))
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_news_catalyst_job_consumes_event_context_and_syncs_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                layout = ensure_storage_layout(Path(tmp_dir))
                (layout.serving_root / "latest_event_context.json").write_text(
                    json.dumps(
                        {
                            "trade_date": "2026-04-18",
                            "generated_at": "2026-04-18T14:36:00",
                            "highlights": [
                                {
                                    "symbol": "600519.SH",
                                    "title": "正面公告催化",
                                    "sentiment": "positive",
                                    "severity": "high",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/runtime/jobs/news-catalyst",
                        json={
                            "account_id": "sim-001",
                            "max_candidates": 2,
                        },
                    ).json()
                self.assertEqual(payload["source"], "news_catalyst_scan")
                self.assertEqual(payload["source_context"]["mode"], "event_context")
                self.assertGreaterEqual(payload["source_context"]["source_symbol_count"], 1)
                self.assertTrue(any(item["symbol"] == "600519.SH" for item in payload["top_picks"]))
                self.assertTrue(
                    any(
                        case.runtime_snapshot.source == "news_catalyst_scan"
                        for case in candidate_case_service.list_cases(limit=10)
                    )
                )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_runtime_tail_ambush_job_consumes_runtime_context_and_syncs_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                layout = ensure_storage_layout(Path(tmp_dir))
                (layout.serving_root / "latest_runtime_context.json").write_text(
                    json.dumps(
                        {
                            "trade_date": "2026-04-18",
                            "generated_at": "2026-04-18T14:46:00",
                            "job_id": "runtime-tail-context-001",
                            "selected_symbols": ["000001.SZ", "600519.SH"],
                            "top_picks": [
                                {"symbol": "000001.SZ"},
                                {"symbol": "600519.SH"},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                candidate_case_service = get_candidate_case_service()
                with TestClient(create_app()) as client:
                    payload = client.post(
                        "/runtime/jobs/tail-ambush",
                        json={
                            "account_id": "sim-001",
                            "max_candidates": 2,
                        },
                    ).json()
                self.assertEqual(payload["source"], "tail_ambush_scan")
                self.assertEqual(payload["source_context"]["mode"], "runtime_context")
                self.assertGreaterEqual(payload["source_context"]["source_symbol_count"], 2)
                self.assertTrue(any(item["symbol"] == "000001.SZ" for item in payload["top_picks"]))
                self.assertTrue(
                    any(
                        case.runtime_snapshot.source == "tail_ambush_scan"
                        for case in candidate_case_service.list_cases(limit=10)
                    )
                )
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_system_api_research_risk_audit_writebacks_progress_mainline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
            reset_container()

            try:
                trade_date = "2026-04-17"
                candidate_case_service = get_candidate_case_service()
                discussion_cycle_service = get_discussion_cycle_service()
                research_state_store = get_research_state_store()
                meeting_state_store = get_meeting_state_store()
                candidate_case_service.sync_from_runtime_report(
                    {
                        "job_id": "runtime-auto-001",
                        "generated_at": f"{trade_date}T10:20:00",
                        "top_picks": [
                            {
                                "symbol": "300001.SZ",
                                "name": "候选甲",
                                "rank": 1,
                                "selection_score": 8.8,
                                "action": "BUY",
                                "summary": "盘中异动放量突破",
                                "resolved_sector": "半导体",
                            }
                        ],
                    },
                    focus_pool_capacity=10,
                    execution_pool_capacity=3,
                )
                discussion_cycle_service.bootstrap_cycle(trade_date)
                discussion_cycle_service.start_round(trade_date, 1)
                research_state_store.set(
                    "summary",
                    {
                        "trade_date": trade_date,
                        "updated_at": f"{trade_date}T10:21:00",
                        "symbols": ["300001.SZ"],
                        "event_titles": ["半导体板块午前热度回升"],
                    },
                )
                meeting_state_store.set(
                    "latest_review_board",
                    {
                        "trade_date": trade_date,
                        "generated_at": f"{trade_date}T10:25:00",
                        "summary_lines": ["当前无新增治理阻断，继续按主线推进。"],
                    },
                )

                with TestClient(create_app()) as client:
                    case_id = "case-20260417-300001-SZ"
                    strategy_response = client.post(
                        f"/system/cases/{case_id}/opinions",
                        json={
                            "round": 1,
                            "agent_id": "ashare-strategy",
                            "stance": "support",
                            "confidence": "high",
                            "reasons": ["compose 已确认该票仍在当前主线"],
                            "evidence_refs": ["compose_trace:test-seed"],
                            "thesis": "策略侧支持继续进入风控与审计",
                        },
                    )
                    self.assertEqual(strategy_response.status_code, 200)
                    self.assertTrue(strategy_response.json()["ok"])

                    research_response = client.post(
                        "/system/discussions/opinions/research-writeback",
                        json={"trade_date": trade_date},
                    )
                    self.assertEqual(research_response.status_code, 200)
                    research_payload = research_response.json()
                    self.assertTrue(research_payload["ok"])
                    self.assertEqual(research_payload["written_count"], 1)
                    self.assertIn("ashare-research", research_payload["discussion_advance"]["completed_agents"])

                    risk_response = client.post(
                        "/system/discussions/opinions/risk-writeback",
                        json={"trade_date": trade_date},
                    )
                    self.assertEqual(risk_response.status_code, 200)
                    risk_payload = risk_response.json()
                    self.assertTrue(risk_payload["ok"])
                    self.assertEqual(risk_payload["written_count"], 1)
                    self.assertIn("ashare-risk", risk_payload["discussion_advance"]["completed_agents"])

                    audit_response = client.post(
                        "/system/discussions/opinions/audit-writeback",
                        json={"trade_date": trade_date},
                    )
                    self.assertEqual(audit_response.status_code, 200)
                    audit_payload = audit_response.json()
                    self.assertTrue(audit_payload["ok"])
                    self.assertEqual(audit_payload["written_count"], 1)
                    self.assertIn("ashare-audit", audit_payload["discussion_advance"]["completed_agents"])

                case = candidate_case_service.get_case("case-20260417-300001-SZ")
                self.assertIsNotNone(case)
                latest_by_agent = {}
                for opinion in list(case.opinions):
                    latest_by_agent[opinion.agent_id] = opinion
                self.assertIn("ashare-research", latest_by_agent)
                self.assertIn("ashare-risk", latest_by_agent)
                self.assertIn("ashare-audit", latest_by_agent)
                self.assertNotEqual(case.risk_gate, "pending")
                self.assertIn(case.audit_gate, {"clear", "hold"})
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()


if __name__ == "__main__":
    unittest.main()

    def test_dashboard_endpoint(self):
        """验证仪表盘端点返回 HTML"""
        from fastapi.testclient import TestClient
        from ashare_system.app import create_app
        with TestClient(create_app()) as client:
            response = client.get("/dashboard")
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn("ASHARE SYSTEM V2", response.text)
            self.assertIn("运行模式", response.text)

    def test_dashboard_actions_not_allowed_without_form(self):
        """验证非表单提交报错"""
        from fastapi.testclient import TestClient
        from ashare_system.app import create_app
        with TestClient(create_app()) as client:
            response = client.post("/dashboard/actions/mode")
            self.assertEqual(response.status_code, 422) # Unprocessable Entity due to missing Form fields
