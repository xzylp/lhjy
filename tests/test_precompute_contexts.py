from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ashare_system.contracts import BarSnapshot, QuoteSnapshot
from ashare_system.discussion.candidate_case import CandidateCaseService
from ashare_system.infra.audit_store import StateStore
from ashare_system.infra.market_adapter import MockMarketDataAdapter
from ashare_system.precompute import DossierPrecomputeService
from ashare_system.runtime_config import RuntimeConfigManager
from ashare_system.settings import load_settings


class PrecomputeContextTests(unittest.TestCase):
    @staticmethod
    def _create_service(root: Path, now: datetime, market: MockMarketDataAdapter | None = None) -> tuple[DossierPrecomputeService, StateStore, StateStore]:
        settings = load_settings()
        settings.workspace = root
        settings.storage_root = root / "state"
        settings.logs_dir = root / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        runtime_state = StateStore(root / "runtime_state.json")
        research_state = StateStore(root / "research_state.json")
        case_service = CandidateCaseService(root / "candidate_cases.json", now_factory=lambda: now)
        config_mgr = RuntimeConfigManager(root / "runtime_config.json")
        service = DossierPrecomputeService(
            settings=settings,
            market_adapter=market or MockMarketDataAdapter(),
            research_state_store=research_state,
            runtime_state_store=runtime_state,
            candidate_case_service=case_service,
            config_mgr=config_mgr,
            now_factory=lambda: now,
        )
        return service, runtime_state, research_state

    @staticmethod
    def _set_runtime_report(runtime_state: StateStore, now: datetime, symbols: list[str]) -> None:
        report = {
            "job_id": f"runtime-{now.strftime('%Y%m%d%H%M%S')}",
            "generated_at": now.isoformat(),
            "top_picks": [
                {
                    "symbol": symbol,
                    "name": symbol,
                    "rank": index,
                    "selection_score": 90.0 - index,
                    "action": "BUY",
                    "summary": "强势候选",
                }
                for index, symbol in enumerate(symbols, start=1)
            ],
        }
        runtime_state.set("latest_runtime_report", report)

    @staticmethod
    def _make_bar(
        symbol: str,
        trade_time: str,
        pre_close: float,
        close: float,
        *,
        high: float | None = None,
        low: float | None = None,
        open_price: float | None = None,
        volume: float = 100000.0,
    ) -> BarSnapshot:
        return BarSnapshot(
            symbol=symbol,
            period="1d",
            open=open_price if open_price is not None else pre_close,
            high=high if high is not None else max(close, pre_close),
            low=low if low is not None else min(close, pre_close),
            close=close,
            volume=volume,
            amount=volume * close,
            trade_time=trade_time,
            pre_close=pre_close,
        )

    def test_precompute_builds_symbol_context_and_event_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            now = datetime.fromisoformat("2026-04-05T09:35:00+08:00")
            market = MockMarketDataAdapter()
            market.get_index_quotes = lambda symbols: [
                QuoteSnapshot(
                    symbol="000300.SH",
                    name="沪深300",
                    last_price=3500.0,
                    bid_price=3499.8,
                    ask_price=3500.2,
                    pre_close=3450.0,
                    volume=1000000.0,
                )
            ]
            service, runtime_state, research_state = self._create_service(root, now, market)
            settings = service._settings

            report = {
                "job_id": "runtime-ctx-1",
                "generated_at": now.isoformat(),
                "top_picks": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "rank": 1,
                        "selection_score": 92.0,
                        "action": "BUY",
                        "summary": "强势候选",
                    }
                ],
            }
            runtime_state.set("latest_runtime_report", report)
            service._candidate_case_service.sync_from_runtime_report(report, focus_pool_capacity=10, execution_pool_capacity=3)
            research_state.set(
                "news",
                [
                    {
                        "event_id": "evt-symbol",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "category": "news",
                        "title": "茅台放量上攻",
                        "summary": "量价齐升。",
                        "source": "manual",
                        "source_type": "newswire",
                        "severity": "warning",
                        "sentiment": "positive",
                        "event_at": "2026-04-05T09:31:00+08:00",
                        "recorded_at": "2026-04-05T09:31:30+08:00",
                        "impact_scope": "symbol",
                        "payload": {"tags": ["white-liquor", "intraday"]},
                        "evidence_url": "https://example.com/symbol",
                    }
                ],
            )
            research_state.set("announcements", [])
            research_state.set(
                "policy",
                [
                    {
                        "event_id": "evt-sector",
                        "symbol": "",
                        "category": "policy",
                        "title": "白酒消费预期修复",
                        "summary": "板块情绪回暖。",
                        "source": "manual",
                        "source_type": "policy",
                        "severity": "info",
                        "sentiment": "positive",
                        "event_at": "2026-04-05T09:32:00+08:00",
                        "recorded_at": "2026-04-05T09:32:10+08:00",
                        "impact_scope": "sector",
                        "payload": {"tags": ["white-liquor"]},
                        "evidence_url": "https://example.com/sector",
                    },
                    {
                        "event_id": "evt-market",
                        "symbol": "",
                        "category": "policy",
                        "title": "指数共振回暖",
                        "summary": "市场风险偏好改善。",
                        "source": "manual",
                        "source_type": "policy",
                        "severity": "warning",
                        "sentiment": "positive",
                        "event_at": "2026-04-05T09:33:00+08:00",
                        "recorded_at": "2026-04-05T09:33:10+08:00",
                        "impact_scope": "market",
                        "payload": {"tags": ["market"]},
                        "evidence_url": "https://example.com/market",
                    },
                ],
            )

            pack = service.precompute(force=True)

            self.assertEqual(pack["event_context"]["counts_by_scope"]["market"], 1)
            self.assertEqual(pack["event_context"]["counts_by_scope"]["sector"], 1)
            self.assertEqual(pack["event_context"]["counts_by_scope"]["symbol"], 1)

            item = pack["items"][0]
            self.assertIn("symbol_context", item)
            self.assertIn("behavior_profile", item)
            self.assertEqual(item["symbol_context"]["sector_relative"]["sector_tags"], ["white-liquor"])
            self.assertEqual(item["symbol_context"]["event_summary"]["counts_by_scope"]["market"], 1)
            self.assertEqual(item["symbol_context"]["event_summary"]["counts_by_scope"]["sector"], 1)
            self.assertEqual(item["symbol_context"]["event_summary"]["counts_by_scope"]["symbol"], 1)
            self.assertEqual(item["event_context"]["by_scope_counts"]["market"], 1)
            self.assertEqual(item["symbol_context"]["market_relative"]["benchmark_symbol"], "000300.SH")
            self.assertIn("behavior_profile", item["symbol_context"])
            self.assertEqual(item["behavior_profile"]["symbol"], "600519.SH")
            self.assertGreaterEqual(item["behavior_profile"]["optimal_hold_days"], 1)
            self.assertGreater(item["behavior_profile"]["board_success_rate_20d"], 0.0)
            self.assertGreater(item["behavior_profile"]["leader_frequency_30d"], 0.0)
            self.assertEqual(item["behavior_profile_source"], "computed")
            self.assertEqual(item["behavior_profile_trade_date"], "2026-04-05")
            self.assertEqual(item["symbol_context"]["behavior_profile_source"], "computed")
            self.assertEqual(item["symbol_context"]["behavior_profile_trade_date"], "2026-04-05")
            self.assertIn("behavior_profiles", pack)
            self.assertGreaterEqual(len(pack["behavior_profiles"]), 1)
            self.assertIn("behavior_profile_context", pack)
            self.assertEqual(pack["behavior_profile_context"]["source_counts"]["computed"], 1)
            self.assertEqual(pack["behavior_profile_context"]["coverage_ratio"], 1.0)

            serving_file = settings.storage_root / "serving" / "latest_symbol_contexts.json"
            self.assertTrue(serving_file.exists())
            serving_payload = json.loads(serving_file.read_text(encoding="utf-8"))
            self.assertEqual(serving_payload["items"][0]["symbol"], "600519.SH")
            self.assertIn("behavior_profile", serving_payload["items"][0])
            profile_file = settings.storage_root / "serving" / "latest_stock_behavior_profiles.json"
            self.assertTrue(profile_file.exists())
            profile_payload = json.loads(profile_file.read_text(encoding="utf-8"))
            self.assertEqual(profile_payload["symbol_count"], 1)
            self.assertEqual(profile_payload["source_counts"]["computed"], 1)

    def test_precompute_prefers_same_day_behavior_profile_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            now = datetime.fromisoformat("2026-04-05T09:35:00+08:00")
            market = MockMarketDataAdapter()
            service, runtime_state, _ = self._create_service(root, now, market)
            self._set_runtime_report(runtime_state, now, ["600519.SH", "600036.SH"])

            def _bars_phase_one(symbols: list[str], period: str, count: int = 1) -> list[BarSnapshot]:
                del period, count
                bars: list[BarSnapshot] = []
                if "600519.SH" in symbols:
                    bars.extend(
                        [
                            self._make_bar("600519.SH", "2026-04-01T15:00:00+08:00", 10.0, 11.0, high=11.0, low=10.05),
                            self._make_bar("600519.SH", "2026-04-02T15:00:00+08:00", 11.0, 11.7, high=12.1, low=10.8),
                            self._make_bar("600519.SH", "2026-04-03T15:00:00+08:00", 11.7, 11.9, high=12.87, low=11.4),
                        ]
                    )
                return bars

            def _bars_phase_two(symbols: list[str], period: str, count: int = 1) -> list[BarSnapshot]:
                del period, count
                bars: list[BarSnapshot] = []
                if "600519.SH" in symbols:
                    bars.extend(
                        [
                            self._make_bar("600519.SH", "2026-04-01T15:00:00+08:00", 10.0, 9.8, high=10.05, low=9.7),
                            self._make_bar("600519.SH", "2026-04-02T15:00:00+08:00", 9.8, 9.5, high=9.85, low=9.4),
                            self._make_bar("600519.SH", "2026-04-03T15:00:00+08:00", 9.5, 9.2, high=9.52, low=9.1),
                        ]
                    )
                if "600036.SH" in symbols:
                    bars.extend(
                        [
                            self._make_bar("600036.SH", "2026-04-01T15:00:00+08:00", 8.0, 8.6, high=8.8, low=7.95),
                            self._make_bar("600036.SH", "2026-04-02T15:00:00+08:00", 8.6, 9.46, high=9.46, low=8.55),
                            self._make_bar("600036.SH", "2026-04-03T15:00:00+08:00", 9.46, 9.7, high=10.4, low=9.3),
                        ]
                    )
                return bars

            market.get_bars = _bars_phase_one
            first_pack = service.precompute(
                trade_date="2026-04-05",
                symbols=["600519.SH"],
                source="first-pass",
                force=True,
            )
            first_profile = first_pack["items"][0]["behavior_profile"]

            market.get_bars = _bars_phase_two
            second_pack = service.precompute(
                trade_date="2026-04-05",
                symbols=["600519.SH", "600036.SH"],
                source="second-pass",
            )
            second_item_map = {item["symbol"]: item for item in second_pack["items"]}

            self.assertEqual(second_item_map["600519.SH"]["behavior_profile_source"], "artifact_cache")
            self.assertEqual(second_item_map["600519.SH"]["behavior_profile_trade_date"], "2026-04-05")
            self.assertEqual(second_item_map["600519.SH"]["behavior_profile"], first_profile)
            self.assertEqual(second_item_map["600036.SH"]["behavior_profile_source"], "computed")
            self.assertEqual(second_pack["behavior_profile_context"]["source_counts"]["artifact_cache"], 1)
            self.assertEqual(second_pack["behavior_profile_context"]["source_counts"]["computed"], 1)

    def test_precompute_uses_history_behavior_profile_when_bars_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first_now = datetime.fromisoformat("2026-04-04T14:35:00+08:00")
            market = MockMarketDataAdapter()
            service, runtime_state, _ = self._create_service(root, first_now, market)
            self._set_runtime_report(runtime_state, first_now, ["600519.SH"])

            market.get_bars = lambda symbols, period, count=1: [
                self._make_bar("600519.SH", "2026-04-01T15:00:00+08:00", 10.0, 11.0, high=11.0, low=10.02),
                self._make_bar("600519.SH", "2026-04-02T15:00:00+08:00", 11.0, 12.1, high=12.1, low=10.9),
                self._make_bar("600519.SH", "2026-04-03T15:00:00+08:00", 12.1, 11.8, high=13.31, low=11.6),
            ]
            day_one_pack = service.precompute(
                trade_date="2026-04-04",
                symbols=["600519.SH"],
                source="day-one",
                force=True,
            )
            expected_profile = day_one_pack["items"][0]["behavior_profile"]

            second_now = datetime.fromisoformat("2026-04-05T09:35:00+08:00")
            service._now_factory = lambda: second_now
            self._set_runtime_report(runtime_state, second_now, ["600519.SH"])
            market.get_bars = lambda symbols, period, count=1: []
            day_two_pack = service.precompute(
                trade_date="2026-04-05",
                symbols=["600519.SH"],
                source="day-two",
            )

            item = day_two_pack["items"][0]
            self.assertEqual(item["behavior_profile_source"], "history_cache")
            self.assertEqual(item["behavior_profile_trade_date"], "2026-04-04")
            self.assertEqual(item["behavior_profile"], expected_profile)
            self.assertEqual(day_two_pack["behavior_profile_context"]["source_counts"]["history_cache"], 1)
            self.assertEqual(day_two_pack["behavior_profile_context"]["coverage_ratio"], 1.0)

    def test_refresh_behavior_profiles_builds_artifact_without_dossier_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            now = datetime.fromisoformat("2026-04-05T09:35:00+08:00")
            market = MockMarketDataAdapter()
            service, runtime_state, research_state = self._create_service(root, now, market)
            self._set_runtime_report(runtime_state, now, ["600519.SH"])

            market.get_bars = lambda symbols, period, count=1: [
                self._make_bar("600519.SH", "2026-04-01T15:00:00+08:00", 10.0, 10.8, high=10.8, low=9.95),
                self._make_bar("600519.SH", "2026-04-02T15:00:00+08:00", 10.8, 11.6, high=11.88, low=10.7),
                self._make_bar("600519.SH", "2026-04-03T15:00:00+08:00", 11.6, 11.5, high=12.76, low=11.3),
            ]

            refresh = service.refresh_behavior_profiles(
                trade_date="2026-04-05",
                symbols=["600519.SH"],
                source="daily-job",
                trigger="unit-test",
            )
            self.assertTrue(refresh["ok"])
            self.assertTrue(refresh["refreshed"])
            self.assertEqual(refresh["symbol_count"], 1)
            self.assertEqual(refresh["profile_count"], 1)
            self.assertEqual(refresh["source_counts"]["computed"], 1)

            self.assertIsNone(research_state.get("latest_dossier_pack"))
            profile_payload = research_state.get("latest_stock_behavior_profiles", {})
            self.assertEqual(profile_payload.get("symbol_count"), 1)
            self.assertEqual(profile_payload.get("trade_date"), "2026-04-05")
            self.assertEqual(profile_payload.get("source_counts", {}).get("computed"), 1)

            serving_path = service._settings.storage_root / "serving" / "latest_stock_behavior_profiles.json"
            self.assertTrue(serving_path.exists())

            pack = service.precompute(
                trade_date="2026-04-05",
                symbols=["600519.SH"],
                source="after-refresh",
            )
            item = pack["items"][0]
            self.assertEqual(item["behavior_profile_source"], "artifact_cache")
            self.assertEqual(item["behavior_profile_trade_date"], "2026-04-05")


if __name__ == "__main__":
    unittest.main()
