from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import get_meeting_state_store, reset_container
from ashare_system.contracts import BarSnapshot, BalanceSnapshot, PositionSnapshot, QuoteSnapshot
from ashare_system.data.archive import DataArchiveStore
from ashare_system.infra.adapters import MockExecutionAdapter
from ashare_system.infra.audit_store import StateStore
from ashare_system.monitor.persistence import MonitorStateService
from ashare_system.scheduler import (
    _build_regime_driven_candidate_payloads,
    run_fast_opportunity_scan,
    run_fast_position_watch_scan,
    run_position_watch_scan,
)
from ashare_system.settings import load_settings


@contextmanager
def _temporary_env(**overrides: str):
    keys = set(overrides.keys()) | {
        "ASHARE_STORAGE_ROOT",
        "ASHARE_LOGS_DIR",
        "ASHARE_EXECUTION_MODE",
        "ASHARE_MARKET_MODE",
        "ASHARE_RUN_MODE",
        "ASHARE_EXECUTION_PLANE",
        "ASHARE_ACCOUNT_ID",
        "ASHARE_LIVE_ENABLE",
    }
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
        os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
        os.environ["ASHARE_LIVE_ENABLE"] = "false"
        for key, value in overrides.items():
            os.environ[key] = value
        reset_container()
        try:
            yield Path(tmp_dir)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_container()


class _PositionWatchMarketAdapter:
    def get_symbol_name(self, symbol: str) -> str:
        return "贵州茅台" if symbol == "600519.SH" else symbol

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        return [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=10.6,
                bid_price=10.59,
                ask_price=10.61,
                volume=250_000,
                pre_close=10.0,
            )
        ]

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        base_time = datetime.fromisoformat("2026-04-20T10:00:00+08:00")
        if period == "1m":
            closes = [10.02, 10.08, 10.12, 10.18, 10.2]
        else:
            closes = [10.0, 10.08, 10.15, 10.22, 10.28]
        bars: list[BarSnapshot] = []
        for index, close in enumerate(closes[-count:]):
            bars.append(
                BarSnapshot(
                    symbol="600519.SH",
                    period=period,
                    open=10.0 if index == 0 else close - 0.03,
                    high=close + 0.05,
                    low=close - 0.05,
                    close=close,
                    volume=100_000 + index * 10_000,
                    amount=(100_000 + index * 10_000) * close,
                    trade_time=base_time.replace(minute=base_time.minute + index).isoformat(),
                    pre_close=10.0,
                )
            )
        return bars


class _FastSellPositionWatchMarketAdapter:
    def get_symbol_name(self, symbol: str) -> str:
        return "贵州茅台" if symbol == "600519.SH" else symbol

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        return [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=9.55,
                bid_price=9.54,
                ask_price=9.56,
                volume=180_000,
                pre_close=10.0,
            )
        ]


class _FastOpportunityMarketAdapter:
    def get_symbol_name(self, symbol: str) -> str:
        return {
            "300001.SZ": "预涨停样本",
            "600001.SH": "异常下跌样本",
        }.get(symbol, symbol)

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        dataset = {
            "300001.SZ": QuoteSnapshot(
                symbol="300001.SZ",
                name="预涨停样本",
                last_price=23.52,
                bid_price=23.49,
                ask_price=23.52,
                volume=1_200_000,
                pre_close=20.0,
            ),
            "600001.SH": QuoteSnapshot(
                symbol="600001.SH",
                name="异常下跌样本",
                last_price=8.8,
                bid_price=8.79,
                ask_price=8.81,
                volume=980_000,
                pre_close=10.0,
            ),
        }
        return [dataset[symbol] for symbol in symbols if symbol in dataset]


class _StagedFastOpportunityMarketAdapter:
    def get_symbol_name(self, symbol: str) -> str:
        return {
            "300001.SZ": "早动量样本",
            "300002.SZ": "加速样本",
            "300003.SZ": "联动样本",
            "600001.SH": "异常下跌样本",
        }.get(symbol, symbol)

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        dataset = {
            "300001.SZ": QuoteSnapshot(
                symbol="300001.SZ",
                name="早动量样本",
                last_price=10.4,
                bid_price=10.39,
                ask_price=10.41,
                volume=900_000,
                pre_close=10.0,
            ),
            "300002.SZ": QuoteSnapshot(
                symbol="300002.SZ",
                name="加速样本",
                last_price=10.62,
                bid_price=10.61,
                ask_price=10.62,
                volume=1_800_000,
                pre_close=10.0,
            ),
            "300003.SZ": QuoteSnapshot(
                symbol="300003.SZ",
                name="联动样本",
                last_price=10.36,
                bid_price=10.35,
                ask_price=10.36,
                volume=1_100_000,
                pre_close=10.0,
            ),
            "600001.SH": QuoteSnapshot(
                symbol="600001.SH",
                name="异常下跌样本",
                last_price=8.8,
                bid_price=8.79,
                ask_price=8.81,
                volume=980_000,
                pre_close=10.0,
            ),
        }
        return [dataset[symbol] for symbol in symbols if symbol in dataset]

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        if period != "5m":
            return []
        base_time = datetime.fromisoformat("2026-04-20T10:00:00+08:00")
        series_map = {
            "300001.SZ": [10.05, 10.12, 10.18, 10.28, 10.4],
            "300002.SZ": [9.95, 10.0, 10.0, 10.35, 10.62],
            "300003.SZ": [10.0, 10.08, 10.16, 10.25, 10.36],
            "600001.SH": [9.85, 9.6, 9.3, 9.05, 8.8],
        }
        volume_map = {
            "300001.SZ": [120_000, 150_000, 180_000, 210_000, 230_000],
            "300002.SZ": [100_000, 130_000, 160_000, 220_000, 420_000],
            "300003.SZ": [90_000, 120_000, 140_000, 180_000, 220_000],
            "600001.SH": [180_000, 220_000, 260_000, 280_000, 300_000],
        }
        bars: list[BarSnapshot] = []
        for symbol in symbols:
            closes = series_map.get(symbol, [])
            volumes = volume_map.get(symbol, [])
            for index, close in enumerate(closes[-count:]):
                bars.append(
                    BarSnapshot(
                        symbol=symbol,
                        period=period,
                        open=close - 0.08,
                        high=close + 0.05,
                        low=close - 0.08,
                        close=close,
                        volume=volumes[index],
                        amount=volumes[index] * close,
                        trade_time=base_time.replace(minute=base_time.minute + index * 5).isoformat(),
                        pre_close=10.0,
                    )
                )
        return bars


class _LowBuyPositionWatchMarketAdapter:
    def get_symbol_name(self, symbol: str) -> str:
        return "贵州茅台" if symbol == "600519.SH" else symbol

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        return [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=9.72,
                bid_price=9.71,
                ask_price=9.73,
                volume=160_000,
                pre_close=10.0,
            )
        ]

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        base_time = datetime.fromisoformat("2026-04-20T10:00:00+08:00")
        if period == "1m":
            closes = [10.1, 10.05, 9.98, 9.85, 9.72]
        else:
            closes = [10.15, 10.08, 10.0, 9.88, 9.72]
        bars: list[BarSnapshot] = []
        for index, close in enumerate(closes[-count:]):
            bars.append(
                BarSnapshot(
                    symbol="600519.SH",
                    period=period,
                    open=close + 0.06,
                    high=close + 0.08,
                    low=close - 0.08,
                    close=close,
                    volume=150_000 + index * 10_000,
                    amount=(150_000 + index * 10_000) * close,
                    trade_time=base_time.replace(minute=base_time.minute + index).isoformat(),
                    pre_close=10.0,
                )
            )
        return bars


class _StubCandidateCaseService:
    def __init__(self) -> None:
        self.tickets: list[dict] = []

    def upsert_candidate_tickets(self, trade_date: str, tickets: list[dict]):
        self.tickets.extend([dict(item) for item in tickets])
        return [type("Case", (), {"symbol": item["symbol"]})() for item in tickets]


class _RegimeDrivenFetcher:
    def fetch_sector_symbols(self, sector_name: str) -> list[str]:
        mapping = {
            "机器人": ["300001.SZ", "300002.SZ"],
            "算力": ["300003.SZ", "300004.SZ"],
        }
        return mapping.get(sector_name, [])

    def fetch_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        dataset = {
            "300001.SZ": QuoteSnapshot(symbol="300001.SZ", name="机器人龙头", last_price=11.0, bid_price=10.99, ask_price=11.0, volume=1_800_000, pre_close=10.0),
            "300002.SZ": QuoteSnapshot(symbol="300002.SZ", name="机器人补涨", last_price=10.55, bid_price=10.54, ask_price=10.55, volume=1_200_000, pre_close=10.0),
            "300003.SZ": QuoteSnapshot(symbol="300003.SZ", name="算力龙头", last_price=10.9, bid_price=10.89, ask_price=10.9, volume=1_500_000, pre_close=10.0),
            "300004.SZ": QuoteSnapshot(symbol="300004.SZ", name="算力异动", last_price=10.35, bid_price=10.34, ask_price=10.35, volume=1_700_000, pre_close=10.0),
        }
        return [dataset[symbol] for symbol in symbols if symbol in dataset]


class PositionWatchTests(unittest.TestCase):
    def test_run_position_watch_scan_persists_intraday_watch_and_exit_snapshot(self) -> None:
        with _temporary_env() as tmp_dir:
            settings = load_settings()
            meeting_state_store = StateStore(tmp_dir / "meeting_state.json")
            runtime_state_store = StateStore(tmp_dir / "runtime_state.json")
            monitor_state_service = MonitorStateService(
                StateStore(tmp_dir / "monitor_state.json"),
                archive_store=DataArchiveStore(settings.storage_root),
            )
            execution_adapter = MockExecutionAdapter(
                balances={"sim-001": BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=500_000)},
                positions={
                    "sim-001": [
                        PositionSnapshot(
                            account_id="sim-001",
                            symbol="600519.SH",
                            quantity=1000,
                            available=1000,
                            cost_price=10.0,
                            last_price=10.6,
                        )
                    ]
                },
            )

            payload = run_position_watch_scan(
                settings=settings,
                market=_PositionWatchMarketAdapter(),
                execution_adapter=execution_adapter,
                meeting_state_store=meeting_state_store,
                runtime_state_store=runtime_state_store,
                monitor_state_service=monitor_state_service,
                mode="intraday",
                include_day_trading=True,
                allow_live_sell_submit=False,
                now_factory=lambda: datetime.fromisoformat("2026-04-20T10:05:00"),
            )

            self.assertEqual(payload["mode"], "intraday")
            self.assertEqual(payload["position_count"], 1)
            self.assertGreaterEqual(payload["day_trading_signal_count"], 1)
            self.assertGreaterEqual(payload["preview_count"], 1)
            latest_watch = meeting_state_store.get("latest_position_watch_scan", {})
            self.assertEqual(latest_watch.get("mode"), "intraday")
            self.assertTrue(any(item.get("t_signal_action") == "HIGH_SELL" for item in latest_watch.get("items", [])))
            self.assertTrue(any(item.get("fast_track_review") is not None for item in latest_watch.get("items", [])))
            latest_exit_snapshot = monitor_state_service.get_latest_exit_snapshot()
            self.assertGreaterEqual(int((latest_exit_snapshot.get("snapshot") or {}).get("signal_count", 0) or 0), 1)
            latest_monitor_context = settings.storage_root / "serving" / "latest_monitor_context.json"
            self.assertTrue(latest_monitor_context.exists())

    def test_feishu_day_trading_question_uses_latest_position_watch_scan(self) -> None:
        with _temporary_env():
            trade_date = datetime.now().date().isoformat()
            get_meeting_state_store().set(
                "latest_position_watch_scan",
                {
                    "trade_date": trade_date,
                    "scanned_at": datetime.now().isoformat(),
                    "mode": "intraday",
                    "summary_lines": ["盘中持仓巡视完成: positions=1 sell_signals=0 t_signals=1。"],
                    "items": [
                        {
                            "symbol": "600519.SH",
                            "name": "贵州茅台",
                            "exit_reason": "intraday_t_high_sell",
                            "t_signal_action": "HIGH_SELL",
                            "review_tags": ["day_trading", "intraday", "high_sell"],
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
            self.assertTrue(any("600519.SH" in line or "可复核信号" in line for line in payload["answer_lines"]))

    def test_run_fast_position_watch_scan_queues_gateway_and_persists_small_state(self) -> None:
        with _temporary_env() as tmp_dir:
            settings = load_settings()
            runtime_state_store = StateStore(tmp_dir / "runtime_state.json")
            position_watch_state_store = StateStore(tmp_dir / "position_watch_state.json")
            execution_adapter = MockExecutionAdapter(
                balances={"sim-001": BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=500_000)},
                positions={
                    "sim-001": [
                        PositionSnapshot(
                            account_id="sim-001",
                            symbol="600519.SH",
                            quantity=1000,
                            available=1000,
                            cost_price=10.0,
                            last_price=9.55,
                        )
                    ]
                },
            )

            payload = run_fast_position_watch_scan(
                settings=settings,
                market=_FastSellPositionWatchMarketAdapter(),
                execution_adapter=execution_adapter,
                meeting_state_store=None,
                runtime_state_store=runtime_state_store,
                position_watch_state_store=position_watch_state_store,
                execution_plane="windows_gateway",
                now_factory=lambda: datetime.fromisoformat("2026-04-20T10:05:03"),
            )

            self.assertEqual(payload["mode"], "fast_intraday")
            self.assertEqual(payload["queued_count"], 1)
            latest_watch = position_watch_state_store.get("latest_position_watch_scan", {})
            self.assertEqual(latest_watch.get("mode"), "fast_intraday")
            self.assertEqual(runtime_state_store.get("pending_execution_intents", [])[0]["symbol"], "600519.SH")
            fast_runtime = position_watch_state_store.get("fast_position_watch_runtime", {})
            self.assertIn("600519.SH", ((fast_runtime.get("pending_sell_tracker") or {}).get("by_symbol") or {}))

    def test_run_fast_position_watch_scan_keeps_unsellable_position_visible(self) -> None:
        with _temporary_env() as tmp_dir:
            settings = load_settings()
            runtime_state_store = StateStore(tmp_dir / "runtime_state.json")
            position_watch_state_store = StateStore(tmp_dir / "position_watch_state.json")
            execution_adapter = MockExecutionAdapter(
                balances={"sim-001": BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=500_000)},
                positions={
                    "sim-001": [
                        PositionSnapshot(
                            account_id="sim-001",
                            symbol="600519.SH",
                            quantity=1000,
                            available=0,
                            cost_price=10.0,
                            last_price=10.6,
                        )
                    ]
                },
            )

            payload = run_fast_position_watch_scan(
                settings=settings,
                market=_PositionWatchMarketAdapter(),
                execution_adapter=execution_adapter,
                meeting_state_store=None,
                runtime_state_store=runtime_state_store,
                position_watch_state_store=position_watch_state_store,
                execution_plane="windows_gateway",
                now_factory=lambda: datetime.fromisoformat("2026-04-20T10:05:03"),
            )

            self.assertEqual(payload["position_count"], 1)
            self.assertEqual(payload["queued_count"], 0)
            self.assertEqual(payload["items"][0]["symbol"], "600519.SH")
            self.assertEqual(payload["items"][0]["available"], 0)

    def test_run_fast_opportunity_scan_persists_pre_limit_up_and_abnormal_drop(self) -> None:
        with _temporary_env() as tmp_dir:
            settings = load_settings()
            runtime_state_store = StateStore(tmp_dir / "runtime_state.json")
            position_watch_state_store = StateStore(tmp_dir / "position_watch_state.json")
            runtime_state_store.set(
                "latest_runtime_context",
                {
                    "selected_symbols": ["300001.SZ", "600001.SH"],
                    "top_picks": [{"symbol": "300001.SZ"}, {"symbol": "600001.SH"}],
                },
            )

            payload = run_fast_opportunity_scan(
                settings=settings,
                market=_FastOpportunityMarketAdapter(),
                runtime_state_store=runtime_state_store,
                position_watch_state_store=position_watch_state_store,
                now_factory=lambda: datetime.fromisoformat("2026-04-20T10:05:12"),
            )

            self.assertEqual(payload["count"], 2)
            signal_types = {item.get("signal_type") for item in payload.get("items", [])}
            self.assertIn("pre_limit_up", signal_types)
            self.assertIn("abnormal_drop", signal_types)
            latest_opportunity = position_watch_state_store.get("latest_fast_opportunity_scan", {})
            self.assertEqual(latest_opportunity.get("count"), 2)

    def test_run_fast_opportunity_scan_detects_early_and_acceleration_stages(self) -> None:
        with _temporary_env() as tmp_dir:
            settings = load_settings()
            runtime_state_store = StateStore(tmp_dir / "runtime_state.json")
            position_watch_state_store = StateStore(tmp_dir / "position_watch_state.json")
            runtime_state_store.set(
                "latest_runtime_context",
                {
                    "selected_symbols": ["300001.SZ", "300002.SZ", "300003.SZ", "600001.SH"],
                    "top_picks": [{"symbol": "300001.SZ"}, {"symbol": "300002.SZ"}, {"symbol": "300003.SZ"}, {"symbol": "600001.SH"}],
                    "market_profile": {"hot_sectors": ["机器人"]},
                    "playbook_contexts": [
                        {"symbol": "300001.SZ", "sector": "机器人", "symbol_context": {"sector_relative": {"sector_tags": ["机器人"]}}},
                        {"symbol": "300002.SZ", "sector": "机器人", "symbol_context": {"sector_relative": {"sector_tags": ["机器人"]}}},
                        {"symbol": "300003.SZ", "sector": "机器人", "symbol_context": {"sector_relative": {"sector_tags": ["机器人"]}}},
                        {"symbol": "600001.SH", "sector": "机器人", "symbol_context": {"sector_relative": {"sector_tags": ["机器人"]}}},
                    ],
                },
            )

            payload = run_fast_opportunity_scan(
                settings=settings,
                market=_StagedFastOpportunityMarketAdapter(),
                runtime_state_store=runtime_state_store,
                position_watch_state_store=position_watch_state_store,
                now_factory=lambda: datetime.fromisoformat("2026-04-20T10:05:12"),
            )

            signal_types = {item.get("signal_type") for item in payload.get("items", [])}
            self.assertIn("early_momentum", signal_types)
            self.assertIn("acceleration", signal_types)
            acceleration_item = next(item for item in payload["items"] if item.get("signal_type") == "acceleration")
            self.assertEqual(acceleration_item.get("sector_sync_signal"), "sector_sync_strong")
            self.assertGreaterEqual(float(acceleration_item.get("momentum_slope_5m") or 0.0), 0.02)

    def test_run_position_watch_scan_injects_day_trading_rebuy_candidates(self) -> None:
        with _temporary_env() as tmp_dir:
            settings = load_settings()
            meeting_state_store = StateStore(tmp_dir / "meeting_state.json")
            runtime_state_store = StateStore(tmp_dir / "runtime_state.json")
            execution_adapter = MockExecutionAdapter(
                balances={"sim-001": BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=500_000)},
                positions={
                    "sim-001": [
                        PositionSnapshot(
                            account_id="sim-001",
                            symbol="600519.SH",
                            quantity=1000,
                            available=1000,
                            cost_price=10.0,
                            last_price=9.72,
                        )
                    ]
                },
            )
            candidate_case_service = _StubCandidateCaseService()

            payload = run_position_watch_scan(
                settings=settings,
                market=_LowBuyPositionWatchMarketAdapter(),
                execution_adapter=execution_adapter,
                meeting_state_store=meeting_state_store,
                runtime_state_store=runtime_state_store,
                candidate_case_service=candidate_case_service,
                mode="intraday",
                include_day_trading=True,
                allow_live_sell_submit=False,
                now_factory=lambda: datetime.fromisoformat("2026-04-20T10:15:00"),
            )

            self.assertEqual(payload["day_trading_rebuy_injection"]["ticket_count"], 1)
            self.assertEqual(candidate_case_service.tickets[0]["source"], "day_trading_rebuy")
            self.assertTrue(any(item.get("t_signal_action") == "LOW_BUY" for item in payload.get("items", [])))

    def test_build_regime_driven_candidate_payloads_marks_regime_sources(self) -> None:
        payload = _build_regime_driven_candidate_payloads(
            fetcher=_RegimeDrivenFetcher(),
            runtime_context={
                "market_profile": {"hot_sectors": ["机器人", "算力"]},
                "sector_profiles": [{"sector_name": "机器人", "strength_score": 9.1}, {"sector_name": "算力", "strength_score": 8.7}],
                "playbook_contexts": [
                    {"symbol": "300001.SZ", "sector": "机器人"},
                    {"symbol": "300002.SZ", "sector": "机器人"},
                    {"symbol": "300003.SZ", "sector": "算力"},
                    {"symbol": "300004.SZ", "sector": "算力"},
                ],
            },
            workspace_context={},
        )

        self.assertTrue(payload["available"])
        self.assertTrue(all(item.get("source") == "regime_driven" for item in payload["items"]))
        self.assertTrue(any("sector_leader" in list(item.get("source_tags") or []) for item in payload["items"]))


if __name__ == "__main__":
    unittest.main()
