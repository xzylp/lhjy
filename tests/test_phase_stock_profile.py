import pandas as pd

from ashare_system.strategy.stock_profile import StockProfileBuilder


class TestStockProfileBuilder:
    def test_build_profile_from_history(self):
        history = pd.DataFrame(
            [
                {
                    "is_zt": True,
                    "seal_success": True,
                    "bombed": False,
                    "afternoon_resealed": True,
                    "next_day_return": 0.04,
                    "return_day_1": 0.04,
                    "return_day_2": 0.08,
                    "return_day_3": 0.03,
                    "sector_rank": 1,
                    "is_leader": True,
                },
                {
                    "is_zt": True,
                    "seal_success": True,
                    "bombed": False,
                    "afternoon_resealed": False,
                    "next_day_return": 0.03,
                    "return_day_1": 0.03,
                    "return_day_2": 0.07,
                    "return_day_3": 0.02,
                    "sector_rank": 2,
                    "is_leader": True,
                },
                {
                    "is_zt": True,
                    "seal_success": False,
                    "bombed": True,
                    "afternoon_resealed": False,
                    "next_day_return": -0.01,
                    "return_day_1": -0.01,
                    "return_day_2": 0.01,
                    "return_day_3": 0.00,
                    "sector_rank": 5,
                    "is_leader": False,
                },
            ]
        )
        profile = StockProfileBuilder().build("300001.SZ", history)
        assert profile.symbol == "300001.SZ"
        assert profile.board_success_rate_20d == 0.6667
        assert profile.bomb_rate_20d == 0.3333
        assert profile.optimal_hold_days == 2
        assert profile.style_tag == "leader"
        assert profile.leader_frequency_30d == 0.6667
