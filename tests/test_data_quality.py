from __future__ import annotations

import tempfile
import unittest
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

from ashare_system.contracts import BarSnapshot
from ashare_system.data.adjust import fill_suspended_days, mark_adjustment_flags
from ashare_system.data.cache import KlineCache
from ashare_system.data.freshness import DataFreshnessMonitor
from ashare_system.data.quality import BarQualityChecker
from ashare_system.infra.market_adapter import MockMarketDataAdapter
from ashare_system.strategy.factor_monitor import FactorMonitor
from ashare_system.strategy.factor_registry import FactorDefinition, FactorRegistry


class _StubMarket:
    def get_main_board_universe(self) -> list[str]:
        return ["AAA.SH", "BBB.SH", "CCC.SH", "DDD.SH"]

    def get_bars(self, symbols: list[str], period: str, count: int = 1, end_time: str | None = None) -> list[BarSnapshot]:
        bars: list[BarSnapshot] = []
        total = max(count, 90)
        for symbol_index, symbol in enumerate(symbols):
            price = 10.0 + symbol_index
            for day in range(total):
                trade_time = f"2026-01-{day + 1:02d}"
                next_return_bias = 0.001 * (symbol_index + 1)
                next_price = price * (1.0 + next_return_bias)
                bars.append(
                    BarSnapshot(
                        symbol=symbol,
                        period="1d",
                        open=price,
                        high=next_price,
                        low=price * 0.99,
                        close=next_price,
                        volume=100000 + symbol_index * 1000,
                        amount=(100000 + symbol_index * 1000) * next_price,
                        trade_time=trade_time,
                        pre_close=price,
                    )
                )
                price = next_price
        return bars


class DataQualityTests(unittest.TestCase):
    def test_bar_quality_checker_and_adjustment_flags(self) -> None:
        checker = BarQualityChecker()
        bars, alerts = checker.validate_bars(
            [
                BarSnapshot(symbol="AAA.SH", period="1d", open=10, high=10, low=10, close=0, volume=100, amount=1000, trade_time="2026-04-18", pre_close=10),
                BarSnapshot(symbol="AAA.SH", period="1d", open=10, high=12, low=10, close=12, volume=0, amount=0, trade_time="2026-04-19", pre_close=10),
            ]
        )
        self.assertEqual(len(bars), 1)
        self.assertTrue(any(item.issue == "zero_price" for item in alerts))
        self.assertTrue(any(item.issue in {"abnormal_gap", "suspended_day"} for item in alerts))

        frame = pd.DataFrame(
            [
                {"trade_time": "2026-04-18", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000.0, "amount": 10000.0, "pre_close": 10.0},
                {"trade_time": "2026-04-19", "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 0.0, "amount": 0.0, "pre_close": 10.0},
            ]
        ).set_index("trade_time")
        adjusted = mark_adjustment_flags(fill_suspended_days(frame))
        self.assertTrue(bool(adjusted.iloc[-1]["is_suspended"]))
        self.assertEqual(float(adjusted.iloc[-1]["close"]), 10.0)

    def test_factor_monitor_outputs_multi_period_ic(self) -> None:
        registry = FactorRegistry()
        registry.register(
            FactorDefinition(
                id="monitor_test_factor",
                name="监控测试因子",
                group="trend_momentum",
            ),
            executor=lambda definition, candidate, context, market_adapter, trade_date, precomputed_factors=None: {
                "score": float((candidate.get("market_snapshot") or {}).get("last_price", 0.0) or 0.0),
                "evidence": ["stub"],
            },
        )
        monitor = FactorMonitor(registry=registry, market_adapter=_StubMarket())
        snapshot = monitor.build_effectiveness_snapshot(force=True)
        item = next(entry for entry in snapshot["items"] if entry["factor_id"] == "monitor_test_factor")
        self.assertIn("20d", item["ic_by_period"])
        self.assertIn("60d", item["ic_by_period"])
        self.assertIn(item["status"], {"effective", "ineffective"})

    def test_factor_monitor_skips_constant_series_without_runtime_warning(self) -> None:
        registry = FactorRegistry()
        registry.register(
            FactorDefinition(
                id="constant_factor",
                name="常数因子",
                group="trend_momentum",
            ),
            executor=lambda definition, candidate, context, market_adapter, trade_date, precomputed_factors=None: {
                "score": 1.0,
                "evidence": ["constant"],
            },
        )
        monitor = FactorMonitor(registry=registry, market_adapter=_StubMarket())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            snapshot = monitor.build_effectiveness_snapshot(force=True)
        item = next(entry for entry in snapshot["items"] if entry["factor_id"] == "constant_factor")
        self.assertEqual(item["status"], "unavailable")
        self.assertFalse(any(issubclass(warning.category, RuntimeWarning) for warning in caught))

    def test_kline_cache_and_freshness_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            adapter = MockMarketDataAdapter()
            cache = KlineCache(Path(tmp_dir) / "kline")
            first = cache.get_or_fetch("600519.SH", "1d", 5, adapter)
            second = cache.get_or_fetch("600519.SH", "1d", 5, adapter)
            self.assertTrue(first)
            self.assertTrue(second)
            stats = cache.stats()
            self.assertGreaterEqual(stats["hits"], 1)
            self.assertGreaterEqual(stats["misses"], 1)

            freshness_monitor = DataFreshnessMonitor(adapter)
            gateway = freshness_monitor.check_gateway_health()
            coverage = freshness_monitor.check_universe_coverage()
            self.assertEqual(gateway["status"], "not_applicable")
            self.assertEqual(coverage["status"], "degraded")

    def test_kline_freshness_treats_last_completed_trade_day_as_fresh_before_close(self) -> None:
        class _LatestFridayBarsMarket:
            def get_main_board_universe(self) -> list[str]:
                return ["000001.SZ"]

            def get_bars(
                self,
                symbols: list[str],
                period: str,
                count: int = 1,
                end_time: str | None = None,
            ) -> list[BarSnapshot]:
                return [
                    BarSnapshot(
                        symbol="000001.SZ",
                        period="1d",
                        open=11.09,
                        high=11.11,
                        low=10.99,
                        close=11.01,
                        volume=722530,
                        amount=797678405.0,
                        trade_time="2026-04-17 00:00:00",
                        pre_close=11.09,
                    )
                ]

        freshness_monitor = DataFreshnessMonitor(
            _LatestFridayBarsMarket(),
            now_factory=lambda: datetime(2026, 4, 20, 9, 27, 0),
        )
        payload = freshness_monitor.check_kline_freshness(["000001.SZ"])
        self.assertEqual(payload["expected_trade_date"], "2026-04-17")
        self.assertEqual(payload["latest_trade_time"], "2026-04-17 00:00:00")
        self.assertEqual(payload["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
