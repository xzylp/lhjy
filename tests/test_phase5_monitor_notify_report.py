"""Phase 5 监控 + 通信 + 报告测试"""

import pytest
from pathlib import Path
from datetime import datetime

from ashare_system.contracts import QuoteSnapshot, MarketProfile
from ashare_system.data.archive import DataArchiveStore
from ashare_system.infra.audit_store import StateStore
from ashare_system.monitor.alert_engine import AlertEngine, AlertEvent
from ashare_system.monitor.persistence import MonitorStateService
from ashare_system.monitor.stock_pool import StockPoolManager
from ashare_system.monitor.limit_analyzer import LimitAnalyzer
from ashare_system.monitor.dragon_tiger import DragonTigerAnalyzer, DragonTigerRecord
from ashare_system.apps.system_api import _build_execution_next_action_lines, _build_execution_next_actions
from ashare_system.notify.feishu import FeishuNotifier
from ashare_system.notify.dispatcher import MessageDispatcher
from ashare_system.notify.discussion_finalize import DiscussionFinalizeNotifier
from ashare_system.notify.governance_adjustment import GovernanceAdjustmentNotifier
from ashare_system.notify.live_execution_alerts import LiveExecutionAlertNotifier
from ashare_system.notify.monitor_changes import MonitorChangeNotifier
from ashare_system.notify.templates import (
    trade_executed_template,
    execution_order_event_template,
    daily_report_template,
    risk_alert_template,
    execution_dispatch_notification_template,
    live_execution_alert_template,
    monitor_change_summary_template,
    discussion_reply_pack_template,
    execution_precheck_summary_lines,
    governance_adjustment_template,
)
from ashare_system.report.generator import ReportGenerator
from ashare_system.report.daily import DailyReporter, DailyReportData
from ashare_system.backtest.metrics import BacktestMetrics
from ashare_system.runtime_config import RuntimeConfigManager


class TestAlertEngine:
    def test_price_spike_alert(self):
        engine = AlertEngine()
        engine._prev_prices["600519.SH"] = 100.0
        snap = QuoteSnapshot(symbol="600519.SH", last_price=106.0, bid_price=105.9, ask_price=106.1, volume=100_000, pre_close=100.0)
        alerts = engine.check(snap)
        assert any(a.alert_type == "price_spike" for a in alerts)

    def test_limit_up_alert(self):
        engine = AlertEngine()
        engine._prev_prices["000001.SZ"] = 10.0
        snap = QuoteSnapshot(symbol="000001.SZ", last_price=11.0, bid_price=10.99, ask_price=11.0, volume=500_000, pre_close=10.0)
        alerts = engine.check(snap)
        assert any(a.alert_type == "limit_up" for a in alerts)

    def test_no_alert_normal(self):
        engine = AlertEngine()
        engine._prev_prices["test"] = 10.0
        snap = QuoteSnapshot(symbol="test", last_price=10.1, bid_price=10.09, ask_price=10.11, volume=100_000, pre_close=10.0)
        alerts = engine.check(snap)
        price_alerts = [a for a in alerts if a.alert_type == "price_spike"]
        assert len(price_alerts) == 0

    def test_batch_check(self):
        engine = AlertEngine()
        snaps = [
            QuoteSnapshot(symbol=f"stock_{i}", last_price=10.0, bid_price=9.99, ask_price=10.01, volume=100_000, pre_close=10.0)
            for i in range(5)
        ]
        alerts = engine.check_batch(snaps)
        assert isinstance(alerts, list)


class TestStockPool:
    def test_update_and_get(self):
        mgr = StockPoolManager()
        symbols = ["600519.SH", "000001.SZ", "300750.SZ"]
        pool = mgr.update(symbols)
        assert pool.symbols == symbols
        assert mgr.get() is not None

    def test_get_top_n_with_scores(self):
        mgr = StockPoolManager()
        symbols = ["A", "B", "C", "D"]
        scores = {"A": 90, "B": 70, "C": 85, "D": 60}
        mgr.update(symbols, scores)
        top2 = mgr.get_top_n(2)
        assert top2[0] == "A"
        assert top2[1] == "C"

    def test_empty_pool(self):
        mgr = StockPoolManager()
        assert mgr.get() is None
        assert mgr.get_top_n(5) == []

    def test_pool_tracks_symbol_names(self):
        mgr = StockPoolManager()
        pool = mgr.update(["600519.SH"], {"600519.SH": 90}, names={"600519.SH": "贵州茅台"})
        assert pool.names["600519.SH"] == "贵州茅台"


class TestMonitorStateService:
    def test_save_heartbeat_if_due_and_record_events(self, tmp_path):
        config_mgr = RuntimeConfigManager(tmp_path / "runtime_config.json")
        config_mgr.update(
            **{
                "watch.heartbeat_save_seconds": 600,
                "watch.auction_heartbeat_save_seconds": 300,
                "watch.event_debounce_seconds": 45,
            }
        )
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            config_mgr=config_mgr,
            now_factory=lambda: clock[0],
        )
        snapshots = [
            QuoteSnapshot(symbol="600519.SH", name="贵州茅台", last_price=106.0, bid_price=105.9, ask_price=106.1, volume=100_000, pre_close=100.0),
            QuoteSnapshot(symbol="000001.SZ", name="平安银行", last_price=10.2, bid_price=10.19, ask_price=10.21, volume=200_000, pre_close=10.0),
        ]
        alerts = [AlertEvent(symbol="600519.SH", alert_type="price_spike", message="贵州茅台 价格异动", severity="warning", price=106.0, change_pct=0.06)]

        heartbeat = service.save_heartbeat_if_due(snapshots, alerts, trigger="test")
        assert heartbeat is not None
        assert heartbeat["items"][0]["name"] == "贵州茅台"
        assert heartbeat["staleness_level"] == "fresh"
        assert heartbeat["expires_at"] == datetime(2026, 4, 4, 10, 10, 0).isoformat()

        skipped = service.save_heartbeat_if_due(snapshots, alerts, trigger="test")
        assert skipped is None

        recorded = service.record_alert_events(alerts, snapshots)
        assert len(recorded) == 1
        assert recorded[0]["name"] == "贵州茅台"

        deduped = service.record_alert_events(alerts, snapshots)
        assert deduped == []

        clock[0] = datetime(2026, 4, 4, 10, 11, 0)
        later = service.save_heartbeat_if_due(snapshots, alerts, trigger="test")
        assert later is not None

        state = service.get_state()
        assert state["event_count"] == 1
        assert len(state["heartbeat_history"]) == 2
        assert state["heartbeat_freshness"]["is_fresh"] is True

    def test_save_pool_snapshot(self, tmp_path):
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            archive_store=DataArchiveStore(tmp_path / "state"),
        )
        snapshot = service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            discussion_state="round_1_running",
            pool_state="focus_pool_building",
            pool_snapshot={
                "counts": {"candidate_pool": 3, "focus_pool": 2, "execution_pool": 1, "watchlist": 1, "rejected": 1},
                "candidate_pool": [{"symbol": "600519.SH", "name": "贵州茅台"}],
                "focus_pool": [{"symbol": "600519.SH", "name": "贵州茅台"}],
                "execution_pool": [{"symbol": "600519.SH", "name": "贵州茅台"}],
                "watchlist": [{"symbol": "000001.SZ", "name": "平安银行"}],
                "rejected": [{"symbol": "688981.SH", "name": "中芯国际"}],
            },
        )
        assert snapshot["counts"]["candidate_pool"] == 3
        assert snapshot["discussion_state"] == "round_1_running"
        state = service.get_state()
        assert state["latest_pool_snapshot"]["focus_pool"][0]["name"] == "贵州茅台"
        assert len(state["pool_snapshot_history"]) == 1
        latest_monitor_context = ((tmp_path / "state") / "serving" / "latest_monitor_context.json").read_text(encoding="utf-8")
        assert "贵州茅台" in latest_monitor_context

    def test_save_pool_snapshot_emits_state_change_events(self, tmp_path):
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            now_factory=lambda: clock[0],
        )
        service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            pool_snapshot={
                "counts": {"candidate_pool": 3, "focus_pool": 2, "execution_pool": 0, "watchlist": 2, "rejected": 1},
                "candidate_pool": [
                    {"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-3", "symbol": "600036.SH", "name": "招商银行", "risk_gate": "pending", "audit_gate": "pending"},
                ],
                "focus_pool": [{"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台"}],
                "execution_pool": [],
                "watchlist": [],
                "rejected": [],
            },
        )
        clock[0] = datetime(2026, 4, 4, 10, 2, 0)
        service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            pool_snapshot={
                "counts": {"candidate_pool": 3, "focus_pool": 2, "execution_pool": 1, "watchlist": 1, "rejected": 1},
                "candidate_pool": [
                    {"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行", "risk_gate": "allow", "audit_gate": "clear"},
                    {"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-3", "symbol": "600036.SH", "name": "招商银行", "risk_gate": "pending", "audit_gate": "pending"},
                ],
                "focus_pool": [{"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行"}],
                "execution_pool": [{"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行"}],
                "watchlist": [],
                "rejected": [],
            },
        )
        state = service.get_state(event_limit=20)
        event_types = [item["alert_type"] for item in state["recent_events"]]
        assert "top3_changed" in event_types
        assert "execution_pool_changed" in event_types
        assert "risk_gate_changed" in event_types
        assert "audit_gate_changed" in event_types
        assert all(item["event_source"] == "state_change" for item in state["recent_events"])

        summary = service.build_change_summary(event_limit=20)
        assert summary["count"] >= 4
        assert "top3_changed" in summary["type_counts"]
        assert summary["notify_level"] == "critical"
        assert summary["should_notify"] is True
        assert any("前3变化" in line for line in summary["lines"])

    def test_heartbeat_freshness_turns_stale_after_expiry(self, tmp_path):
        config_mgr = RuntimeConfigManager(tmp_path / "runtime_config.json")
        config_mgr.update(**{"watch.heartbeat_save_seconds": 60})
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            config_mgr=config_mgr,
            now_factory=lambda: clock[0],
        )
        snapshots = [QuoteSnapshot(symbol="600519.SH", name="贵州茅台", last_price=100.0, bid_price=99.9, ask_price=100.1, volume=1000, pre_close=99.0)]
        service.save_heartbeat_if_due(snapshots, trigger="test")
        clock[0] = datetime(2026, 4, 4, 10, 1, 1)
        freshness = service.get_state()["heartbeat_freshness"]
        assert freshness["is_fresh"] is False
        assert freshness["staleness_level"] == "stale"

    def test_polling_status_tracks_candidate_focus_execution_layers(self, tmp_path):
        config_mgr = RuntimeConfigManager(tmp_path / "runtime_config.json")
        config_mgr.update(
            **{
                "watch.candidate_poll_seconds": 300,
                "watch.focus_poll_seconds": 60,
                "watch.execution_poll_seconds": 30,
            }
        )
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            config_mgr=config_mgr,
            now_factory=lambda: clock[0],
        )

        initial = service.get_state()["polling_status"]
        assert initial["candidate"]["due_now"] is True
        assert initial["focus"]["due_now"] is True
        assert initial["execution"]["due_now"] is True

        candidate = service.mark_poll_if_due("candidate", trigger="test")
        focus = service.mark_poll_if_due("focus", trigger="test")
        execution = service.mark_poll_if_due("execution", trigger="test")
        assert candidate["triggered"] is True
        assert focus["triggered"] is True
        assert execution["triggered"] is True

        clock[0] = datetime(2026, 4, 4, 10, 0, 31)
        status = service.get_state()["polling_status"]
        assert status["candidate"]["due_now"] is False
        assert status["focus"]["due_now"] is False
        assert status["execution"]["due_now"] is True

        skipped = service.mark_poll_if_due("focus", trigger="test")
        assert skipped["triggered"] is False
        assert skipped["due_now"] is False


class TestLimitAnalyzer:
    def test_analyze_snapshots(self):
        analyzer = LimitAnalyzer()
        snaps = [
            QuoteSnapshot(symbol="A", last_price=11.0, bid_price=10.99, ask_price=11.0, volume=100_000, pre_close=10.0),
            QuoteSnapshot(symbol="B", last_price=9.0, bid_price=8.99, ask_price=9.0, volume=100_000, pre_close=10.0),
            QuoteSnapshot(symbol="C", last_price=10.5, bid_price=10.49, ask_price=10.51, volume=100_000, pre_close=10.0),
        ]
        stats = analyzer.analyze(snaps, "2026-04-03")
        assert stats.limit_up_count == 1
        assert stats.limit_down_count == 1
        assert "A" in stats.limit_up_symbols
        assert "B" in stats.limit_down_symbols

    def test_board_fail_rate(self):
        analyzer = LimitAnalyzer()
        ever_up = ["A", "B", "C"]
        current_up = ["A"]
        rate = analyzer.calc_board_fail_rate(ever_up, current_up)
        assert rate == pytest.approx(2 / 3, abs=0.01)


class TestDragonTiger:
    def test_hot_money_detection(self):
        analyzer = DragonTigerAnalyzer()
        records = [
            DragonTigerRecord(symbol="600519.SH", date="2026-04-03", buy_seats=["宁波解放南路"], sell_seats=[], net_buy=5_000_000),
            DragonTigerRecord(symbol="000001.SZ", date="2026-04-03", buy_seats=["机构专用"], sell_seats=["宁波解放南路"], net_buy=-3_000_000),
        ]
        result = analyzer.analyze(records)
        assert "600519.SH" in result["hot_money_buy"]
        assert "000001.SZ" in result["hot_money_sell"]

    def test_is_hot_money_seat(self):
        analyzer = DragonTigerAnalyzer()
        assert analyzer.is_hot_money_seat("宁波解放南路")
        assert not analyzer.is_hot_money_seat("机构专用")


class TestFeishuNotifier:
    def test_disabled_when_no_url(self):
        notifier = FeishuNotifier("", "", "")
        result = notifier.send_text("test")
        assert result is False

    def test_enabled_flag(self):
        notifier_off = FeishuNotifier("", "", "")
        notifier_on = FeishuNotifier("app_id", "secret", "chat_id")
        assert not notifier_off._enabled
        assert notifier_on._enabled


class TestMessageDispatcher:
    def test_dispatch_no_url(self):
        notifier = FeishuNotifier("", "", "")
        dispatcher = MessageDispatcher(notifier)
        result = dispatcher.dispatch("test", "title", "content")
        assert result is False

    def test_rate_limiting(self):
        notifier = FeishuNotifier("", "", "")
        dispatcher = MessageDispatcher(notifier)
        dispatcher.dispatch("test_channel", "t", "c")
        dispatcher.dispatch("test_channel", "t", "c")
        rec = dispatcher._records.get("test_channel")
        assert rec is not None
        assert rec.count >= 1

    def test_dispatch_monitor_changes(self):
        notifier = FeishuNotifier("", "", "")
        dispatcher = MessageDispatcher(notifier)
        result = dispatcher.dispatch_monitor_changes("池层状态变化提醒", "content", level="warning", force=True)
        assert result is False

    def test_dispatch_discussion_summary(self):
        notifier = FeishuNotifier("", "", "")
        dispatcher = MessageDispatcher(notifier)
        result = dispatcher.dispatch_discussion_summary("最终推荐已生成", "content", level="info", force=True)
        assert result is False

    def test_dispatch_governance_update(self):
        notifier = FeishuNotifier("", "", "")
        dispatcher = MessageDispatcher(notifier)
        result = dispatcher.dispatch_governance_update("自然语言调参已生效", "content", level="info", force=True)
        assert result is False


class DummyDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch_trade(self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "force": force,
                "channel": "trade",
            }
        )
        return True

    def dispatch_monitor_changes(self, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "force": force,
            }
        )
        return True

    def dispatch_discussion_summary(self, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "force": force,
                "channel": "discussion_summary",
            }
        )
        return True

    def dispatch_governance_update(self, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "force": force,
                "channel": "governance_update",
            }
        )
        return True

    def dispatch_live_execution_alert(self, title: str, content: str, level: str = "warning", force: bool = False) -> bool:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "force": force,
                "channel": "live_execution_alert",
            }
        )
        return True


class TestMonitorChangeNotifier:
    def test_dispatch_latest_sends_critical_summary_once(self, tmp_path):
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            now_factory=lambda: clock[0],
        )
        dispatcher = DummyDispatcher()
        notifier = MonitorChangeNotifier(service, dispatcher)

        service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            pool_snapshot={
                "counts": {"candidate_pool": 2, "focus_pool": 2, "execution_pool": 0, "watchlist": 0, "rejected": 0},
                "candidate_pool": [
                    {"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行", "risk_gate": "pending", "audit_gate": "pending"},
                ],
                "focus_pool": [],
                "execution_pool": [],
                "watchlist": [],
                "rejected": [],
            },
        )
        clock[0] = datetime(2026, 4, 4, 10, 1, 0)
        service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            pool_snapshot={
                "counts": {"candidate_pool": 2, "focus_pool": 2, "execution_pool": 1, "watchlist": 0, "rejected": 0},
                "candidate_pool": [
                    {"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台", "risk_gate": "allow", "audit_gate": "clear"},
                    {"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行", "risk_gate": "pending", "audit_gate": "pending"},
                ],
                "focus_pool": [],
                "execution_pool": [{"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台"}],
                "watchlist": [],
                "rejected": [],
            },
        )

        first = notifier.dispatch_latest()
        second = notifier.dispatch_latest()

        assert first.dispatched is True
        assert first.reason == "sent"
        assert second.dispatched is False
        assert second.reason == "duplicate"
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["level"] == "critical"
        assert service.get_last_change_summary_dispatch() is not None

    def test_dispatch_latest_skips_info_only_summary(self, tmp_path):
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        service = MonitorStateService(
            StateStore(tmp_path / "monitor_state.json"),
            now_factory=lambda: clock[0],
        )
        dispatcher = DummyDispatcher()
        notifier = MonitorChangeNotifier(service, dispatcher)

        service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            pool_snapshot={
                "counts": {"candidate_pool": 3, "focus_pool": 0, "execution_pool": 0, "watchlist": 0, "rejected": 0},
                "candidate_pool": [
                    {"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-3", "symbol": "600036.SH", "name": "招商银行", "risk_gate": "pending", "audit_gate": "pending"},
                ],
                "focus_pool": [],
                "execution_pool": [],
                "watchlist": [],
                "rejected": [],
            },
        )
        clock[0] = datetime(2026, 4, 4, 10, 1, 0)
        service.save_pool_snapshot(
            trade_date="2026-04-04",
            source="unit-test",
            pool_snapshot={
                "counts": {"candidate_pool": 3, "focus_pool": 0, "execution_pool": 0, "watchlist": 0, "rejected": 0},
                "candidate_pool": [
                    {"case_id": "case-2", "symbol": "000001.SZ", "name": "平安银行", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-1", "symbol": "600519.SH", "name": "贵州茅台", "risk_gate": "pending", "audit_gate": "pending"},
                    {"case_id": "case-3", "symbol": "600036.SH", "name": "招商银行", "risk_gate": "pending", "audit_gate": "pending"},
                ],
                "focus_pool": [],
                "execution_pool": [],
                "watchlist": [],
                "rejected": [],
            },
        )

        result = notifier.dispatch_latest()

        assert result.dispatched is False
        assert result.reason == "policy_skip"
        assert dispatcher.calls == []


class TestDiscussionFinalizeNotifier:
    def test_dispatch_sends_once_for_ready_finalize(self, tmp_path):
        from ashare_system.discussion.candidate_case import CandidateCaseService, CandidateOpinion
        from ashare_system.discussion.discussion_service import DiscussionCycleService
        from ashare_system.governance.param_registry import ParameterRegistry
        from ashare_system.governance.param_service import ParameterService
        from ashare_system.governance.param_store import ParameterEventStore

        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        registry = ParameterRegistry()
        param_service = ParameterService(registry=registry, store=ParameterEventStore(tmp_path / "params.json"))
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        dispatcher = DummyDispatcher()
        state_store = StateStore(tmp_path / "meeting_state.json")

        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-04T10:00:00",
                "top_picks": [
                    {"symbol": "000001.SZ", "name": "平安银行", "rank": 1, "selection_score": 88.0, "action": "BUY", "summary": "策略排序靠前"},
                    {"symbol": "600036.SH", "name": "招商银行", "rank": 2, "selection_score": 75.0, "action": "HOLD", "summary": "进入观察池"},
                ],
                "summary": {"buy_count": 1, "hold_count": 1},
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case_id = case_service.list_cases(trade_date="2026-04-04", limit=1)[0].case_id
        for agent_id, stance, reasons in [
            ("ashare-research", "selected", ["研究支持"]),
            ("ashare-strategy", "selected", ["策略支持"]),
            ("ashare-risk", "selected", ["风控通过"]),
            ("ashare-audit", "selected", ["审计通过"]),
        ]:
            case_service.record_opinion(
                case_id,
                CandidateOpinion(
                    round=1,
                    agent_id=agent_id,
                    stance=stance,
                    confidence="high" if agent_id != "ashare-audit" else "medium",
                    reasons=reasons,
                    evidence_refs=[f"{agent_id}:1"],
                    recorded_at=clock[0].isoformat(),
                ),
            )
        case_service.rebuild_case(case_id)
        cycle_service.bootstrap_cycle("2026-04-04")
        cycle_service.finalize_cycle("2026-04-04")
        state_store.set(
            "latest_execution_precheck",
            {
                "trade_date": "2026-04-04",
                "status": "ready",
                "approved_count": 1,
                "blocked_count": 0,
                "summary_lines": [
                    "账户 sim-001 执行预检: 通过 1，阻断 0。",
                    "测试基线 total>=100000.0 repo=70000.0 stock_budget=30000.0 remaining=29000.0。",
                ],
                "minimum_total_invested_amount": 100000.0,
                "reverse_repo_reserved_amount": 70000.0,
                "reverse_repo_value": 70000.0,
                "stock_test_budget_amount": 30000.0,
                "stock_test_budget_remaining": 29000.0,
                "items": [
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "approved": True,
                        "proposed_quantity": 100,
                        "proposed_value": 1000.0,
                        "budget_value": 1000.0,
                        "blockers": [],
                    }
                ],
            },
        )
        state_store.set(
            "execution_dispatch:2026-04-04",
            {
                "trade_date": "2026-04-04",
                "status": "preview",
                "submitted_count": 0,
                "preview_count": 1,
                "blocked_count": 0,
                "summary_lines": ["执行派发结果: submitted=0 preview=1 blocked=0."],
                "receipts": [
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "status": "preview",
                        "reason": "preview_only",
                        "request": {"quantity": 100, "price": 10.0},
                    }
                ],
            },
        )

        notifier = DiscussionFinalizeNotifier(case_service, cycle_service, state_store, dispatcher)
        first = notifier.dispatch("2026-04-04")
        second = notifier.dispatch("2026-04-04")

        assert first.dispatched is True
        assert first.reason == "sent"
        assert first.payload["status"] == "ready"
        assert "client_brief" in first.payload
        assert first.payload["client_brief"]["status"] == "ready"
        assert second.dispatched is False
        assert second.reason == "duplicate"
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["channel"] == "discussion_summary"
        assert "执行预检" in dispatcher.calls[0]["content"]
        assert "执行回执" in dispatcher.calls[0]["content"]
        assert "测试基线" in dispatcher.calls[0]["content"]
        assert "stock_budget=30000.0" in dispatcher.calls[0]["content"]
        assert "建议数量 100 股" in dispatcher.calls[0]["content"]
        assert "预算 1000.0" in dispatcher.calls[0]["content"]
        assert "执行派发状态 预演，提交 0，预演 1，阻断 0。" in dispatcher.calls[0]["content"]
        assert "preview_only" in dispatcher.calls[0]["content"]

    def test_dispatch_sends_warning_for_blocked_finalize(self, tmp_path):
        from ashare_system.discussion.candidate_case import CandidateCaseService
        from ashare_system.discussion.discussion_service import DiscussionCycleService
        from ashare_system.governance.param_registry import ParameterRegistry
        from ashare_system.governance.param_service import ParameterService
        from ashare_system.governance.param_store import ParameterEventStore

        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        registry = ParameterRegistry()
        param_service = ParameterService(registry=registry, store=ParameterEventStore(tmp_path / "params.json"))
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        dispatcher = DummyDispatcher()
        state_store = StateStore(tmp_path / "meeting_state.json")

        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-2",
                "generated_at": "2026-04-04T10:00:00",
                "top_picks": [
                    {"symbol": "688981.SH", "name": "中芯国际", "rank": 4, "selection_score": 58.0, "action": "HOLD", "summary": "当前仍不足以执行"},
                ],
                "summary": {"buy_count": 0, "hold_count": 1},
            },
            focus_pool_capacity=3,
            execution_pool_capacity=1,
        )
        cycle_service.bootstrap_cycle("2026-04-04")
        cycle_service.finalize_cycle("2026-04-04")

        notifier = DiscussionFinalizeNotifier(case_service, cycle_service, state_store, dispatcher)
        result = notifier.dispatch("2026-04-04")

        assert result.dispatched is True
        assert result.payload["status"] == "blocked"
        assert result.payload["client_brief"]["status"] == "blocked"
        assert dispatcher.calls[0]["level"] == "warning"


class TestGovernanceAdjustmentNotifier:
    def test_dispatch_sends_once(self, tmp_path):
        dispatcher = DummyDispatcher()
        notifier = GovernanceAdjustmentNotifier(StateStore(tmp_path / "meeting_state.json"), dispatcher)

        first = notifier.dispatch(
            instruction="把股票池改到30只，心跳改成5分钟",
            applied=True,
            summary_lines=["base_pool_capacity 已调整为 30", "monitor_heartbeat_save_seconds 已调整为 300"],
            matched_items=[
                {"param_key": "base_pool_capacity", "new_value": 30},
                {"param_key": "monitor_heartbeat_save_seconds", "new_value": 300},
            ],
        )
        second = notifier.dispatch(
            instruction="把股票池改到30只，心跳改成5分钟",
            applied=True,
            summary_lines=["base_pool_capacity 已调整为 30", "monitor_heartbeat_save_seconds 已调整为 300"],
            matched_items=[
                {"param_key": "base_pool_capacity", "new_value": 30},
                {"param_key": "monitor_heartbeat_save_seconds", "new_value": 300},
            ],
        )
        assert first.dispatched is True
        assert second.dispatched is False
        assert second.reason == "duplicate"
        assert dispatcher.calls[0]["channel"] == "governance_update"


class TestLiveExecutionAlertNotifier:
    def test_next_action_mapping_prefers_reduce_position_before_market_retry(self):
        actions = _build_execution_next_actions(["trading_session_closed", "total_position_limit_reached"])
        assert actions[0]["code"] == "reduce_existing_positions"
        assert actions[0]["label"] == "先减仓再重试"
        assert actions[1]["code"] == "retry_when_market_opens"
        lines = _build_execution_next_action_lines(actions)
        assert lines == ["建议动作: 先减仓再重试；开盘后重新预检。"]

    def test_precheck_dispatches_once_for_live_system_blockers(self, tmp_path):
        dispatcher = DummyDispatcher()
        notifier = LiveExecutionAlertNotifier(StateStore(tmp_path / "meeting_state.json"), dispatcher, enabled=True)

        payload = {
            "trade_date": "2026-04-04",
            "status": "blocked",
            "approved_count": 0,
            "blocked_count": 1,
            "degraded": True,
            "degrade_to": "blocked",
            "degrade_reason": "emergency_stop_active",
            "primary_recommended_next_action": "resume_trading_switch",
            "primary_recommended_next_action_label": "恢复交易总开关后再试",
            "balance_error": None,
            "items": [
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "approved": False,
                    "blockers": ["emergency_stop_active"],
                    "primary_recommended_next_action": "resume_trading_switch",
                    "primary_recommended_next_action_label": "恢复交易总开关后再试",
                }
            ],
        }

        first = notifier.dispatch_precheck(payload)
        second = notifier.dispatch_precheck(payload)

        assert first.dispatched is True
        assert first.reason == "sent"
        assert first.payload["title"] == "实盘预检异常"
        assert second.dispatched is False
        assert second.reason == "duplicate"
        assert dispatcher.calls[0]["channel"] == "live_execution_alert"
        assert "emergency_stop_active" in dispatcher.calls[0]["content"]
        assert "恢复交易总开关后再试" in dispatcher.calls[0]["content"]

    def test_dispatch_dispatches_for_failed_live_submission(self, tmp_path):
        dispatcher = DummyDispatcher()
        notifier = LiveExecutionAlertNotifier(StateStore(tmp_path / "meeting_state.json"), dispatcher, enabled=True)

        payload = {
            "trade_date": "2026-04-04",
            "status": "blocked",
            "submitted_count": 0,
            "blocked_count": 1,
            "degraded": True,
            "degrade_to": "blocked",
            "degrade_reason": "dispatch_failed",
            "receipts": [
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "status": "dispatch_failed",
                    "reason": "broker timeout",
                }
            ],
        }

        result = notifier.dispatch_dispatch(payload)

        assert result.dispatched is True
        assert result.payload["level"] == "critical"
        assert dispatcher.calls[0]["channel"] == "live_execution_alert"
        assert "券商返回超时" in dispatcher.calls[0]["content"]
        assert "失败" in dispatcher.calls[0]["content"]


class TestTemplates:
    def test_trade_template(self):
        msg = trade_executed_template("600519.SH", "BUY", 1600.0, 100, pnl=500.0)
        assert "600519.SH" in msg
        assert "买入" in msg
        assert "BUY" in msg
        assert "500" in msg

    def test_execution_order_event_template(self):
        msg = execution_order_event_template(
            action="自动撤单",
            symbol="000001.SZ",
            name="平安银行",
            account_id="sim-001",
            side="BUY",
            quantity=100,
            price=10.0,
            order_id="order-1",
            status="CANCELLED",
            decision_id="case-1",
            reason="stale_pending_order",
        )
        assert "自动撤单" in msg
        assert "平安银行" in msg
        assert "order-1" in msg
        assert "买入" in msg
        assert "已撤单" in msg
        assert "挂单超时，触发自动撤单" in msg
        assert "stale_pending_order" in msg

    def test_daily_report_template(self):
        profile = MarketProfile(sentiment_phase="主升", sentiment_score=65.0, position_ceiling=0.8, hot_sectors=["AI", "新能源"])
        msg = daily_report_template("2026-04-03", profile, 5000.0, 3)
        assert "主升" in msg
        assert "5000" in msg

    def test_risk_alert_template(self):
        msg = risk_alert_template("stop_loss", "亏损超过5%", "600519.SH")
        assert "stop_loss" in msg
        assert "600519.SH" in msg

    def test_monitor_change_summary_template(self):
        msg = monitor_change_summary_template("池层状态变化提醒", ["[执行池变化] 000001.SZ 平安银行 进入 execution_pool"])
        assert "池层状态变化提醒" in msg
        assert "execution_pool" in msg

    def test_live_execution_alert_template(self):
        msg = live_execution_alert_template("实盘派发异常", ["交易日 2026-04-04 实盘派发异常，状态 blocked。"])
        assert "实盘派发异常" in msg

    def test_execution_dispatch_notification_template(self):
        msg = execution_dispatch_notification_template("执行预演回执", ["执行派发状态 预演，提交 0，预演 1，阻断 0。"])
        assert "执行预演回执" in msg
        assert "预演" in msg
        assert "2026-04-04" in msg

    def test_execution_precheck_summary_lines_include_guidance(self):
        lines = execution_precheck_summary_lines(
            {
                "summary_lines": ["账户 sim-001 执行预检: 通过 0，阻断 1。"],
                "minimum_total_invested_amount": 100000.0,
                "reverse_repo_reserved_amount": 70000.0,
                "reverse_repo_value": 70000.0,
                "stock_test_budget_amount": 30000.0,
                "stock_test_budget_remaining": 30000.0,
                "primary_recommended_next_action": "retry_when_market_opens",
                "primary_recommended_next_action_label": "开盘后重新预检",
                "items": [
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "approved": False,
                        "blockers": ["trading_session_closed"],
                        "primary_recommended_next_action": "retry_when_market_opens",
                        "primary_recommended_next_action_label": "开盘后重新预检",
                    }
                ],
            }
        )
        assert any("测试基线 100000.0" in line for line in lines)
        assert any("建议 开盘后重新预检" in line for line in lines)
        assert any("全局建议: 开盘后重新预检" in line for line in lines)

    def test_discussion_reply_pack_template(self):
        msg = discussion_reply_pack_template(
            "2026-04-04",
            ["交易日 2026-04-04，候选 10 只，入选 2 只，观察 5 只，淘汰 3 只。"],
            ["000001.SZ 平安银行：仓位限制仍需说明"],
            ["000001.SZ 平安银行：ashare-risk 在 ashare-strategy 质疑后改判为 support，原因=执行仓位条件明确"],
            ["000001.SZ 平安银行 | 排名=1 | 分数=88.5 | 风控=allow 审计=clear | 理由=策略排序靠前"],
            ["600036.SH 招商银行 | 排名=4 | 分数=71.0 | 风控=limit 审计=hold | 理由=仍需补证据"],
            ["688981.SH 中芯国际 | 排名=10 | 分数=58.0 | 风控=reject 审计=hold | 理由=波动过大"],
        )
        assert "选股讨论简报 2026-04-04" in msg
        assert "争议焦点:" in msg
        assert "讨论收敛:" in msg
        assert "入选:" in msg
        assert "观察:" in msg
        assert "淘汰:" in msg

    def test_governance_adjustment_template(self):
        msg = governance_adjustment_template(
            "自然语言调参已生效",
            "把股票池改到30只，心跳改成5分钟",
            ["base_pool_capacity 已调整为 30", "monitor_heartbeat_save_seconds 已调整为 300"],
        )
        assert "自然语言调参已生效" in msg
        assert "把股票池改到30只" in msg
        assert "base_pool_capacity 已调整为 30" in msg


class TestReportGenerator:
    def test_render(self):
        gen = ReportGenerator()
        template = "日期: {date}, 收益: {pnl}"
        result = gen.render(template, {"date": "2026-04-03", "pnl": "1000"})
        assert "2026-04-03" in result
        assert "1000" in result

    def test_timestamp_filename(self):
        gen = ReportGenerator()
        name = gen.timestamp_filename("daily_report")
        assert name.startswith("daily_report_")
        assert name.endswith(".md")


class TestDailyReporter:
    def test_generate(self, tmp_path):
        gen = ReportGenerator(output_dir=tmp_path)
        reporter = DailyReporter(gen)
        profile = MarketProfile(sentiment_phase="回暖", sentiment_score=40.0, position_ceiling=0.6)
        data = DailyReportData(date="2026-04-03", profile=profile, total_pnl=2000.0, total_return_pct=0.002, trades=[{"pnl": 2000}], positions=[{"symbol": "600519.SH"}])
        content = reporter.generate(data)
        assert "2026-04-03" in content
        assert "回暖" in content
