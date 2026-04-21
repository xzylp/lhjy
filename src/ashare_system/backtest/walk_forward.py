"""Walk-forward 与 regime conditional 回测。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class WalkForwardWindowResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    in_sample_sharpe: float = 0.0
    out_of_sample_sharpe: float = 0.0
    in_sample_ic: float = 0.0
    out_of_sample_ic: float = 0.0
    decay_ratio: float = 0.0
    regime_label: str = "unknown"


@dataclass
class WalkForwardSummary:
    available: bool
    generated_at: str
    window_count: int
    windows: list[WalkForwardWindowResult] = field(default_factory=list)
    in_sample_sharpe: float = 0.0
    out_of_sample_sharpe: float = 0.0
    decay_ratio: float = 0.0
    regime_summary: list[dict[str, Any]] = field(default_factory=list)
    factor_decay: list[dict[str, Any]] = field(default_factory=list)


class WalkForwardValidator:
    """滚动窗口验证器。"""

    def run_factor_validation(
        self,
        *,
        factor_frame: pd.DataFrame,
        forward_return: pd.Series,
        train_window: int = 60,
        test_window: int = 20,
        regime_labels: pd.Series | None = None,
    ) -> WalkForwardSummary:
        normalized_factor = factor_frame.sort_index()
        normalized_return = forward_return.sort_index()
        common_index = normalized_factor.index.intersection(normalized_return.index)
        normalized_factor = normalized_factor.loc[common_index]
        normalized_return = normalized_return.loc[common_index]
        if len(common_index) < train_window + test_window + 1:
            return WalkForwardSummary(
                available=False,
                generated_at=pd.Timestamp.utcnow().isoformat(),
                window_count=0,
            )

        windows: list[WalkForwardWindowResult] = []
        factor_decay: list[dict[str, Any]] = []
        for start in range(0, len(common_index) - train_window - test_window + 1, test_window):
            train_index = common_index[start : start + train_window]
            test_index = common_index[start + train_window : start + train_window + test_window]
            train_factor = normalized_factor.loc[train_index]
            test_factor = normalized_factor.loc[test_index]
            train_return = normalized_return.loc[train_index]
            test_return = normalized_return.loc[test_index]
            in_ic = self._mean_rank_ic(train_factor, train_return)
            out_ic = self._mean_rank_ic(test_factor, test_return)
            in_sharpe = self._sharpe(train_return)
            out_sharpe = self._sharpe(test_return)
            regime_label = "unknown"
            if regime_labels is not None and not regime_labels.empty:
                test_regimes = regime_labels.loc[test_index].dropna()
                if not test_regimes.empty:
                    regime_label = str(test_regimes.mode().iloc[0])
            windows.append(
                WalkForwardWindowResult(
                    train_start=str(train_index[0])[:10],
                    train_end=str(train_index[-1])[:10],
                    test_start=str(test_index[0])[:10],
                    test_end=str(test_index[-1])[:10],
                    in_sample_sharpe=in_sharpe,
                    out_of_sample_sharpe=out_sharpe,
                    in_sample_ic=in_ic,
                    out_of_sample_ic=out_ic,
                    decay_ratio=(out_sharpe / in_sharpe) if abs(in_sharpe) > 1e-9 else 0.0,
                    regime_label=regime_label,
                )
            )

        for factor_name in normalized_factor.columns:
            in_values: list[float] = []
            out_values: list[float] = []
            for window in windows:
                train_mask = (normalized_factor.index >= window.train_start) & (normalized_factor.index <= window.train_end)
                test_mask = (normalized_factor.index >= window.test_start) & (normalized_factor.index <= window.test_end)
                in_values.append(self._rank_ic(normalized_factor.loc[train_mask, factor_name], normalized_return.loc[train_mask]))
                out_values.append(self._rank_ic(normalized_factor.loc[test_mask, factor_name], normalized_return.loc[test_mask]))
            mean_in = sum(in_values) / len(in_values) if in_values else 0.0
            mean_out = sum(out_values) / len(out_values) if out_values else 0.0
            factor_decay.append(
                {
                    "factor_id": factor_name,
                    "in_sample_ic": round(mean_in, 6),
                    "out_of_sample_ic": round(mean_out, 6),
                    "decay_ratio": round(mean_out / mean_in, 6) if abs(mean_in) > 1e-9 else 0.0,
                }
            )

        regime_summary = self._summarize_regime_windows(windows)
        in_sharpe = sum(item.in_sample_sharpe for item in windows) / len(windows)
        out_sharpe = sum(item.out_of_sample_sharpe for item in windows) / len(windows)
        return WalkForwardSummary(
            available=True,
            generated_at=pd.Timestamp.utcnow().isoformat(),
            window_count=len(windows),
            windows=windows,
            in_sample_sharpe=in_sharpe,
            out_of_sample_sharpe=out_sharpe,
            decay_ratio=(out_sharpe / in_sharpe) if abs(in_sharpe) > 1e-9 else 0.0,
            regime_summary=regime_summary,
            factor_decay=sorted(factor_decay, key=lambda item: item["decay_ratio"]),
        )

    @staticmethod
    def _mean_rank_ic(frame: pd.DataFrame, returns: pd.Series) -> float:
        if frame.empty or returns.empty:
            return 0.0
        values = [
            WalkForwardValidator._rank_ic(frame[column], returns)
            for column in frame.columns
        ]
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _rank_ic(factor: pd.Series, returns: pd.Series) -> float:
        joined = pd.concat([factor, returns], axis=1).dropna()
        if joined.empty:
            return 0.0
        ranked = joined.rank()
        return float(ranked.iloc[:, 0].corr(ranked.iloc[:, 1]) or 0.0)

    @staticmethod
    def _sharpe(returns: pd.Series) -> float:
        series = returns.dropna()
        if series.empty or float(series.std() or 0.0) <= 0:
            return 0.0
        return float(series.mean() / series.std() * (252 ** 0.5))

    @staticmethod
    def _summarize_regime_windows(windows: list[WalkForwardWindowResult]) -> list[dict[str, Any]]:
        grouped: dict[str, list[WalkForwardWindowResult]] = {}
        for item in windows:
            grouped.setdefault(item.regime_label, []).append(item)
        results: list[dict[str, Any]] = []
        for regime, items in grouped.items():
            sharpe_values = [float(item.out_of_sample_sharpe or 0.0) for item in items]
            results.append(
                {
                    "regime": regime,
                    "window_count": len(items),
                    "sharpe": round(sum(sharpe_values) / len(sharpe_values), 6) if sharpe_values else 0.0,
                    "mdd_proxy": round(min(float(item.out_of_sample_ic or 0.0) for item in items), 6) if items else 0.0,
                }
            )
        results.sort(key=lambda item: item["regime"])
        return results
