"""个股股性画像构建。"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from ..contracts import StockBehaviorProfile


class StockProfileBuilder:
    """基于近 20 到 60 个交易日的稳定字段构建股性画像。"""

    WINDOW = 60

    def build(
        self,
        symbol: str,
        history: pd.DataFrame | Iterable[dict],
        baseline: StockBehaviorProfile | dict | None = None,
    ) -> StockBehaviorProfile:
        fallback = self._coerce_baseline(symbol, baseline)
        frame = self._ensure_frame(history)
        if frame.empty:
            return fallback or StockBehaviorProfile(symbol=symbol)

        frame = frame.tail(self.WINDOW).copy()
        zt_days = frame[self._bool_series(frame, "is_zt")]
        board_success_rate = self._mean_bool(
            zt_days,
            "seal_success",
            default=fallback.board_success_rate_20d if fallback is not None else 0.0,
        )
        bomb_rate = self._mean_bool(
            zt_days,
            "bombed",
            default=fallback.bomb_rate_20d if fallback is not None else 0.0,
        )
        reseal_rate = self._mean_bool(
            zt_days,
            "afternoon_resealed",
            default=fallback.reseal_rate_20d if fallback is not None else 0.0,
        )
        next_day_premium = self._mean_float(
            zt_days,
            "next_day_return",
            default=fallback.next_day_premium_20d if fallback is not None else 0.0,
        )
        avg_sector_rank = self._mean_float(
            frame.tail(30),
            "sector_rank",
            default=fallback.avg_sector_rank_30d if fallback is not None else 99.0,
        )
        leader_frequency = self._calc_leader_frequency(
            frame.tail(30),
            default=fallback.leader_frequency_30d if fallback is not None else 0.0,
        )
        optimal_hold = self._calc_optimal_hold(
            zt_days,
            default=fallback.optimal_hold_days if fallback is not None else 1,
        )
        style_tag = self._classify_style(
            board_success_rate,
            bomb_rate,
            reseal_rate,
            next_day_premium,
            leader_frequency,
        )
        if fallback is not None and zt_days.empty:
            style_tag = fallback.style_tag or style_tag

        return StockBehaviorProfile(
            symbol=symbol,
            board_success_rate_20d=round(board_success_rate, 4),
            bomb_rate_20d=round(bomb_rate, 4),
            next_day_premium_20d=round(next_day_premium, 4),
            reseal_rate_20d=round(reseal_rate, 4),
            optimal_hold_days=optimal_hold,
            style_tag=style_tag,
            avg_sector_rank_30d=round(avg_sector_rank, 4),
            leader_frequency_30d=round(leader_frequency, 4),
        )

    @staticmethod
    def _ensure_frame(history: pd.DataFrame | Iterable[dict]) -> pd.DataFrame:
        if isinstance(history, pd.DataFrame):
            return history.copy()
        return pd.DataFrame(list(history))

    @staticmethod
    def _bool_series(frame: pd.DataFrame, column: str) -> pd.Series:
        if column not in frame:
            return pd.Series([False] * len(frame), index=frame.index, dtype=bool)
        return frame[column].fillna(False).astype(bool)

    @staticmethod
    def _mean_bool(frame: pd.DataFrame, column: str, default: float = 0.0) -> float:
        if frame.empty or column not in frame:
            return default
        return float(frame[column].fillna(False).astype(bool).mean())

    @staticmethod
    def _mean_float(frame: pd.DataFrame, column: str, default: float = 0.0) -> float:
        if frame.empty or column not in frame:
            return default
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if values.empty:
            return default
        return float(values.mean())

    def _calc_leader_frequency(self, frame: pd.DataFrame, default: float = 0.0) -> float:
        if frame.empty:
            return default
        if "is_leader" in frame:
            return float(frame["is_leader"].fillna(False).astype(bool).mean())
        if "sector_rank" in frame:
            ranks = pd.to_numeric(frame["sector_rank"], errors="coerce").fillna(99)
            return float((ranks <= 3).mean())
        return default

    @staticmethod
    def _calc_optimal_hold(zt_days: pd.DataFrame, default: int = 1) -> int:
        if zt_days.empty:
            return max(int(default or 1), 1)
        candidates = {
            1: "return_day_1",
            2: "return_day_2",
            3: "return_day_3",
        }
        best_day = max(int(default or 1), 1)
        best_value = float("-inf")
        for day, column in candidates.items():
            if column not in zt_days:
                continue
            values = pd.to_numeric(zt_days[column], errors="coerce").dropna()
            if values.empty:
                continue
            value = float(values.mean())
            if value > best_value:
                best_value = value
                best_day = day
        return best_day

    @staticmethod
    def _coerce_baseline(symbol: str, baseline: StockBehaviorProfile | dict | None) -> StockBehaviorProfile | None:
        if baseline is None:
            return None
        if isinstance(baseline, StockBehaviorProfile):
            if baseline.symbol == symbol:
                return baseline
            return baseline.model_copy(update={"symbol": symbol})
        payload = dict(baseline)
        payload["symbol"] = symbol
        return StockBehaviorProfile.model_validate(payload)

    @staticmethod
    def _classify_style(
        board_success_rate: float,
        bomb_rate: float,
        reseal_rate: float,
        next_day_premium: float,
        leader_frequency: float,
    ) -> str:
        if board_success_rate >= 0.65 and leader_frequency >= 0.35:
            return "leader"
        if reseal_rate >= 0.35:
            return "reseal"
        if bomb_rate >= 0.45:
            return "defensive"
        if next_day_premium >= 0.02:
            return "momentum"
        return "mixed"
