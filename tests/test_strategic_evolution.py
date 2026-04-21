import os
import tempfile
import unittest
from pathlib import Path

from ashare_system.container import (
    get_execution_adapter,
    reset_container,
)
from ashare_system.backtest.live_drift import LiveBacktestDriftTracker
from ashare_system.contracts import BalanceSnapshot, PositionSnapshot
from ashare_system.execution.quality_tracker import ExecutionQualityTracker
from ashare_system.infra.trace import TradeTraceService
from ashare_system.learning.failure_journal import FailureJournalService
from ashare_system.learning.market_memory import MarketMemoryService
from ashare_system.risk.portfolio_risk import PortfolioPositionContext, PortfolioRiskChecker
from ashare_system.risk.position_sizing import HistoricalTradeStats, PositionSizer, PositionSizingContext
from ashare_system.strategy.sell_decision import PositionState, SellDecisionEngine


class StrategicEvolutionTests(unittest.TestCase):
    def test_position_sizer_and_portfolio_risk_checker(self) -> None:
        sizer = PositionSizer(default_mode="half_kelly")
        result = sizer.calculate(
            context=PositionSizingContext(
                total_equity=100000.0,
                cash_available=60000.0,
                price=10.0,
                open_slots=3,
            ),
            stats=HistoricalTradeStats(sample_count=30, win_rate=0.6, payoff_ratio=1.8),
        )
        self.assertGreaterEqual(result.quantity, 100)
        self.assertEqual(result.mode, "half_kelly")

        checker = PortfolioRiskChecker()
        risk = checker.check(
            total_equity=100000.0,
            existing_positions=[
                PortfolioPositionContext(symbol="A", market_value=20000.0, sector="半导体", beta=1.2),
                PortfolioPositionContext(symbol="B", market_value=18000.0, sector="半导体", beta=1.1),
            ],
            buy_value=5000.0,
            candidate_symbol="C",
            candidate_sector="半导体",
            candidate_beta=1.0,
            regime_label="weak_defense",
            daily_new_exposure=10000.0,
        )
        self.assertFalse(risk.approved)
        self.assertIn("sector_concentration_exceeded", risk.blockers)

    def test_execution_quality_tracker_generates_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tracker = ExecutionQualityTracker(Path(tmp_dir) / "quality.json")
            tracker.record_submission(
                intent_id="intent-1",
                order_id="order-1",
                trace_id="trace-1",
                symbol="600000.SH",
                side="BUY",
                signal_price=10.0,
                signal_time="2026-04-20T09:30:00",
                submit_price=10.05,
                submit_time="2026-04-20T09:30:02",
                bid_price=10.0,
                ask_price=10.04,
            )
            tracker.record_fill(
                intent_id="intent-1",
                order_id="order-1",
                fill_price=10.08,
                fill_time="2026-04-20T09:30:03",
                filled_quantity=1000,
                filled_value=10080.0,
                commission_cost=5.0,
                status="filled",
            )
            report = tracker.summarize_day("2026-04-20", persist=True)
            self.assertTrue(report["available"])
            self.assertEqual(report["completed_count"], 1)
            self.assertGreater(report["avg_slippage_bps"], 0)

    def test_sell_decision_supports_param_override(self) -> None:
        engine = SellDecisionEngine()
        state = PositionState(
            symbol="600000.SH",
            entry_price=10.0,
            atr=1.0,
            holding_days=1,
            current_price=8.9,
        )
        self.assertIsNone(engine.evaluate(state, playbook="leader_chase", regime="rotation"))
        tightened = engine.evaluate(
            state,
            playbook="leader_chase",
            regime="rotation",
            param_overrides={"atr_stop_mult": 1.0},
        )
        self.assertIsNotNone(tightened)
        self.assertEqual(tightened.reason.value, "initial_stop")

    def test_live_drift_tracker_persists_proxy_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tracker = LiveBacktestDriftTracker(Path(tmp_dir) / "live_drift.json")
            report = tracker.record(
                trade_date="2026-04-20",
                live_pnl_pct=0.023,
                backtest_pnl_pct=0.01,
                cause_breakdown={"mode": "proxy_from_attribution_and_evaluation_ledger"},
            )
            self.assertTrue(report["alert"])
            self.assertAlmostEqual(report["drift_pct"], 0.013, places=6)
            self.assertEqual(tracker.latest()["trade_date"], "2026-04-20")

    def test_live_drift_tracker_replays_minimal_signals(self) -> None:
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            self.skipTest(f"缺少依赖，无法执行 replay 用例: {exc}")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tracker = LiveBacktestDriftTracker(Path(tmp_dir) / "live_drift.json")
            price_data = {
                "600000.SH": pd.DataFrame(
                    [
                        {"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 1000},
                        {"open": 10.1, "high": 10.5, "low": 10.0, "close": 10.4, "volume": 1200},
                    ],
                    index=["2026-04-17", "2026-04-18"],
                )
            }
            report = tracker.record_from_replay(
                trade_date="2026-04-18",
                live_pnl_pct=0.015,
                evaluation_records=[
                    {
                        "trace_id": "trace-1",
                        "generated_at": "2026-04-17T14:55:00",
                        "selected_symbols": ["600000.SH"],
                        "adoption": {"trade_date": "2026-04-17", "adopted_symbols": ["600000.SH"]},
                    }
                ],
                price_data=price_data,
                execution_quality_report={"avg_slippage_bps": 12.5, "avg_latency_ms": 180.0},
                report_trade_date="2026-04-18",
                report_score_date="2026-04-17",
            )
            self.assertEqual(report["cause_breakdown"]["mode"], "minimal_signal_replay")
            self.assertEqual(report["cause_breakdown"]["replay_sample_count"], 1)
            self.assertIn("trace-1", report["cause_breakdown"]["replayed_trace_ids"])
            self.assertGreater(report["backtest_pnl_pct"], 0.0)

    def test_market_memory_deduplicates_and_generates_avoid_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = MarketMemoryService(Path(tmp_dir) / "market_memory.json")
            records = [
                {
                    "trade_date": f"2026-04-{day:02d}",
                    "score_date": f"2026-04-{day:02d}",
                    "symbol": f"6000{day:02d}.SH",
                    "regime": "strong_rotation",
                    "playbook": "leader_chase",
                    "sector": "半导体",
                    "holding_return_pct": -0.02,
                    "holding_days": 2,
                }
                for day in range(1, 7)
            ]
            service.update_from_attribution(records)
            service.update_from_attribution(records)
            context = service.build_compose_context(
                regime_label="strong_rotation",
                playbooks=["leader_chase"],
                sectors=["半导体"],
            )
            self.assertTrue(context["available"])
            exact_match = next(
                item for item in context["items"] if item["playbook"] == "leader_chase" and item["sector"] == "半导体"
            )
            self.assertEqual(exact_match["sample_count"], 6)
            self.assertTrue(context["avoid_pattern"])

    def test_failure_journal_generates_monthly_summary_and_warning_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = FailureJournalService(Path(tmp_dir) / "failure_journal.json")
            records = [
                {
                    "trade_date": f"2026-04-{day:02d}",
                    "score_date": f"2026-04-{day:02d}",
                    "symbol": f"0000{day:02d}.SZ",
                    "playbook": "leader_chase",
                    "regime": "strong_rotation",
                    "sector": "机器人",
                    "holding_return_pct": -0.08,
                    "exit_reason": "entry_failure",
                    "review_tags": ["timing"],
                    "note": "追高后承接不足",
                }
                for day in range(1, 4)
            ]
            service.record_failures(records)
            warning = service.build_pattern_warning(
                playbooks=["leader_chase"],
                regime_label="strong_rotation",
                sectors=["机器人"],
                review_tags=["timing"],
            )
            self.assertTrue(warning["pattern_recurrence_warning"])
            self.assertIn(warning["warning_level"], {"medium", "high"})
            summary = service.latest_monthly_summary(month="2026-04")
            self.assertEqual(summary["sample_count"], 3)

    def test_system_api_exposes_portfolio_efficiency_trace_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                from fastapi.testclient import TestClient
                from ashare_system.app import create_app
            except ModuleNotFoundError as exc:
                self.skipTest(f"缺少依赖，无法拉起 FastAPI 端到端验证: {exc}")
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
            os.environ["ASHARE_LOGS_DIR"] = tmp_dir
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "dry-run"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()
            adapter = get_execution_adapter()
            adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=40000.0)
            adapter.positions["sim-001"] = [
                PositionSnapshot(account_id="sim-001", symbol="600000.SH", quantity=1000, available=1000, cost_price=10.0, last_price=11.0),
                PositionSnapshot(account_id="sim-001", symbol="000001.SZ", quantity=500, available=500, cost_price=20.0, last_price=22.0),
            ]
            trace_service = TradeTraceService(Path(tmp_dir) / "infra" / "trade_traces.json")
            trace_service.append_event(trace_id="trace-demo", stage="compose", payload={"symbol": "600000.SH"}, trade_date="2026-04-20")

            try:
                with TestClient(create_app()) as client:
                    config_response = client.post("/system/config", json={"equity_position_limit": 0.35})
                    self.assertEqual(config_response.status_code, 200)
                    config_payload = config_response.json()
                    self.assertTrue(config_payload["ok"])
                    snapshot_id = config_payload["snapshot_id"]

                    efficiency_payload = client.get("/system/portfolio/efficiency?account_id=sim-001").json()
                    self.assertTrue(efficiency_payload["ok"])
                    self.assertEqual(efficiency_payload["position_count"], 2)

                    trace_payload = client.get("/system/trace/trace-demo").json()
                    self.assertTrue(trace_payload["available"])
                    self.assertEqual(trace_payload["trace"]["event_count"], 1)

                    market_memory_service = MarketMemoryService(Path(tmp_dir) / "learning" / "market_memory.json")
                    market_memory_service.update_from_attribution(
                        [
                            {
                                "trade_date": "2026-04-20",
                                "score_date": "2026-04-20",
                                "symbol": "600000.SH",
                                "regime": "strong_rotation",
                                "playbook": "leader_chase",
                                "sector": "银行",
                                "holding_return_pct": 0.03,
                            }
                        ]
                    )
                    failure_service = FailureJournalService(Path(tmp_dir) / "learning" / "failure_journal.json")
                    failure_service.record_failures(
                        [
                            {
                                "trade_date": "2026-04-20",
                                "score_date": "2026-04-20",
                                "symbol": "000001.SZ",
                                "playbook": "leader_chase",
                                "regime": "strong_rotation",
                                "sector": "银行",
                                "holding_return_pct": -0.05,
                                "exit_reason": "entry_failure",
                                "review_tags": ["timing"],
                            }
                        ]
                    )

                    market_memory_payload = client.get("/system/learning/market-memory").json()
                    self.assertTrue(market_memory_payload["available"])
                    failure_payload = client.get("/system/learning/failure-journal").json()
                    self.assertTrue(failure_payload["available"])
                    pattern_payload = client.get(
                        "/system/learning/failure-journal/pattern-warning?playbooks=leader_chase&regime_label=strong_rotation&sectors=%E9%93%B6%E8%A1%8C&review_tags=timing"
                    ).json()
                    self.assertTrue(pattern_payload["available"])

                    rollback_payload = client.post(f"/system/governance/rollback?snapshot_id={snapshot_id}").json()
                    self.assertTrue(rollback_payload["ok"])
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()
