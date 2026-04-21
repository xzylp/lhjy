"""因子有效性监控。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

import pandas as pd

from ..data.adjust import fill_suspended_days, mark_adjustment_flags
from ..infra.audit_store import StateStore
from .factor_registry import FactorRegistry


@dataclass
class FactorMonitorConfig:
    lookback_days: int = 20
    lookback_periods: list[int] | None = None
    universe_limit: int = 12
    min_cross_section: int = 4
    ic_threshold: float = 0.05
    significance_level: float = 0.1


class FactorMonitor:
    _UNSUPPORTED_MONITOR_GROUPS = {
        "micro_structure",
        "event_catalyst",
        "macro_environment",
        "position_management",
        "chip_distribution",
        "cross_market",
        "alternative_data",
        "growth_quality",
    }
    _UNSUPPORTED_MONITOR_TAGS = {
        "high_freq",
        "intraday",
        "order_book",
        "opening",
        "counterparty",
    }

    def __init__(
        self,
        registry: FactorRegistry,
        market_adapter: Any,
        state_store: StateStore | None = None,
        config: FactorMonitorConfig | None = None,
    ) -> None:
        self._registry = registry
        self._market = market_adapter
        self._state_store = state_store
        self._config = config or FactorMonitorConfig()
        if not self._config.lookback_periods:
            self._config.lookback_periods = [20, 60]

    def build_effectiveness_snapshot(self, trade_date: str | None = None, force: bool = False) -> dict[str, Any]:
        cache_key = f"factor_effectiveness:{trade_date or 'latest'}"
        if self._state_store and not force:
            cached = self._state_store.get(cache_key, {})
            if isinstance(cached, dict) and cached.get("items"):
                return cached
        previous_items_map: dict[str, dict[str, Any]] = {}
        if self._state_store:
            previous_snapshot = self._state_store.get(cache_key, {}) or self._state_store.get("factor_effectiveness:latest", {})
            if isinstance(previous_snapshot, dict):
                previous_items_map = {
                    str(item.get("factor_id") or "").strip(): dict(item)
                    for item in list(previous_snapshot.get("items") or [])
                    if str(item.get("factor_id") or "").strip()
                }
        universe = list(dict.fromkeys(self._market.get_main_board_universe()[: self._config.universe_limit]))
        items: list[dict[str, Any]] = []
        bars_map = self._load_price_data(universe, trade_date=trade_date)
        for definition in self._registry.list_all():
            definition_group = str(getattr(definition, "group", "") or "")
            definition_tags = {
                str(tag).strip()
                for tag in list(getattr(definition, "tags", []) or [])
                if str(tag).strip()
            }
            if (
                definition_group in self._UNSUPPORTED_MONITOR_GROUPS
                or definition_tags.intersection(self._UNSUPPORTED_MONITOR_TAGS)
            ):
                items.append(
                    self._decorate_monitor_item(
                        {
                            "factor_id": definition.id,
                            "name": definition.name,
                            "mean_rank_ic": 0.0,
                            "p_value": 1.0,
                            "sample_count": 0,
                            "status": "unsupported_for_monitor",
                            "monitor_mode": "outcome_attribution_only",
                            "ic_by_period": {
                                f"{lookback}d": {"mean_ic": 0.0, "p_value": 1.0, "sample_count": 0}
                                for lookback in list(self._config.lookback_periods or [self._config.lookback_days])
                            },
                        },
                        previous_items_map.get(definition.id),
                    )
                )
                continue
            ic_by_period: dict[str, dict[str, Any]] = {}
            period_results: list[dict[str, Any]] = []
            for lookback in list(self._config.lookback_periods or [self._config.lookback_days]):
                ic_series = self._compute_factor_ic_series(
                    definition.id,
                    bars_map,
                    trade_date=trade_date,
                    lookback_days=max(int(lookback or 0), 1),
                )
                if not ic_series:
                    ic_by_period[f"{lookback}d"] = {
                        "mean_ic": 0.0,
                        "p_value": 1.0,
                        "sample_count": 0,
                    }
                    continue
                ic_values = pd.Series(ic_series, dtype=float)
                mean_ic = float(ic_values.mean() or 0.0)
                std_ic = float(ic_values.std(ddof=1) or 0.0)
                sample_count = int(ic_values.count())
                p_value = self._approx_p_value(mean_ic, std_ic, sample_count)
                effective = abs(mean_ic) >= self._config.ic_threshold and p_value <= self._config.significance_level
                summary = {
                    "mean_ic": round(mean_ic, 4),
                    "p_value": round(p_value, 4),
                    "sample_count": sample_count,
                    "effective": effective,
                }
                ic_by_period[f"{lookback}d"] = summary
                period_results.append(summary)
            if not period_results:
                items.append(
                    self._decorate_monitor_item(
                        {
                            "factor_id": definition.id,
                            "name": definition.name,
                            "mean_rank_ic": 0.0,
                            "p_value": 1.0,
                            "sample_count": 0,
                            "status": "unavailable",
                            "monitor_mode": "cross_sectional_rank_ic_unavailable",
                            "ic_by_period": ic_by_period,
                        },
                        previous_items_map.get(definition.id),
                    )
                )
                continue
            effective = any(bool(item.get("effective")) for item in period_results)
            selected = sorted(
                period_results,
                key=lambda item: (
                    bool(item.get("effective")),
                    abs(float(item.get("mean_ic", 0.0) or 0.0)),
                    int(item.get("sample_count", 0) or 0),
                ),
                reverse=True,
            )[0]
            items.append(
                self._decorate_monitor_item(
                    {
                        "factor_id": definition.id,
                        "name": definition.name,
                        "mean_rank_ic": round(float(selected.get("mean_ic", 0.0) or 0.0), 4),
                        "p_value": round(float(selected.get("p_value", 1.0) or 1.0), 4),
                        "sample_count": int(selected.get("sample_count", 0) or 0),
                        "status": "effective" if effective else "ineffective",
                        "monitor_mode": "cross_sectional_rank_ic",
                        "ic_by_period": ic_by_period,
                    },
                    previous_items_map.get(definition.id),
                )
            )
        payload = {
            "generated_at": datetime.now().isoformat(),
            "trade_date": trade_date,
            "lookback_days": self._config.lookback_days,
            "lookback_periods": list(self._config.lookback_periods or []),
            "items": items,
        }
        if self._state_store:
            self._state_store.set(cache_key, payload)
            self._state_store.set("factor_effectiveness:latest", payload)
        return payload

    def _decorate_monitor_item(self, item: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        status = str(item.get("status") or "unknown")
        previous = dict(previous or {})
        previous_status = str(previous.get("status") or "unknown")
        consecutive_invalid_days = int(previous.get("consecutive_invalid_days", 0) or 0)
        consecutive_valid_days = int(previous.get("consecutive_valid_days", 0) or 0)
        if status == "ineffective":
            consecutive_invalid_days = consecutive_invalid_days + 1 if previous_status == "ineffective" else 1
            consecutive_valid_days = 0
        elif status == "effective":
            consecutive_valid_days = consecutive_valid_days + 1 if previous_status == "effective" else 1
            consecutive_invalid_days = 0
        else:
            consecutive_invalid_days = 0
            consecutive_valid_days = 0
        item["consecutive_invalid_days"] = consecutive_invalid_days
        item["consecutive_valid_days"] = consecutive_valid_days
        item["previous_status"] = previous_status
        return item

    def _load_price_data(self, symbols: list[str], *, trade_date: str | None) -> dict[str, pd.DataFrame]:
        max_lookback = max(list(self._config.lookback_periods or [self._config.lookback_days]) or [self._config.lookback_days])
        bars = self._market.get_bars(symbols, period="1d", count=max(max_lookback + 25, 90), end_time=trade_date)
        frames: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        for bar in bars:
            frames.setdefault(bar.symbol, []).append(
                {
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                    "amount": float(bar.amount),
                    "pre_close": float(bar.pre_close or 0.0),
                    "trade_time": str(bar.trade_time)[:10],
                }
            )
        result: dict[str, pd.DataFrame] = {}
        for symbol, rows in frames.items():
            if len(rows) < max_lookback + 5:
                continue
            frame = pd.DataFrame(rows).drop_duplicates(subset=["trade_time"]).sort_values("trade_time")
            frame.index = frame["trade_time"]
            frame = fill_suspended_days(frame)
            frame = mark_adjustment_flags(frame)
            result[symbol] = frame
        return result

    def _compute_factor_ic_series(
        self,
        factor_id: str,
        bars_map: dict[str, pd.DataFrame],
        *,
        trade_date: str | None,
        lookback_days: int,
    ) -> list[float]:
        if not bars_map:
            return []
        dates = sorted(set.intersection(*(set(frame.index[-lookback_days:]) for frame in bars_map.values() if not frame.empty)))
        ic_values: list[float] = []
        for current_date in dates[:-1]:
            scores: dict[str, float] = {}
            next_returns: dict[str, float] = {}
            for symbol, frame in bars_map.items():
                if current_date not in frame.index:
                    continue
                idx = frame.index.get_loc(current_date)
                if isinstance(idx, slice) or idx + 1 >= len(frame):
                    continue
                if bool(frame.iloc[idx].get("is_suspended", False)):
                    continue
                window = frame.iloc[: idx + 1].tail(90).copy()
                candidate = {
                    "symbol": symbol,
                    "rank": 1,
                    "selection_score": 0.0,
                    "resolved_sector": "",
                    "_disable_live_bar_fetch": True,
                    "_preloaded_bar_frames": {"1d": window},
                    "market_snapshot": {
                        "last_price": float(window["close"].iloc[-1]),
                        "pre_close": float(window["pre_close"].iloc[-1] or window["close"].iloc[-2]),
                        "volume": float(window["volume"].iloc[-1]),
                    },
                    "score_breakdown": {"pb_ratio": 1.5},
                }
                evaluation = self._registry.evaluate(
                    factor_id,
                    version="v1",
                    candidate=candidate,
                    context={},
                    market_adapter=self._market,
                    trade_date=current_date,
                )
                if "因子当前不可用" in " ".join(str(item) for item in evaluation.get("evidence", [])):
                    continue
                scores[symbol] = float(evaluation.get("score", 0.0) or 0.0)
                today_close = float(frame["close"].iloc[idx])
                next_close = float(frame["close"].iloc[idx + 1])
                next_returns[symbol] = (next_close - today_close) / max(today_close, 1e-9)
            if len(scores) < self._config.min_cross_section:
                continue
            score_series = pd.Series(scores)
            return_series = pd.Series(next_returns).reindex(score_series.index).dropna()
            score_series = score_series.reindex(return_series.index)
            if len(return_series) < self._config.min_cross_section:
                continue
            # 常数序列做相关系数会触发 numpy RuntimeWarning，也没有统计意义。
            if score_series.nunique(dropna=True) < 2 or return_series.nunique(dropna=True) < 2:
                continue
            corr = score_series.rank().corr(return_series.rank())
            if corr is not None and not math.isnan(float(corr)):
                ic_values.append(float(corr))
        return ic_values

    def _approx_p_value(self, mean_ic: float, std_ic: float, sample_count: int) -> float:
        if sample_count <= 2 or std_ic <= 1e-9:
            return 1.0
        t_stat = abs(mean_ic) / (std_ic / math.sqrt(sample_count))
        degrees_of_freedom = max(sample_count - 1, 1)
        corrected_t = t_stat * (1.0 - 1.0 / max(4.0 * degrees_of_freedom, 1.0))
        return max(min(math.erfc(corrected_t / math.sqrt(2.0)), 1.0), 0.0)
