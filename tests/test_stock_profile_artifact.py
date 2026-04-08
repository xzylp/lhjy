from __future__ import annotations

import unittest

from ashare_system.contracts import StockBehaviorProfile
from ashare_system.strategy.stock_profile import StockProfileBuilder


class StockProfileArtifactTests(unittest.TestCase):
    def test_build_reuses_baseline_when_history_empty(self) -> None:
        baseline = StockBehaviorProfile(
            symbol="600519.SH",
            board_success_rate_20d=0.71,
            bomb_rate_20d=0.12,
            next_day_premium_20d=0.031,
            reseal_rate_20d=0.22,
            optimal_hold_days=2,
            style_tag="leader",
            avg_sector_rank_30d=1.8,
            leader_frequency_30d=0.64,
        )

        profile = StockProfileBuilder().build("600519.SH", [], baseline=baseline)

        self.assertEqual(profile.model_dump(), baseline.model_dump())

    def test_build_keeps_baseline_signal_when_limit_up_samples_missing(self) -> None:
        baseline = StockBehaviorProfile(
            symbol="600519.SH",
            board_success_rate_20d=0.68,
            bomb_rate_20d=0.11,
            next_day_premium_20d=0.028,
            reseal_rate_20d=0.18,
            optimal_hold_days=2,
            style_tag="leader",
            avg_sector_rank_30d=2.4,
            leader_frequency_30d=0.55,
        )
        history = [
            {"is_zt": False, "sector_rank": 4, "is_leader": False},
            {"is_zt": False, "sector_rank": 3, "is_leader": True},
            {"is_zt": False, "sector_rank": 2, "is_leader": True},
        ]

        profile = StockProfileBuilder().build("600519.SH", history, baseline=baseline)

        self.assertEqual(profile.board_success_rate_20d, baseline.board_success_rate_20d)
        self.assertEqual(profile.bomb_rate_20d, baseline.bomb_rate_20d)
        self.assertEqual(profile.next_day_premium_20d, baseline.next_day_premium_20d)
        self.assertEqual(profile.optimal_hold_days, baseline.optimal_hold_days)
        self.assertEqual(profile.style_tag, baseline.style_tag)
        self.assertAlmostEqual(profile.avg_sector_rank_30d, 3.0)
        self.assertAlmostEqual(profile.leader_frequency_30d, 0.6667)


if __name__ == "__main__":
    unittest.main()
