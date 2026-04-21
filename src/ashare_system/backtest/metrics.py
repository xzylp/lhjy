"""回测绩效指标 — 夏普/回撤/卡尔玛。

注意：
- 本模块补充的是 offline_backtest 维度指标，只用于离线回测样本统计。
- 这里的分战法/分市场状态/分退出原因指标不是线上事实归因，不应替代 learning attribution。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


OFFLINE_BACKTEST_METRICS_NOTE = (
    "这是 offline_backtest metrics，用于离线回测绩效拆分，不代表线上真实成交后的事实归因。"
)


@dataclass
class BacktestMetrics:
    metrics_scope: str = "offline_backtest_metrics"
    semantics_note: str = OFFLINE_BACKTEST_METRICS_NOTE
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    benchmark_return: float = 0.0
    portfolio_beta: float = 0.0
    alpha: float = 0.0
    tracking_error: float = 0.0
    information_ratio: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    # offline_backtest 维度指标（仅用于离线样本统计，不是线上事实归因）
    by_playbook_metrics: list[dict] = field(default_factory=list)
    by_regime_metrics: list[dict] = field(default_factory=list)
    by_exit_reason_metrics: list[dict] = field(default_factory=list)
    win_rate_by_playbook: list[dict] = field(default_factory=list)
    avg_return_by_regime: list[dict] = field(default_factory=list)
    exit_reason_distribution: list[dict] = field(default_factory=list)
    calmar_by_playbook: list[dict] = field(default_factory=list)
    active_return_attribution: list[dict] = field(default_factory=list)
    self_improvement_inputs: dict = field(default_factory=dict)
    export_payload: dict = field(default_factory=dict)

    def by_playbook(self) -> list[dict]:
        return list(self.by_playbook_metrics)

    def by_regime(self) -> list[dict]:
        return list(self.by_regime_metrics)

    def by_exit_reason(self) -> list[dict]:
        return list(self.by_exit_reason_metrics)


class MetricsCalculator:
    """回测绩效计算器"""

    TRADING_DAYS = 252
    RISK_FREE_RATE = 0.02  # 年化无风险利率

    def calc(
        self,
        equity_curve: pd.Series,
        trades: list[dict],
        *,
        benchmark_curve: pd.Series | None = None,
        sector_map: dict[str, str] | None = None,
    ) -> BacktestMetrics:
        trade_results = [self._trade_result_value(item) for item in trades]
        wins = [value for value in trade_results if value > 0]
        losses = [value for value in trade_results if value <= 0]
        by_playbook_metrics = self._build_dimension_metrics(trades, "playbook")
        by_regime_metrics = self._build_dimension_metrics(trades, "regime")
        by_exit_reason_metrics = self._build_dimension_metrics(trades, "exit_reason", include_ratio=True)
        win_rate_by_playbook = self._build_win_rate_by_playbook(by_playbook_metrics)
        avg_return_by_regime = self._build_avg_return_by_regime(by_regime_metrics)
        exit_reason_distribution = self._build_exit_reason_distribution(by_exit_reason_metrics)
        calmar_by_playbook = self._build_calmar_by_playbook(by_playbook_metrics)
        self_improvement_inputs = self._build_self_improvement_inputs(
            by_playbook_metrics=by_playbook_metrics,
            by_regime_metrics=by_regime_metrics,
            by_exit_reason_metrics=by_exit_reason_metrics,
        )

        total_return = 0.0
        annual_return = 0.0
        sharpe = 0.0
        max_dd = 0.0
        calmar = 0.0
        benchmark_return = 0.0
        portfolio_beta = 0.0
        alpha = 0.0
        tracking_error = 0.0
        information_ratio = 0.0
        win_rate = len(wins) / len(trades) if trades else 0.0
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = abs(np.mean(losses)) if losses else 1e-9
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

        if not equity_curve.empty and len(equity_curve) >= 2:
            returns = equity_curve.pct_change().dropna()
            total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0]) - 1
            n_days = len(equity_curve)
            annual_return = (1 + total_return) ** (self.TRADING_DAYS / n_days) - 1

            # 夏普比率
            daily_rf = self.RISK_FREE_RATE / self.TRADING_DAYS
            excess = returns - daily_rf
            sharpe = float(excess.mean() / excess.std() * np.sqrt(self.TRADING_DAYS)) if excess.std() > 0 else 0.0

            # 最大回撤
            peak = equity_curve.cummax()
            drawdown = (equity_curve - peak) / peak
            max_dd = float(drawdown.min())

            # 卡尔玛比率
            calmar = annual_return / abs(max_dd) if max_dd != 0 else 0.0

            if benchmark_curve is not None and not benchmark_curve.empty:
                benchmark = benchmark_curve.copy()
                benchmark.index = pd.to_datetime(benchmark.index)
                eq = equity_curve.copy()
                eq.index = pd.to_datetime(eq.index)
                aligned = pd.concat(
                    [
                        eq.pct_change().rename("portfolio"),
                        benchmark.pct_change().rename("benchmark"),
                    ],
                    axis=1,
                ).dropna()
                if not aligned.empty:
                    benchmark_return = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1.0) if len(benchmark) >= 2 else 0.0
                    benchmark_var = float(aligned["benchmark"].var() or 0.0)
                    portfolio_beta = float(aligned["portfolio"].cov(aligned["benchmark"]) / benchmark_var) if benchmark_var > 0 else 0.0
                    alpha = total_return - portfolio_beta * benchmark_return
                    active_return = aligned["portfolio"] - aligned["benchmark"]
                    tracking_error = float(active_return.std() * np.sqrt(self.TRADING_DAYS)) if float(active_return.std() or 0.0) > 0 else 0.0
                    information_ratio = float(active_return.mean() / active_return.std() * np.sqrt(self.TRADING_DAYS)) if float(active_return.std() or 0.0) > 0 else 0.0

        active_return_attribution = self._build_active_return_attribution(trades, sector_map or {}, benchmark_return)

        export_payload = {
            "metrics_scope": "offline_backtest_metrics",
            "semantics_note": OFFLINE_BACKTEST_METRICS_NOTE,
            "overview": {
                "total_trades": len(trades),
                "winning_trades": len(wins),
                "losing_trades": len(losses),
                "win_rate": round(float(win_rate), 6),
                "total_return": round(float(total_return), 6),
                "annual_return": round(float(annual_return), 6),
                "sharpe_ratio": round(float(sharpe), 6),
                "max_drawdown": round(float(max_dd), 6),
                "calmar_ratio": round(float(calmar), 6),
                "benchmark_return": round(float(benchmark_return), 6),
                "portfolio_beta": round(float(portfolio_beta), 6),
                "alpha": round(float(alpha), 6),
                "tracking_error": round(float(tracking_error), 6),
                "information_ratio": round(float(information_ratio), 6),
            },
            "self_improvement_inputs": self_improvement_inputs,
            "by_playbook": by_playbook_metrics,
            "by_regime": by_regime_metrics,
            "by_exit_reason": by_exit_reason_metrics,
            "active_return_attribution": active_return_attribution,
        }

        return BacktestMetrics(
            total_return=total_return,
            annual_return=annual_return,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            calmar_ratio=calmar,
            win_rate=win_rate,
            profit_loss_ratio=pl_ratio,
            benchmark_return=benchmark_return,
            portfolio_beta=portfolio_beta,
            alpha=alpha,
            tracking_error=tracking_error,
            information_ratio=information_ratio,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            by_playbook_metrics=by_playbook_metrics,
            by_regime_metrics=by_regime_metrics,
            by_exit_reason_metrics=by_exit_reason_metrics,
            win_rate_by_playbook=win_rate_by_playbook,
            avg_return_by_regime=avg_return_by_regime,
            exit_reason_distribution=exit_reason_distribution,
            calmar_by_playbook=calmar_by_playbook,
            active_return_attribution=active_return_attribution,
            self_improvement_inputs=self_improvement_inputs,
            export_payload=export_payload,
        )

    @staticmethod
    def _build_active_return_attribution(
        trades: list[dict],
        sector_map: dict[str, str],
        benchmark_return: float,
    ) -> list[dict]:
        grouped: dict[str, list[float]] = {}
        for item in trades:
            symbol = str(item.get("symbol") or "")
            sector = str(item.get("sector") or sector_map.get(symbol) or "unknown")
            grouped.setdefault(sector, []).append(float(item.get("return_pct", item.get("pnl", 0.0)) or 0.0))
        results: list[dict] = []
        for sector, values in grouped.items():
            avg_return = sum(values) / max(len(values), 1)
            results.append(
                {
                    "sector": sector,
                    "trade_count": len(values),
                    "avg_return_pct": round(avg_return, 6),
                    "active_return_pct": round(avg_return - benchmark_return, 6),
                }
            )
        results.sort(key=lambda item: (-item["active_return_pct"], -item["trade_count"], item["sector"]))
        return results

    def _build_dimension_metrics(
        self,
        trades: list[dict],
        dimension: str,
        *,
        include_ratio: bool = False,
    ) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        total = len(trades)
        for item in trades:
            key = str(item.get(dimension) or self._default_dimension_key(dimension))
            groups.setdefault(key, []).append(item)
        result: list[dict] = []
        for key, items in groups.items():
            trade_values = [self._trade_result_value(item) for item in items]
            returns = [value for value in (self._trade_return_pct(item) for item in items) if value is not None]
            wins = sum(1 for value in trade_values if value > 0)
            losses = sum(1 for value in trade_values if value < 0)
            flat = len(items) - wins - losses
            avg_win = float(np.mean([value for value in trade_values if value > 0])) if wins else 0.0
            avg_loss_base = [abs(value) for value in trade_values if value < 0]
            avg_loss = float(np.mean(avg_loss_base)) if avg_loss_base else 1e-9
            profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
            total_return_pct = round(float(np.sum(returns)), 6) if returns else 0.0
            avg_return_pct = round(total_return_pct / len(returns), 6) if returns else 0.0
            annualized_return = 0.0
            max_drawdown = 0.0
            calmar_ratio = 0.0
            if returns:
                curve = np.cumprod([1.0 + value for value in returns])
                compounded_return = float(curve[-1] - 1.0)
                annualized_return = (
                    (1 + compounded_return) ** (self.TRADING_DAYS / len(returns)) - 1
                    if returns
                    else 0.0
                )
                max_drawdown = self._max_drawdown(curve)
                calmar_ratio = annualized_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
            payload = {
                "key": key,
                "trade_count": len(items),
                "winning_trades": wins,
                "losing_trades": losses,
                "flat_trades": flat,
                "win_rate": round(wins / len(items), 6) if items else 0.0,
                "avg_return_pct": avg_return_pct,
                "total_return_pct": total_return_pct,
                "profit_loss_ratio": round(float(profit_loss_ratio), 6),
                "annual_return": round(float(annualized_return), 6),
                "max_drawdown": round(float(max_drawdown), 6),
                "calmar_ratio": round(float(calmar_ratio), 6),
                "sample_symbols": list(dict.fromkeys(str(item.get("symbol") or "") for item in items if item.get("symbol")))[:5],
            }
            if include_ratio:
                payload["ratio"] = round(len(items) / total, 6) if total else 0.0
            result.append(payload)
        result.sort(key=lambda item: (-item["trade_count"], -item["avg_return_pct"], item["key"]))
        return result

    @staticmethod
    def _build_win_rate_by_playbook(items: list[dict]) -> list[dict]:
        return [
            {
                "key": item["key"],
                "trade_count": item["trade_count"],
                "winning_trades": item["winning_trades"],
                "losing_trades": item["losing_trades"],
                "win_rate": item["win_rate"],
            }
            for item in items
        ]

    @staticmethod
    def _build_avg_return_by_regime(items: list[dict]) -> list[dict]:
        return [
            {
                "key": item["key"],
                "trade_count": item["trade_count"],
                "avg_return_pct": item["avg_return_pct"],
                "total_return_pct": item["total_return_pct"],
            }
            for item in items
        ]

    @staticmethod
    def _build_exit_reason_distribution(items: list[dict]) -> list[dict]:
        return [
            {
                "key": item["key"],
                "trade_count": item["trade_count"],
                "ratio": item.get("ratio", 0.0),
                "win_rate": item["win_rate"],
                "avg_return_pct": item["avg_return_pct"],
            }
            for item in items
        ]

    @staticmethod
    def _build_calmar_by_playbook(items: list[dict]) -> list[dict]:
        return [
            {
                "key": item["key"],
                "trade_count": item["trade_count"],
                "total_return_pct": item["total_return_pct"],
                "annual_return": item["annual_return"],
                "max_drawdown": item["max_drawdown"],
                "calmar_ratio": item["calmar_ratio"],
            }
            for item in items
        ]

    @staticmethod
    def _build_self_improvement_inputs(
        *,
        by_playbook_metrics: list[dict],
        by_regime_metrics: list[dict],
        by_exit_reason_metrics: list[dict],
    ) -> dict:
        def _weakest(items: list[dict]) -> dict:
            if not items:
                return {}
            return min(
                items,
                key=lambda item: (
                    item.get("avg_return_pct", 0.0),
                    item.get("total_return_pct", 0.0),
                    -item.get("trade_count", 0),
                    str(item.get("key", "")),
                ),
            )

        weakest_by_dimension = {
            "playbook": _weakest(by_playbook_metrics),
            "regime": _weakest(by_regime_metrics),
            "exit_reason": _weakest(by_exit_reason_metrics),
        }
        weak_dimensions = [
            {
                "dimension": dimension,
                "key": bucket.get("key", ""),
                "trade_count": bucket.get("trade_count", 0),
                "avg_return_pct": bucket.get("avg_return_pct", 0.0),
            }
            for dimension, bucket in weakest_by_dimension.items()
            if bucket
        ]
        weak_dimensions.sort(key=lambda item: (item["avg_return_pct"], -item["trade_count"], item["dimension"]))
        return {
            "proposal_scope": "offline_self_improvement_inputs",
            "metrics_scope": "offline_backtest_metrics",
            "live_promotion_allowed": False,
            "weakest_by_dimension": weakest_by_dimension,
            "weak_dimensions": weak_dimensions,
        }

    @staticmethod
    def _default_dimension_key(dimension: str) -> str:
        if dimension == "playbook":
            return "unassigned"
        if dimension == "exit_reason":
            return "unlabeled"
        return "unknown"

    @staticmethod
    def _trade_return_pct(item: dict) -> float | None:
        value = item.get("return_pct")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _trade_result_value(self, item: dict) -> float:
        return_pct = self._trade_return_pct(item)
        if return_pct is not None:
            return return_pct
        try:
            return float(item.get("pnl", 0.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _max_drawdown(curve: np.ndarray) -> float:
        if curve.size == 0:
            return 0.0
        peak = np.maximum.accumulate(curve)
        drawdown = (curve - peak) / peak
        return float(np.min(drawdown))
