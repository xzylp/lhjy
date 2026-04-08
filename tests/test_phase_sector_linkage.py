"""板块联动与板块行为因子测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_system.factors import FactorEngine, registry


def make_sector_frame(n: int = 40) -> pd.DataFrame:
    np.random.seed(7)
    close = 10 + np.linspace(0, 1.5, n)
    sector_close = 100 + np.linspace(0, 8, n)
    market_close = 3000 + np.linspace(0, 40, n)
    limit_up_price = close * 1.1
    high = np.maximum(close * 1.02, limit_up_price * 0.999)
    low = close * 0.97
    volume = np.linspace(1_000_000, 1_800_000, n)
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": high,
            "low": low,
            "close": close,
            "pre_close": pd.Series(close).shift(1).bfill(),
            "volume": volume,
            "amount": close * volume,
            "sector_close": sector_close,
            "market_close": market_close,
            "sector_volume": np.linspace(5_000_000, 9_000_000, n),
            "sector_advancers": np.linspace(8, 20, n),
            "sector_decliners": np.linspace(10, 4, n),
            "sector_limit_up_count": np.linspace(1, 6, n),
            "sector_constituent_count": np.full(n, 30),
            "sector_reseal_count": np.linspace(0, 3, n),
            "sector_bomb_count": np.linspace(3, 1, n),
            "leader_close": close * np.linspace(1.0, 1.12, n),
            "limit_up_price": limit_up_price,
        }
    )


class TestSectorLinkageRegistration:
    def test_sector_linkage_registers_minimum_factors(self) -> None:
        names = {
            "sector_relative_return_5d",
            "sector_relative_volume_5d",
            "sector_breadth_thrust",
            "sector_limit_up_ratio",
            "sector_reseal_ratio",
            "sector_leader_premium_5d",
            "sector_linkage_heat",
        }
        assert names.issubset(set(registry.names()))

    def test_board_behavior_registers_special_factors(self) -> None:
        names = {
            "board_limit_up_distance",
            "board_bomb_risk",
            "board_reseal_strength",
            "board_reseal_persistence",
            "board_limit_up_premium",
        }
        assert names.issubset(set(registry.names()))


class TestSectorLinkageFactorOutput:
    def test_sector_linkage_factor_outputs_series(self) -> None:
        engine = FactorEngine()
        df = make_sector_frame()
        result = engine.compute_one("sector_linkage_heat", df, normalize=False)
        assert result is not None
        assert len(result.values) == len(df)
        assert not result.values.iloc[-1:].isna().all()

    def test_board_behavior_factor_outputs_series(self) -> None:
        engine = FactorEngine()
        df = make_sector_frame()
        result = engine.compute_one("board_bomb_risk", df, normalize=False)
        assert result is not None
        assert len(result.values) == len(df)
        valid = result.values.dropna()
        assert set(valid.unique()).issubset({0.0, 1.0})
