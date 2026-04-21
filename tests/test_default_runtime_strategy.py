from __future__ import annotations

import unittest

from ashare_system.contracts import MarketProfile, SectorProfile, StockBehaviorProfile
from ashare_system.strategy.default_runtime_strategy import (
    apply_market_alignment_order,
    build_routing_market_profile,
    resolve_symbol_sector_map,
    score_runtime_snapshot,
)


class DefaultRuntimeStrategyTests(unittest.TestCase):
    def test_resolve_symbol_sector_map_prefers_dossier_resolved_sector_without_full_scan(self) -> None:
        class FailingMarketAdapter:
            def get_sectors(self) -> list[str]:
                raise AssertionError("不应退化到全市场板块扫描")

            def get_sector_symbols(self, sector_name: str) -> list[str]:
                raise AssertionError("不应请求板块成员接口")

        resolved = resolve_symbol_sector_map(
            [
                {
                    "symbol": "002361.SZ",
                    "resolved_sector": "中盘",
                    "symbol_context": {"sector_relative": {"sector_tags": []}},
                }
            ],
            ["002361.SZ"],
            FailingMarketAdapter(),
        )
        self.assertEqual(resolved["002361.SZ"], "中盘")

    def test_score_runtime_snapshot_prefers_active_momentum_over_cold_negative(self) -> None:
        hot = score_runtime_snapshot(
            symbol="300001.SZ",
            last_price=10.5,
            pre_close=10.0,
            volume=8_000_000,
            rank=1,
        )
        cold = score_runtime_snapshot(
            symbol="000002.SZ",
            last_price=9.82,
            pre_close=10.0,
            volume=12_000_000,
            rank=1,
        )
        self.assertGreater(hot["selection_score"], cold["selection_score"])
        self.assertEqual(hot["action"], "BUY")
        self.assertEqual(cold["action"], "HOLD")

    def test_build_routing_market_profile_adds_probe_playbooks_when_hot_chain_exists(self) -> None:
        profile = MarketProfile(
            sentiment_phase="回暖",
            sentiment_score=48.0,
            regime="chaos",
            regime_score=0.1,
            allowed_playbooks=[],
            hot_sectors=["机器人", "算力"],
        )
        sector_profiles = [
            SectorProfile(
                sector_name="机器人",
                life_cycle="ferment",
                strength_score=0.68,
                zt_count=3,
                up_ratio=1.0,
                breadth_score=0.3,
                reflow_score=0.52,
                leader_symbols=["300001.SZ", "300024.SZ"],
                active_days=2,
                zt_count_delta=1,
            )
        ]
        routed, meta = build_routing_market_profile(profile, sector_profiles)
        self.assertTrue(meta["allow_probe_routing"])
        self.assertEqual(meta["routing_regime"], "rotation")
        self.assertEqual(routed.regime, "rotation")
        self.assertIn("sector_reflow_first_board", routed.allowed_playbooks)
        self.assertIn("divergence_reseal", routed.allowed_playbooks)

    def test_apply_market_alignment_order_demotes_cold_non_mainline_symbol(self) -> None:
        decisions = [
            {"symbol": "300001.SZ", "selection_score": 75.0, "resolved_sector": "机器人", "summary": "热点龙头"},
            {"symbol": "000002.SZ", "selection_score": 68.0, "resolved_sector": "地产", "summary": "普通候选"},
        ]
        ordered = apply_market_alignment_order(
            decisions,
            pack_items=[
                {
                    "symbol": "300001.SZ",
                    "symbol_context": {
                        "market_relative": {"relative_strength_pct": 0.03},
                        "sector_relative": {"sector_event_count": 3},
                    },
                },
                {
                    "symbol": "000002.SZ",
                    "symbol_context": {
                        "market_relative": {"relative_strength_pct": -0.02},
                        "sector_relative": {"sector_event_count": 0},
                    },
                },
            ],
            playbook_contexts=[
                {"symbol": "300001.SZ", "playbook": "leader_chase", "confidence": 0.8, "leader_score": 0.9},
            ],
            behavior_profiles={
                "300001.SZ": StockBehaviorProfile(
                    symbol="300001.SZ",
                    board_success_rate_20d=0.72,
                    bomb_rate_20d=0.12,
                    next_day_premium_20d=0.03,
                    reseal_rate_20d=0.4,
                    optimal_hold_days=1,
                    style_tag="leader",
                    avg_sector_rank_30d=1.0,
                    leader_frequency_30d=0.6,
                ),
                "000002.SZ": StockBehaviorProfile(
                    symbol="000002.SZ",
                    board_success_rate_20d=0.1,
                    bomb_rate_20d=0.55,
                    next_day_premium_20d=-0.02,
                    reseal_rate_20d=0.05,
                    optimal_hold_days=3,
                    style_tag="defensive",
                    avg_sector_rank_30d=5.0,
                    leader_frequency_30d=0.02,
                ),
            },
            sector_map={"300001.SZ": "机器人", "000002.SZ": "地产"},
            market_profile_payload={
                "hot_sectors": ["机器人"],
                "sector_profiles": [
                    {"sector_name": "机器人", "strength_score": 0.72},
                    {"sector_name": "地产", "strength_score": 0.25},
                ],
            },
        )
        self.assertEqual(ordered[0]["symbol"], "300001.SZ")
        self.assertEqual(ordered[-1]["symbol"], "000002.SZ")
        self.assertEqual(ordered[-1]["action"], "HOLD")
        self.assertIn("主线贴合度偏弱", ordered[-1]["summary"])


if __name__ == "__main__":
    unittest.main()
