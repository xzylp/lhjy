"""因子注册表 - 为 runtime compose 提供可发现的因子菜单。"""

from __future__ import annotations

from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import re
from threading import Lock
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..contracts import OrderBookSnapshot
from ..factors.counterparty import counterparty_strength_from_bars
from ..factors.microstructure import large_order_flow, order_book_imbalance
from ..logging_config import get_logger
from .atomic_repository import StrategyRepositoryEntry, strategy_atomic_repository

logger = get_logger("strategy.factor_registry")

REGIME_FACTOR_MAP: dict[str, list[str]] = {
    "strong_rotation": ["turnover_acceleration", "sector_heat_score", "sector_leader_drive", "peer_follow_strength"],
    "sector_breakout": ["breakout_quality", "volume_breakout_confirmation", "sector_leader_drive", "main_fund_turning_point"],
    "index_rebound": ["oversold_bounce_strength", "support_reclaim_score", "momentum_slope", "market_breadth_index"],
    "weak_defense": ["dividend_support_score", "balance_sheet_safety", "low_volume_pullback_quality", "correlation_hedge_score"],
    "panic_sell": ["drawdown_repair_pressure", "balance_sheet_safety", "correlation_hedge_score", "liquidity_risk_penalty"],
}


class FactorDefinition(BaseModel):
    id: str
    name: str
    version: str = "v1"
    group: str
    correlation_group: str = ""
    description: str = ""
    params_schema: dict[str, Any] = Field(default_factory=dict)
    evidence_schema: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source: str = "seed"
    author: str = "system"
    status: str = "active"


class FactorRegistry:
    def __init__(self) -> None:
        self._factors: dict[str, FactorDefinition] = {}
        self._executors: dict[str, Callable[[FactorDefinition, dict[str, Any], dict[str, Any], Any | None, str | None, dict[str, Any] | None], dict[str, Any]]] = {}

    def register(
        self,
        definition: FactorDefinition,
        *,
        executor: Callable[[FactorDefinition, dict[str, Any], dict[str, Any], Any | None, str | None, dict[str, Any] | None], dict[str, Any]] | None = None,
    ) -> FactorDefinition:
        key = f"{definition.id}:{definition.version}"
        self._factors[key] = definition.model_copy()
        if executor is not None:
            self._executors[key] = executor
        if strategy_atomic_repository.get(definition.id, definition.version) is None:
            strategy_atomic_repository.register(
                StrategyRepositoryEntry(
                    id=definition.id,
                    name=definition.name,
                    type="factor",
                    status=definition.status,  # type: ignore[arg-type]
                    version=definition.version,
                    author=definition.author,
                    source=definition.source,
                    params_schema=definition.params_schema,
                    evidence_schema=definition.evidence_schema,
                    tags=definition.tags,
                    content={
                        "group": definition.group,
                        "correlation_group": definition.correlation_group,
                        "description": definition.description,
                    },
                )
            )
        logger.info("因子注册: %s", key)
        return definition.model_copy()

    def get(self, factor_id: str, version: str = "v1") -> FactorDefinition | None:
        item = self._factors.get(f"{factor_id}:{version}")
        return item.model_copy() if item else None

    def list_all(self) -> list[FactorDefinition]:
        return [item.model_copy() for item in self._factors.values()]

    def suggest_factors(
        self,
        *,
        market_hypothesis: str = "",
        market_regime: str = "",
        limit: int = 8,
        focus_sectors: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        text = " ".join([str(market_hypothesis or ""), " ".join(str(item) for item in list(focus_sectors or []))]).lower()
        ranked: list[tuple[float, FactorDefinition]] = []
        regime_ids = set(REGIME_FACTOR_MAP.get(str(market_regime or "").strip(), []))
        for definition in self.list_all():
            score = 0.0
            if definition.id in regime_ids:
                score += 8.0
            if any(tag.lower() in text for tag in list(definition.tags or [])):
                score += 2.0
            if str(definition.group or "").lower() in text:
                score += 1.5
            if any(keyword in text for keyword in ("热点", "扩散", "轮动")) and definition.group in {"sector_heat", "capital_behavior"}:
                score += 1.2
            if any(keyword in text for keyword in ("防守", "回撤", "避险")) and definition.group in {"valuation_filter", "risk_penalty", "position_management"}:
                score += 1.2
            if score <= 0:
                continue
            ranked.append((score, definition))
        ranked.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        return [
            {
                **definition.model_dump(),
                "suggestion_score": round(score, 4),
                "suggested_by_regime": definition.id in regime_ids,
            }
            for score, definition in ranked[: max(int(limit or 0), 1)]
        ]

    def evaluate(
        self,
        factor_id: str,
        *,
        version: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        market_adapter: Any | None = None,
        trade_date: str | None = None,
        precomputed_factors: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = f"{factor_id}:{version}"
        definition = self._factors.get(key)
        if definition is None:
            return {"score": 0.0, "evidence": [f"因子未注册: {factor_id}:{version}"]}
        executor = self._executors.get(key)
        if executor is None:
            return {"score": 0.0, "evidence": [f"因子缺少执行器: {factor_id}:{version}"]}
        return executor(definition.model_copy(), dict(candidate), dict(context), market_adapter, trade_date, precomputed_factors)


factor_registry = FactorRegistry()
_AKSHARE_CACHE_LOCK = Lock()
_AKSHARE_CACHE: dict[str, Any] = {}


def _selection_score(candidate: dict[str, Any]) -> float:
    return float(candidate.get("selection_score", 0.0) or 0.0)


def _score_breakdown(candidate: dict[str, Any]) -> dict[str, Any]:
    return dict(candidate.get("score_breakdown") or {})


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _factor_unavailable(definition: FactorDefinition, reason: str) -> dict[str, Any]:
    return {"score": 0.0, "evidence": [definition.name, f"因子当前不可用: {reason}"]}


def _resolve_timeframes(context: dict[str, Any], default_lookback: int) -> list[int]:
    params = dict(context.get("factor_params") or {})
    raw = list(params.get("timeframes") or [])
    if not raw:
        return [default_lookback]
    resolved: list[int] = []
    for item in raw:
        text = str(item).lower().strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            continue
        value = int(digits)
        if value > 0 and value not in resolved:
            resolved.append(value)
    return resolved or [default_lookback]


def _bars_to_frame(bars: list[Any]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    frame = pd.DataFrame(
        [
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
            for bar in bars
        ]
    )
    frame = frame.drop_duplicates(subset=["trade_time"]).sort_values("trade_time")
    frame.index = frame["trade_time"]
    return frame


def _load_bar_frame(
    candidate: dict[str, Any],
    market_adapter: Any | None,
    trade_date: str | None,
    *,
    period: str,
    count: int,
) -> pd.DataFrame:
    prefetched_frames = dict(candidate.get("_preloaded_bar_frames") or {})
    prefetched = prefetched_frames.get(period)
    if isinstance(prefetched, pd.DataFrame):
        if prefetched.empty:
            return prefetched
        return prefetched.tail(max(int(count or 0), 1)).copy()
    if bool(candidate.get("_disable_live_bar_fetch")):
        return pd.DataFrame()
    symbol = str(candidate.get("symbol") or "").strip()
    if not symbol or market_adapter is None:
        return pd.DataFrame()
    try:
        bars = list(market_adapter.get_bars([symbol], period=period, count=count, end_time=trade_date) or [])
    except Exception:
        return pd.DataFrame()
    return _bars_to_frame(bars)


def _weighted_average(values: list[float], weights: list[float] | None = None) -> float:
    if not values:
        return 0.0
    weights = weights or [1.0] * len(values)
    total = sum(max(float(weight), 0.0) for weight in weights) or 1.0
    return sum(float(value) * max(float(weight), 0.0) for value, weight in zip(values, weights)) / total


def _snapshot_turnover_amount(candidate: dict[str, Any], frame: pd.DataFrame | None = None) -> float:
    snapshot = dict(candidate.get("market_snapshot") or {})
    amount = float(snapshot.get("amount", 0.0) or 0.0)
    if amount > 0:
        return amount
    if frame is not None and not frame.empty and "amount" in frame.columns:
        return float(frame["amount"].iloc[-1] or 0.0)
    last_price = float(snapshot.get("last_price", 0.0) or 0.0)
    volume = float(snapshot.get("volume", 0.0) or 0.0)
    return last_price * volume


def _latest_order_book(candidate: dict[str, Any], market_adapter: Any | None) -> OrderBookSnapshot | None:
    symbol = str(candidate.get("symbol") or "").strip()
    if bool(candidate.get("_disable_live_bar_fetch")):
        return None
    if not symbol or market_adapter is None or not hasattr(market_adapter, "get_order_book_snapshots"):
        return None
    try:
        items = list(market_adapter.get_order_book_snapshots([symbol]) or [])
    except Exception:
        return None
    return items[0] if items else None


def _safe_akshare_import() -> Any | None:
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return None
    return ak


def _trade_date_text(trade_date: str | None) -> str:
    if not trade_date:
        return datetime.now().date().isoformat()
    return str(trade_date)[:10]


def _trade_date_compact(trade_date: str | None) -> str:
    return _trade_date_text(trade_date).replace("-", "")


def _cached_value(cache_key: str, loader: Callable[[], Any]) -> Any:
    with _AKSHARE_CACHE_LOCK:
        if cache_key in _AKSHARE_CACHE:
            return _AKSHARE_CACHE[cache_key]
    value = loader()
    with _AKSHARE_CACHE_LOCK:
        _AKSHARE_CACHE[cache_key] = value
    return value


def _normalize_symbol_code(symbol: str) -> str:
    return str(symbol or "").split(".")[0].strip()


def _fetch_symbol_float_shares(symbol: str) -> float | None:
    code = _normalize_symbol_code(symbol)
    if not code:
        return None

    def _loader() -> float | None:
        ak = _safe_akshare_import()
        if ak is None:
            return None
        try:
            frame = ak.stock_individual_info_em(symbol=code)
        except Exception:
            return None
        if frame is None or frame.empty:
            return None
        items = {
            str(row.get("item") or "").strip(): row.get("value")
            for _, row in frame.iterrows()
        }
        for key in ("流通股", "总股本"):
            try:
                value = float(items.get(key) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return None

    return _cached_value(f"float_shares:{code}", _loader)


def _load_chip_metrics(candidate: dict[str, Any], frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty or len(frame) < 20:
        return {}
    closes = frame["close"].astype(float).tail(60)
    volumes = frame["volume"].astype(float).clip(lower=0.0).tail(60)
    total_volume = float(volumes.sum() or 0.0)
    if total_volume <= 0:
        return {}
    latest_close = float(closes.iloc[-1] or 0.0)
    profit_ratio = float(volumes[closes <= latest_close].sum() / total_volume)
    bin_count = max(8, min(24, len(closes) // 2))
    hist, edges = np.histogram(closes.to_numpy(), bins=bin_count, weights=volumes.to_numpy())
    peak_index = int(np.argmax(hist)) if len(hist) else 0
    peak_cost = float((edges[peak_index] + edges[peak_index + 1]) / 2.0) if len(edges) >= peak_index + 2 else latest_close
    normalized = hist / max(float(hist.sum() or 1.0), 1.0)
    hhi = float((normalized ** 2).sum() or 0.0)
    concentration = _clamp((hhi - 1.0 / bin_count) / max(1.0 - 1.0 / bin_count, 1e-9), 0.0, 1.0)
    float_shares = _fetch_symbol_float_shares(str(candidate.get("symbol") or ""))
    turnover_rate_20d = None
    if float_shares and float_shares > 0:
        turnover_rate_20d = float(frame["volume"].astype(float).tail(20).sum() / float_shares)
    return {
        "profit_ratio": profit_ratio,
        "peak_cost": peak_cost,
        "peak_distance_pct": _safe_ratio(latest_close - peak_cost, peak_cost or latest_close or 1.0, 0.0),
        "concentration_20d": concentration,
        "turnover_rate_20d": float(turnover_rate_20d or 0.0),
        "float_shares_available": 1.0 if turnover_rate_20d is not None else 0.0,
    }


def _fetch_market_regime_metrics(market_adapter: Any | None, trade_date: str | None) -> dict[str, float]:
    if market_adapter is None or not hasattr(market_adapter, "get_main_board_universe"):
        return {}
    cache_key = f"market_regime_metrics:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        try:
            universe = list(dict.fromkeys(list(market_adapter.get_main_board_universe() or [])[:120]))
        except Exception:
            return {}
        if not universe:
            return {}
        try:
            bars = list(market_adapter.get_bars(universe, period="1d", count=35, end_time=trade_date) or [])
        except Exception:
            return {}
        if not bars:
            return {}
        rows = [
            {
                "symbol": str(bar.symbol),
                "trade_time": str(bar.trade_time)[:10],
                "close": float(bar.close or 0.0),
                "pre_close": float(bar.pre_close or 0.0),
            }
            for bar in bars
            if float(bar.pre_close or 0.0) > 0
        ]
        frame = pd.DataFrame(rows)
        if frame.empty:
            return {}
        frame["return"] = (frame["close"] - frame["pre_close"]) / frame["pre_close"].replace(0.0, np.nan)
        frame = frame.dropna(subset=["return"])
        if frame.empty:
            return {}
        grouped = frame.groupby("trade_time")
        market_returns = grouped["return"].mean().dropna().sort_index()
        if market_returns.empty:
            return {}
        latest_date = str(market_returns.index[-1])
        latest_values = frame[frame["trade_time"] == latest_date]["return"].astype(float)
        breadth = float((latest_values > 0.0).mean() or 0.0)
        avg_change = float(latest_values.mean() or 0.0)
        rolling_vol = market_returns.rolling(window=20).std().dropna()
        latest_vol = float(rolling_vol.iloc[-1] or 0.0) if not rolling_vol.empty else float(market_returns.tail(20).std() or 0.0)
        vol_rank = float((rolling_vol <= latest_vol).mean() or 0.5) if not rolling_vol.empty else 0.5
        return {
            "breadth": breadth,
            "avg_change": avg_change,
            "market_volatility_20d": latest_vol,
            "volatility_rank_20d": vol_rank,
        }

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _fetch_margin_balance_metrics(trade_date: str | None) -> dict[str, float]:
    compact = _trade_date_compact(trade_date)

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            sh = ak.macro_china_market_margin_sh()
            sz = ak.macro_china_market_margin_sz()
        except Exception:
            return {}
        if sh is None or sh.empty or sz is None or sz.empty:
            return {}
        sh = sh.copy()
        sz = sz.copy()
        sh["日期"] = sh["日期"].astype(str)
        sz["日期"] = sz["日期"].astype(str)
        sh["融资余额"] = pd.to_numeric(sh["融资余额"], errors="coerce")
        sz["融资余额"] = pd.to_numeric(sz["融资余额"], errors="coerce")
        merged = pd.merge(
            sh[["日期", "融资余额"]],
            sz[["日期", "融资余额"]].rename(columns={"融资余额": "深市融资余额"}),
            on="日期",
            how="inner",
        ).sort_values("日期")
        if merged.empty:
            return {}
        merged["total_margin_balance"] = merged["融资余额"].fillna(0.0) + merged["深市融资余额"].fillna(0.0)
        subset = merged[merged["日期"] <= compact] if compact else merged
        if subset.empty:
            subset = merged
        latest = subset.tail(1)
        base = subset.tail(6).head(1)
        if latest.empty or base.empty:
            return {}
        latest_value = float(latest["total_margin_balance"].iloc[-1] or 0.0)
        base_value = float(base["total_margin_balance"].iloc[-1] or 0.0)
        return {
            "margin_balance": latest_value,
            "margin_balance_change_5d": _safe_ratio(latest_value - base_value, base_value or latest_value or 1.0, 0.0),
        }

    payload = _cached_value(f"margin_balance:{compact}", _loader)
    return dict(payload or {})


def _fetch_northbound_flow_metrics(trade_date: str | None) -> dict[str, float]:
    compact = _trade_date_compact(trade_date)

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.stock_hsgt_fund_flow_summary_em()
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        frame = frame.copy()
        frame["交易日"] = frame["交易日"].astype(str).str.replace("-", "", regex=False)
        frame["成交净买额"] = pd.to_numeric(frame["成交净买额"], errors="coerce").fillna(0.0)
        subset = frame[frame["交易日"] <= compact] if compact else frame
        if subset.empty:
            subset = frame
        latest_date = str(subset["交易日"].max() or "")
        latest = subset[(subset["交易日"] == latest_date) & (subset["资金方向"] == "北向")]
        if latest.empty:
            return {}
        return {"northbound_net_flow": float(latest["成交净买额"].sum() or 0.0)}

    payload = _cached_value(f"northbound_flow:{compact}", _loader)
    return dict(payload or {})


def _fetch_credit_spread_metrics(trade_date: str | None) -> dict[str, float]:
    compact = _trade_date_compact(trade_date)
    start_date = (datetime.strptime(_trade_date_text(trade_date), "%Y-%m-%d") - timedelta(days=40)).strftime("%Y%m%d")

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.bond_china_yield(start_date=start_date, end_date=compact)
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        frame = frame.copy()
        frame["日期"] = frame["日期"].astype(str).str.replace("-", "", regex=False)
        frame["10年"] = pd.to_numeric(frame["10年"], errors="coerce")
        subset = frame[frame["日期"] <= compact] if compact else frame
        if subset.empty:
            subset = frame
        latest_date = str(subset["日期"].max() or "")
        latest = subset[subset["日期"] == latest_date]
        gov = latest[latest["曲线名称"].astype(str).str.contains("国债收益率曲线", na=False)]
        corp = latest[latest["曲线名称"].astype(str).str.contains("商业银行普通债收益率曲线\\(AAA\\)", na=False)]
        if gov.empty or corp.empty:
            return {}
        gov_10y = float(gov["10年"].iloc[-1] or 0.0)
        corp_10y = float(corp["10年"].iloc[-1] or 0.0)
        return {
            "credit_spread_10y": corp_10y - gov_10y,
            "gov_bond_10y": gov_10y,
            "corp_bond_10y": corp_10y,
        }

    payload = _cached_value(f"credit_spread:{compact}", _loader)
    return dict(payload or {})


def _parse_numeric_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if np.isnan(numeric):
            return None
        return numeric
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "--", "-"}:
        return None
    multiplier = 1.0
    if text.endswith("%"):
        multiplier *= 0.01
        text = text[:-1].strip()
    if text.endswith("亿"):
        multiplier *= 100_000_000.0
        text = text[:-1].strip()
    elif text.endswith("万"):
        multiplier *= 10_000.0
        text = text[:-1].strip()
    elif text.endswith("千"):
        multiplier *= 1_000.0
        text = text[:-1].strip()
    matched = re.search(r"-?\d+(?:\.\d+)?", text)
    if matched is None:
        return None
    try:
        return float(matched.group(0)) * multiplier
    except (TypeError, ValueError):
        return None


def _parse_percent_value(value: Any) -> float | None:
    numeric = _parse_numeric_value(value)
    if numeric is None:
        return None
    if abs(numeric) > 1.5:
        return numeric / 100.0
    return numeric


def _em_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    code = _normalize_symbol_code(raw)
    suffix = raw.split(".")[-1].upper() if "." in raw else ""
    if suffix == "SH" or code.startswith(("5", "6", "9")):
        return f"SH{code}"
    return f"SZ{code}"


def _row_first_match(frame: pd.DataFrame, *aliases: str) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    first_column = str(frame.columns[0])
    alias_set = tuple(str(item).strip() for item in aliases if str(item).strip())
    for _, row in frame.iterrows():
        label = str(row.get(first_column) or "").strip()
        if not label:
            continue
        if any(alias in label for alias in alias_set):
            return row
    return None


def _latest_two_row_values(row: pd.Series | None) -> tuple[float | None, float | None]:
    if row is None:
        return None, None
    values: list[float] = []
    for index, value in enumerate(row.tolist()):
        if index == 0:
            continue
        numeric = _parse_numeric_value(value)
        if numeric is None:
            continue
        values.append(float(numeric))
        if len(values) >= 2:
            break
    latest = values[0] if values else None
    previous = values[1] if len(values) >= 2 else None
    return latest, previous


def _recent_days_between(value: Any, trade_date: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        target = pd.Timestamp(text)
        anchor = pd.Timestamp(_trade_date_text(trade_date))
    except Exception:
        return None
    return int((anchor.normalize() - target.normalize()).days)


def _fetch_ah_premium_metrics(symbol: str) -> dict[str, float]:
    code = _normalize_symbol_code(symbol)
    if not code:
        return {}

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.stock_zh_ah_spot_em()
        except Exception:
            return {}
        if frame is None or frame.empty or "A股代码" not in frame.columns:
            return {}
        matched = frame[frame["A股代码"].astype(str).str.strip() == code]
        if matched.empty:
            return {}
        row = matched.iloc[0]
        premium = _parse_percent_value(row.get("溢价"))
        a_change = _parse_percent_value(row.get("A股-涨跌幅"))
        h_change = _parse_percent_value(row.get("H股-涨跌幅"))
        ratio = _parse_numeric_value(row.get("比价"))
        if premium is None and a_change is None and h_change is None:
            return {}
        return {
            "ah_premium": float(premium or 0.0),
            "a_change_pct": float(a_change or 0.0),
            "h_change_pct": float(h_change or 0.0),
            "ah_price_ratio": float(ratio or 0.0),
        }

    payload = _cached_value(f"ah_premium_metrics:{code}", _loader)
    return dict(payload or {})


def _fetch_us_tech_overnight_metrics(trade_date: str | None) -> dict[str, float]:
    cache_key = f"us_tech_overnight:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.index_us_stock_sina(symbol=".IXIC")
        except Exception:
            return {}
        if frame is None or frame.empty or len(frame) < 2:
            return {}
        frame = frame.copy()
        for column in ("open", "close", "high", "low"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["open", "close"])
        if len(frame) < 2:
            return {}
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        prev_close = float(previous["close"] or 0.0)
        latest_open = float(latest["open"] or 0.0)
        latest_close = float(latest["close"] or 0.0)
        if prev_close <= 0 or latest_open <= 0:
            return {}
        return {
            "nasdaq_overnight_gap": _safe_ratio(latest_open - prev_close, prev_close, 0.0),
            "nasdaq_session_return": _safe_ratio(latest_close - latest_open, latest_open, 0.0),
            "nasdaq_close_to_close": _safe_ratio(latest_close - prev_close, prev_close, 0.0),
        }

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _fetch_global_commodity_metrics(trade_date: str | None) -> dict[str, float]:
    cache_key = f"global_commodity_metrics:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.futures_global_spot_em()
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        aliases = {
            "brent": ("布伦特", "原油"),
            "gold": ("黄金",),
            "copper": ("铜",),
            "aluminum": ("铝",),
            "natural_gas": ("天然气",),
            "lithium": ("锂",),
        }
        payload: dict[str, float] = {}
        for key, keywords in aliases.items():
            matched_row = None
            for _, row in frame.iterrows():
                descriptor = " ".join(str(row.get(column) or "") for column in ("代码", "名称"))
                if any(keyword in descriptor for keyword in keywords):
                    matched_row = row
                    break
            if matched_row is None:
                continue
            change_pct = _parse_percent_value(matched_row.get("涨跌幅"))
            if change_pct is not None:
                payload[f"{key}_change_pct"] = float(change_pct)
        return payload

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _fetch_hot_attention_metrics(symbol: str, trade_date: str | None) -> dict[str, float]:
    em_symbol = _em_symbol(symbol)
    if not em_symbol:
        return {}
    cache_key = f"hot_attention:{em_symbol}:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        metrics: dict[str, float] = {}
        try:
            rank_frame = ak.stock_hot_rank_detail_em(symbol=em_symbol)
        except Exception:
            rank_frame = None
        if rank_frame is not None and not rank_frame.empty:
            rank_frame = rank_frame.copy()
            latest = rank_frame.iloc[-1]
            rank_value = _parse_numeric_value(latest.get("排名"))
            new_fans = _parse_numeric_value(latest.get("新晋粉丝"))
            core_fans = _parse_numeric_value(latest.get("铁杆粉丝"))
            if rank_value is not None:
                metrics["hot_rank"] = float(rank_value)
            if new_fans is not None:
                metrics["new_fans"] = float(new_fans)
            if core_fans is not None:
                metrics["core_fans"] = float(core_fans)
        try:
            keyword_frame = ak.stock_hot_keyword_em(symbol=em_symbol)
        except Exception:
            keyword_frame = None
        if keyword_frame is not None and not keyword_frame.empty and "热度" in keyword_frame.columns:
            heats = pd.to_numeric(keyword_frame["热度"], errors="coerce").dropna()
            if not heats.empty:
                metrics["keyword_heat_mean"] = float(heats.tail(5).mean() or 0.0)
                metrics["keyword_count"] = float(heats.tail(5).count() or 0.0)
        return metrics

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _text_sentiment_score(text: str) -> float:
    normalized = str(text or "").strip()
    if not normalized:
        return 0.0
    positive_keywords = ("增长", "突破", "回购", "中标", "签约", "获批", "增持", "预增", "创新高", "扩产", "订单", "利好", "上修")
    negative_keywords = ("减持", "问询", "处罚", "诉讼", "亏损", "下修", "终止", "违约", "暴跌", "风险", "质押", "监管")
    positive_hits = sum(normalized.count(keyword) for keyword in positive_keywords)
    negative_hits = sum(normalized.count(keyword) for keyword in negative_keywords)
    return float(positive_hits - negative_hits)


def _fetch_news_sentiment_metrics(symbol: str, trade_date: str | None) -> dict[str, float]:
    code = _normalize_symbol_code(symbol)
    if not code:
        return {}
    cache_key = f"news_sentiment:{code}:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.stock_news_em(symbol=code)
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        recent_scores: list[float] = []
        source_count = 0
        for _, row in frame.head(20).iterrows():
            days = _recent_days_between(row.get("发布时间"), trade_date)
            if days is not None and days > 7:
                continue
            text = " ".join(
                [
                    str(row.get("新闻标题") or ""),
                    str(row.get("新闻内容") or "")[:160],
                ]
            )
            sentiment = _text_sentiment_score(text)
            recent_scores.append(sentiment)
            source_count += 1
        if not recent_scores:
            return {}
        positive = sum(1 for item in recent_scores if item > 0)
        negative = sum(1 for item in recent_scores if item < 0)
        return {
            "news_sentiment_mean": float(sum(recent_scores) / len(recent_scores)),
            "news_positive_ratio": positive / len(recent_scores),
            "news_negative_ratio": negative / len(recent_scores),
            "news_item_count": float(source_count),
        }

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _fetch_announcement_metrics(symbol: str, trade_date: str | None) -> dict[str, float]:
    code = _normalize_symbol_code(symbol)
    if not code:
        return {}
    cache_key = f"announcement_metrics:{code}:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.stock_individual_notice_report(security=code, symbol="全部")
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        positive_keywords = ("回购", "中标", "签约", "增持", "激励", "预增", "订单", "扩产", "合作")
        negative_keywords = ("减持", "诉讼", "问询", "处罚", "终止", "风险", "质押", "亏损")
        item_count = 0
        positive_count = 0
        negative_count = 0
        for _, row in frame.head(30).iterrows():
            days = _recent_days_between(row.get("公告日期"), trade_date)
            if days is not None and days > 14:
                continue
            descriptor = " ".join([str(row.get("公告标题") or ""), str(row.get("公告类型") or "")])
            item_count += 1
            if any(keyword in descriptor for keyword in positive_keywords):
                positive_count += 1
            if any(keyword in descriptor for keyword in negative_keywords):
                negative_count += 1
        if item_count <= 0:
            return {}
        return {
            "notice_count_14d": float(item_count),
            "notice_positive_ratio": positive_count / item_count,
            "notice_negative_ratio": negative_count / item_count,
        }

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _fetch_research_report_metrics(symbol: str, trade_date: str | None) -> dict[str, float]:
    code = _normalize_symbol_code(symbol)
    if not code:
        return {}
    cache_key = f"research_report_metrics:{code}:{_trade_date_text(trade_date)}"

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.stock_research_report_em(symbol=code)
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        positive_ratings = ("买入", "增持", "强烈推荐", "推荐", "优于大市", "跑赢行业")
        total_count = 0
        positive_count = 0
        latest_month_count = 0.0
        for _, row in frame.head(30).iterrows():
            days = _recent_days_between(row.get("日期"), trade_date)
            if days is not None and days > 45:
                continue
            total_count += 1
            rating = str(row.get("东财评级") or "")
            if any(keyword in rating for keyword in positive_ratings):
                positive_count += 1
            month_count = _parse_numeric_value(row.get("近一月个股研报数"))
            if month_count is not None:
                latest_month_count = max(latest_month_count, float(month_count))
        if total_count <= 0 and latest_month_count <= 0:
            return {}
        return {
            "research_report_count": float(total_count),
            "research_positive_ratio": positive_count / max(total_count, 1),
            "research_month_count": float(latest_month_count),
        }

    payload = _cached_value(cache_key, _loader)
    return dict(payload or {})


def _fetch_growth_quality_metrics(symbol: str) -> dict[str, float]:
    code = _normalize_symbol_code(symbol)
    if not code:
        return {}

    def _loader() -> dict[str, float]:
        ak = _safe_akshare_import()
        if ak is None:
            return {}
        try:
            frame = ak.stock_financial_abstract(symbol=code)
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        roe_latest, roe_previous = _latest_two_row_values(_row_first_match(frame, "净资产收益率", "ROE"))
        revenue_growth_latest, revenue_growth_previous = _latest_two_row_values(_row_first_match(frame, "营业总收入增长率", "营业收入增长率"))
        margin_latest, margin_previous = _latest_two_row_values(_row_first_match(frame, "毛利率"))
        revenue_latest, revenue_previous = _latest_two_row_values(_row_first_match(frame, "营业总收入", "营业收入"))
        payload: dict[str, float] = {}
        if roe_latest is not None:
            payload["roe_latest"] = float(_parse_percent_value(roe_latest) or roe_latest)
        if roe_previous is not None:
            payload["roe_previous"] = float(_parse_percent_value(roe_previous) or roe_previous)
        if revenue_growth_latest is not None:
            payload["revenue_growth_latest"] = float(_parse_percent_value(revenue_growth_latest) or revenue_growth_latest)
        if revenue_growth_previous is not None:
            payload["revenue_growth_previous"] = float(_parse_percent_value(revenue_growth_previous) or revenue_growth_previous)
        if margin_latest is not None:
            payload["gross_margin_latest"] = float(_parse_percent_value(margin_latest) or margin_latest)
        if margin_previous is not None:
            payload["gross_margin_previous"] = float(_parse_percent_value(margin_previous) or margin_previous)
        if revenue_latest is not None:
            payload["revenue_latest"] = float(revenue_latest)
        if revenue_previous is not None:
            payload["revenue_previous"] = float(revenue_previous)
        return payload

    payload = _cached_value(f"growth_quality_metrics:{code}", _loader)
    return dict(payload or {})


def _simple_factor_executor(score_fn: Callable[[dict[str, Any], dict[str, Any]], float], evidence: str):
    def _executor(
        definition: FactorDefinition, 
        candidate: dict[str, Any], 
        context: dict[str, Any],
        market_adapter: Any | None = None,
        trade_date: str | None = None,
        precomputed_factors: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        score = max(min(score_fn(candidate, context), 1.0), -1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, evidence],
        }

    return _executor


def _momentum_slope_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=80)
    if frame.empty and precomputed_factors and "trend_slopes" in precomputed_factors:
        trend_slopes = dict(precomputed_factors.get("trend_slopes") or {})
        score = _weighted_average(
            [float(value or 0.0) for value in trend_slopes.values()],
            [1.0 + index for index, _ in enumerate(trend_slopes.values())],
        )
        return {"score": round(_clamp(score, -1.0, 1.0), 4), "evidence": [definition.name, "使用预计算多周期趋势斜率"]}
    if frame.empty:
        return _factor_unavailable(definition, "缺少日线数据")
    closes = frame["close"].astype(float)
    values: list[float] = []
    details: list[str] = []
    for lookback in _resolve_timeframes(context, 20):
        sample = closes.tail(max(lookback, 5))
        if len(sample) < 5:
            continue
        x = np.arange(len(sample), dtype=float)
        y = np.log(sample.clip(lower=1e-6).to_numpy(dtype=float))
        slope, _ = np.polyfit(x, y, 1)
        annualized = float(np.exp(slope * min(len(sample), 20)) - 1.0)
        values.append(_clamp(annualized * 3.0, -1.0, 1.0))
        details.append(f"{lookback}日年化斜率={annualized:.2%}")
    if not values:
        return _factor_unavailable(definition, "可用样本不足")
    score = _weighted_average(values, [1.0 + idx for idx in range(len(values))])
    return {"score": round(_clamp(score, -1.0, 1.0), 4), "evidence": [definition.name, *details[:3]]}


def _relative_volume_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """量比因子: 当前成交量相对于过去 5 日平均水平。"""
    if precomputed_factors and "relative_volume" in precomputed_factors:
        rel_vol = float(precomputed_factors["relative_volume"] or 1.0)
        score = _clamp((rel_vol - 1.0) / 1.5, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"量比={rel_vol:.2f}"]}
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=10)
    if frame.empty or len(frame) < 6:
        return _factor_unavailable(definition, "缺少量能历史")
    current_vol = float(frame["volume"].iloc[-1] or 0.0)
    avg_vol = float(frame["volume"].iloc[-6:-1].mean() or 0.0)
    if avg_vol <= 0:
        return _factor_unavailable(definition, "平均成交量为零")
    rel_vol = current_vol / avg_vol
    score = _clamp((rel_vol - 1.0) / 1.5, -1.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"当前量比={rel_vol:.2f}"]}


def _limit_sentiment_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """涨跌停情绪: 衡量标的在 A 股特有的涨停/跌停机制下的活跃度。"""
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=25)
    if frame.empty or len(frame) < 5:
        return _factor_unavailable(definition, "缺少涨跌停样本")
    changes = frame["close"].pct_change().dropna()
    limit_up_hits = float((changes > 0.095).tail(10).sum())
    limit_down_hits = float((changes < -0.095).tail(10).sum())
    latest_change = float(changes.iloc[-1] if not changes.empty else 0.0)
    sentiment = latest_change * 6.0 + limit_up_hits * 0.15 - limit_down_hits * 0.2
    score = _clamp(sentiment, -1.0, 1.0)
    return {
        "score": round(score, 4),
        "evidence": [
            definition.name,
            f"近10日涨停命中={int(limit_up_hits)} 次",
            f"近10日跌停命中={int(limit_down_hits)} 次",
            f"最新涨跌幅={latest_change:.2%}",
        ],
    }


def _smart_money_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """聪明钱Q指标: 高冲击成交的 VWAP 与全日 VWAP 比。"""
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1m", count=240)
    if frame.empty or len(frame) < 60:
        return _factor_unavailable(definition, "缺少分时成交")
    closes = frame["close"].astype(float)
    volumes = frame["volume"].astype(float).clip(lower=1e-9)
    returns = closes.pct_change().abs().fillna(0.0)
    impact = returns / np.sqrt(volumes)
    threshold = float(impact.quantile(0.8) or 0.0)
    smart_mask = impact >= threshold
    if int(smart_mask.sum()) <= 0:
        return _factor_unavailable(definition, "高冲击样本不足")
    vwap_smart = float((closes[smart_mask] * volumes[smart_mask]).sum() / max(volumes[smart_mask].sum(), 1e-9))
    vwap_all = float((closes * volumes).sum() / max(volumes.sum(), 1e-9))
    q_value = vwap_smart / max(vwap_all, 1e-9)
    score = _clamp((1.0 - q_value) * 25.0, -1.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"Q={q_value:.4f}", f"高冲击样本={int(smart_mask.sum())}"]}    


def _main_fund_inflow_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1m", count=240)
    if frame.empty or len(frame) < 80:
        return _factor_unavailable(definition, "缺少分时资金流数据")
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    raw_flow = typical * frame["volume"]
    direction = typical.diff().fillna(0.0)
    positive_flow = float(raw_flow[direction >= 0].sum())
    negative_flow = float(raw_flow[direction < 0].sum())
    ratio = (positive_flow - negative_flow) / max(positive_flow + negative_flow, 1e-9)
    score = _clamp(ratio * 2.5, -1.0, 1.0)
    return {
        "score": round(score, 4),
        "evidence": [definition.name, f"正向资金流={positive_flow:.0f}", f"净流强度={ratio:.2%}"],
    }


def _sector_heat_score_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sector = str(candidate.get("resolved_sector") or "").strip()
    if not sector:
        return _factor_unavailable(definition, "缺少板块归属")
    if market_adapter is None:
        return _factor_unavailable(definition, "缺少板块行情源")
    try:
        members = list(dict.fromkeys(market_adapter.get_sector_symbols(sector)[:18]))
        snapshots = list(market_adapter.get_snapshots(members))
    except Exception:
        snapshots = []
    if not snapshots:
        return _factor_unavailable(definition, "无法获取板块成员行情")
    rises = []
    for snapshot in snapshots:
        if snapshot.pre_close > 0:
            rises.append((snapshot.last_price - snapshot.pre_close) / snapshot.pre_close)
    if not rises:
        return _factor_unavailable(definition, "板块成员涨跌幅缺失")
    breadth = sum(1 for value in rises if value > 0.0) / len(rises)
    avg_return = sum(rises) / len(rises)
    hot_bonus = 0.15 if sector in set(context.get("hot_sectors", [])) else 0.0
    score = _clamp(breadth * 0.8 + avg_return * 4.0 + hot_bonus, -1.0, 1.0)
    return {
        "score": round(score, 4),
        "evidence": [definition.name, f"板块上涨占比={breadth:.0%}", f"平均涨幅={avg_return:.2%}"],
    }


def _breakout_quality_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=40)
    if frame.empty or len(frame) < 25:
        return _factor_unavailable(definition, "缺少突破样本")
    recent = frame.tail(20)
    last_close = float(recent["close"].iloc[-1])
    base_high = float(recent["high"].iloc[:-1].max() or 0.0)
    avg_volume = float(recent["volume"].iloc[:-1].mean() or 0.0)
    latest_volume = float(recent["volume"].iloc[-1] or 0.0)
    breakout_pct = (last_close - base_high) / max(base_high, 1e-9)
    volume_ratio = latest_volume / max(avg_volume, 1e-9)
    score = _clamp(breakout_pct * 12.0 + (volume_ratio - 1.0) * 0.35, -1.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"突破幅度={breakout_pct:.2%}", f"量能比={volume_ratio:.2f}"]}


def _news_catalyst_score_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    highlights = list(context.get("event_highlights") or context.get("highlights") or [])
    symbol = str(candidate.get("symbol") or "").strip()
    sector = str(candidate.get("resolved_sector") or "").strip()
    if not highlights:
        return _factor_unavailable(definition, "缺少事件上下文")
    score = 0.0
    matched_titles: list[str] = []
    for item in highlights:
        if str(item.get("sentiment") or "").lower() in {"negative", "bearish"}:
            continue
        title = str(item.get("title") or "")
        related_symbol = str(item.get("symbol") or "").strip()
        related_sector = str(item.get("sector") or item.get("tag") or "").strip()
        if related_symbol and related_symbol == symbol:
            score += 0.65
            matched_titles.append(title)
        elif sector and related_sector and sector in related_sector:
            score += 0.25
            matched_titles.append(title)
    if score <= 0:
        return _factor_unavailable(definition, "当前无正面催化命中")
    return {"score": round(_clamp(score, 0.0, 1.0), 4), "evidence": [definition.name, *matched_titles[:2]]}


def _liquidity_risk_penalty_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    daily_frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=20)
    snapshot = dict(candidate.get("market_snapshot") or {})
    turnover = _snapshot_turnover_amount(candidate, daily_frame)
    bid = float(snapshot.get("bid_price", 0.0) or 0.0)
    ask = float(snapshot.get("ask_price", 0.0) or 0.0)
    last_price = float(snapshot.get("last_price", 0.0) or 0.0)
    spread = (ask - bid) / max(((ask + bid) / 2.0) or last_price or 1.0, 1e-9) if ask > 0 and bid > 0 else 0.0
    vol = 0.0
    if not daily_frame.empty and len(daily_frame) >= 10:
        vol = float(daily_frame["close"].pct_change().tail(10).std() or 0.0)
    penalty = max(0.0, 0.025 - min(turnover / 100_000_000.0, 0.025)) * 25.0 + spread * 15.0 + vol * 8.0
    return {"score": round(_clamp(-penalty, -1.0, 0.0), 4), "evidence": [definition.name, f"成交额={turnover:.0f}", f"价差={spread:.2%}", f"波动率={vol:.2%}"]}


def _price_drawdown_20d_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    drawdown = None
    if precomputed_factors and "price_drawdown_20d" in precomputed_factors:
        drawdown = float(precomputed_factors["price_drawdown_20d"] or 0.0)
    if drawdown is None:
        frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=25)
        if frame.empty or len(frame) < 20:
            return _factor_unavailable(definition, "缺少20日回撤样本")
        recent = frame.tail(20)
        peak = float(recent["high"].max() or 0.0)
        close = float(recent["close"].iloc[-1] or 0.0)
        drawdown = (close - peak) / max(peak, 1e-9)
    score = _clamp(abs(min(drawdown, 0.0)) * 4.0, 0.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"20日回撤={drawdown:.2%}"]}


def _volatility_20d_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vol = None
    if precomputed_factors and "volatility_20d" in precomputed_factors:
        vol = float(precomputed_factors["volatility_20d"] or 0.0)
    if vol is None:
        frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=25)
        if frame.empty or len(frame) < 21:
            return _factor_unavailable(definition, "缺少20日波动样本")
        vol = float(frame["close"].pct_change().tail(20).std() or 0.0)
    score = _clamp((0.03 - vol) / 0.03, -1.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"20日波动率={vol:.2%}"]}


def _rsi_14_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rsi_value = None
    if precomputed_factors and "rsi_14" in precomputed_factors:
        rsi_value = float(precomputed_factors["rsi_14"] or 0.0)
    if rsi_value is None:
        frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=20)
        if frame.empty or len(frame) < 15:
            return _factor_unavailable(definition, "缺少 RSI 样本")
        delta = frame["close"].diff().dropna()
        gain = delta.clip(lower=0.0)
        loss = (-delta.clip(upper=0.0)).replace(0.0, 1e-9)
        rs = (gain.rolling(window=14).mean() / loss.rolling(window=14).mean()).iloc[-1]
        rsi_value = float(100 - (100 / (1 + rs)))
    score = _clamp((rsi_value - 50.0) / 25.0, -1.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"RSI14={rsi_value:.2f}"]}


def _pb_ratio_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    breakdown = _score_breakdown(candidate)
    pb_ratio = breakdown.get("pb_ratio")
    if pb_ratio is None:
        pb_ratio = dict(candidate.get("fundamental_snapshot") or {}).get("pb_ratio")
    try:
        pb_value = float(pb_ratio)
    except (TypeError, ValueError):
        return _factor_unavailable(definition, "缺少 PB 基础面数据")
    style = _market_style(candidate, context)
    if style == "momentum":
        score = _clamp((pb_value - 2.0) / 4.0, -0.35, 1.0)
    elif style == "value":
        score = _clamp((2.5 - pb_value) / 2.5, -1.0, 1.0)
    else:
        score = _clamp((3.0 - abs(pb_value - 3.0)) / 3.0, -0.4, 0.8)
    return {"score": round(score, 4), "evidence": [definition.name, f"PB={pb_value:.2f}", f"市场风格={style}"]}


def _limit_up_popularity_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    count = None
    if precomputed_factors and "limit_up_count_20d" in precomputed_factors:
        count = int(precomputed_factors["limit_up_count_20d"] or 0)
    if count is None:
        frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=25)
        if frame.empty or len(frame) < 21:
            return _factor_unavailable(definition, "缺少涨停统计样本")
        changes = frame["close"].pct_change().dropna().tail(20)
        count = int((changes > 0.095).sum())
    score = _clamp(count / 4.0, 0.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, f"20日涨停次数={count}"]}


def _order_book_imbalance_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = _latest_order_book(candidate, market_adapter)
    if snapshot is None:
        return _factor_unavailable(definition, "缺少盘口五档快照")
    signal = order_book_imbalance(snapshot)
    return {"score": signal.score, "evidence": [definition.name, *signal.evidence[:2]]}


def _large_order_flow_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = _latest_order_book(candidate, market_adapter)
    if snapshot is None:
        return _factor_unavailable(definition, "缺少大单流向快照")
    signal = large_order_flow(snapshot)
    return {"score": signal.score, "evidence": [definition.name, *signal.evidence[:2]]}


def _counterparty_strength_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1m", count=240)
    if frame.empty or len(frame) < 60:
        return _factor_unavailable(definition, "缺少对手盘分时样本")
    result = counterparty_strength_from_bars(frame)
    return {
        "score": round(_clamp(float(result.get("score", 0.0) or 0.0), -1.0, 1.0), 4),
        "evidence": [definition.name, f"对手盘类型={result.get('trader_type')}", *list(result.get("evidence") or [])[:1]],
    }


def _score_breakdown_float(candidate: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    breakdown = _score_breakdown(candidate)
    for key in keys:
        value = breakdown.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return float(default)


def _market_change_pct(candidate: dict[str, Any]) -> float:
    snapshot = dict(candidate.get("market_snapshot") or {})
    last_price = float(snapshot.get("last_price", 0.0) or 0.0)
    pre_close = float(snapshot.get("pre_close", 0.0) or 0.0)
    if last_price > 0 and pre_close > 0:
        return (last_price - pre_close) / pre_close
    return 0.0


def _selection_quality(candidate: dict[str, Any]) -> float:
    return _clamp(_selection_score(candidate) / 100.0, 0.0, 1.0)


def _rank_strength(candidate: dict[str, Any], top_n: int = 10) -> float:
    rank = int(candidate.get("rank", top_n + 10) or top_n + 10)
    return _clamp((top_n - rank + 1) / max(float(top_n), 1.0), 0.0, 1.0)


def _market_text(context: dict[str, Any]) -> str:
    return " ".join(
        [
            str(context.get("market_hypothesis") or ""),
            " ".join(str(item) for item in list(context.get("hot_sectors") or [])),
            " ".join(str(item) for item in list(context.get("focus_sectors") or [])),
        ]
    ).lower()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    if abs(float(denominator)) <= 1e-9:
        return float(default)
    return float(numerator) / float(denominator)


def _resolved_sector(candidate: dict[str, Any]) -> str:
    sector_profile = dict(candidate.get("sector_profile") or {})
    return str(
        candidate.get("resolved_sector")
        or sector_profile.get("resolved_sector")
        or sector_profile.get("sector_name")
        or candidate.get("sector")
        or ""
    ).strip()


def _market_profile_payload(candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    payload = dict(context.get("market_profile") or {})
    payload.update(dict(candidate.get("market_profile") or {}))
    return payload


def _behavior_profile_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return dict(candidate.get("behavior_profile") or {})


def _sector_profile_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return dict(candidate.get("sector_profile") or {})


def _fundamental_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return dict(candidate.get("fundamental_snapshot") or {})


def _market_style(candidate: dict[str, Any], context: dict[str, Any]) -> str:
    profile = _market_profile_payload(candidate, context)
    style_tokens = " ".join(
        [
            str(profile.get("regime") or ""),
            str(profile.get("sentiment_phase") or ""),
            _market_text(context),
        ]
    ).lower()
    if any(token in style_tokens for token in ("trend", "main", "growth", "momentum", "主升", "趋势", "成长", "题材", "扩散")):
        return "momentum"
    if any(token in style_tokens for token in ("value", "defensive", "mean_reversion", "震荡", "防守", "价值", "低估", "回撤")):
        return "value"
    regime = str(profile.get("regime") or "").lower()
    if regime in {"trend", "breakout", "momentum"}:
        return "momentum"
    if regime in {"defensive", "chaos", "range", "mean_reversion"}:
        return "value"
    return "neutral"


def _event_highlights(context: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    symbol = str(candidate.get("symbol") or "").strip()
    sector = _resolved_sector(candidate)
    positives: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []
    highlights = list(context.get("event_highlights") or context.get("highlights") or [])
    for raw in highlights:
        item = dict(raw or {})
        sentiment = str(item.get("sentiment") or "").lower()
        related_symbol = str(item.get("symbol") or "").strip()
        related_sector = str(item.get("sector") or item.get("tag") or "").strip()
        title = str(item.get("title") or "")
        matched = False
        if related_symbol and related_symbol == symbol:
            matched = True
        elif sector and related_sector and sector in related_sector:
            matched = True
        elif sector and title and sector in title:
            matched = True
        if not matched:
            continue
        if sentiment in {"negative", "bearish"}:
            negatives.append(item)
        else:
            positives.append(item)
    return positives, negatives


def _load_daily_metrics(
    candidate: dict[str, Any],
    market_adapter: Any | None,
    trade_date: str | None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1d", count=80)
    if frame.empty or len(frame) < 12:
        return frame, {}
    closes = frame["close"].astype(float)
    opens = frame["open"].astype(float)
    highs = frame["high"].astype(float)
    lows = frame["low"].astype(float)
    volumes = frame["volume"].astype(float).clip(lower=1e-9)
    amounts = frame["amount"].astype(float).clip(lower=1e-9)
    returns = closes.pct_change().fillna(0.0)
    ma5 = float(closes.tail(5).mean() or closes.iloc[-1])
    ma10 = float(closes.tail(10).mean() or ma5)
    ma20 = float(closes.tail(20).mean() or ma10)
    latest_close = float(closes.iloc[-1] or 0.0)
    prev_close = float(closes.iloc[-2] or latest_close)
    latest_open = float(opens.iloc[-1] or latest_close)
    latest_high = float(highs.iloc[-1] or latest_close)
    latest_low = float(lows.iloc[-1] or latest_close)
    latest_volume = float(volumes.iloc[-1] or 0.0)
    latest_amount = float(amounts.iloc[-1] or 0.0)
    range_pct = _safe_ratio(latest_high - latest_low, latest_close or prev_close or 1.0)
    lower_shadow = _safe_ratio(min(latest_open, latest_close) - latest_low, latest_high - latest_low, 0.0)
    base_high_20 = float(highs.tail(21).iloc[:-1].max() or latest_high)
    base_low_10 = float(lows.tail(10).min() or latest_low)
    previous_volume_mean = float(volumes.tail(6).iloc[:-1].mean() or latest_volume)
    previous_amount_mean = float(amounts.tail(6).iloc[:-1].mean() or latest_amount)
    prev_vol_window = returns.tail(20).iloc[:-5]
    metrics = {
        "return_1d": _safe_ratio(latest_close - prev_close, prev_close, 0.0),
        "return_3d": _safe_ratio(latest_close - float(closes.iloc[-4] or latest_close), float(closes.iloc[-4] or latest_close), 0.0) if len(closes) >= 4 else 0.0,
        "return_5d": _safe_ratio(latest_close - float(closes.iloc[-6] or latest_close), float(closes.iloc[-6] or latest_close), 0.0) if len(closes) >= 6 else 0.0,
        "return_10d": _safe_ratio(latest_close - float(closes.iloc[-11] or latest_close), float(closes.iloc[-11] or latest_close), 0.0) if len(closes) >= 11 else 0.0,
        "return_20d": _safe_ratio(latest_close - float(closes.iloc[-21] or latest_close), float(closes.iloc[-21] or latest_close), 0.0) if len(closes) >= 21 else 0.0,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma_alignment": ((1.0 if latest_close > ma5 else -1.0) + (1.0 if ma5 > ma10 else -1.0) + (1.0 if ma10 > ma20 else -1.0)) / 3.0,
        "positive_ratio_5d": float((returns.tail(5) > 0).mean() or 0.0),
        "positive_ratio_10d": float((returns.tail(10) > 0).mean() or 0.0),
        "drawdown_10d": _safe_ratio(latest_close - float(highs.tail(10).max() or latest_close), float(highs.tail(10).max() or latest_close), 0.0),
        "drawdown_20d": _safe_ratio(latest_close - float(highs.tail(20).max() or latest_close), float(highs.tail(20).max() or latest_close), 0.0),
        "gap_pct": _safe_ratio(latest_open - prev_close, prev_close, 0.0),
        "intraday_continuation": _safe_ratio(latest_close - latest_open, latest_open or latest_close or 1.0, 0.0),
        "breakout_pct": _safe_ratio(latest_close - base_high_20, base_high_20, 0.0),
        "retest_buffer_pct": _safe_ratio(float(lows.tail(3).min() or latest_low) - base_high_20, base_high_20, 0.0),
        "volume_ratio_5d": _safe_ratio(latest_volume, previous_volume_mean, 1.0),
        "amount_ratio_5d": _safe_ratio(latest_amount, previous_amount_mean, 1.0),
        "down_volume_ratio_5d": _safe_ratio(
            float(volumes.tail(5)[returns.tail(5) < 0].mean() or 0.0),
            float(volumes.tail(5)[returns.tail(5) >= 0].mean() or previous_volume_mean or 1.0),
            0.0,
        ),
        "close_bias_20d": _safe_ratio(latest_close - ma20, ma20, 0.0),
        "close_zscore_20d": _safe_ratio(latest_close - float(closes.tail(20).mean() or latest_close), float(closes.tail(20).std() or 1.0), 0.0),
        "volatility_5d": float(returns.tail(5).std() or 0.0),
        "volatility_20d": float(returns.tail(20).std() or 0.0),
        "volatility_expansion": _safe_ratio(float(returns.tail(5).std() or 0.0), float(prev_vol_window.std() or 1e-9), 1.0),
        "range_pct": range_pct,
        "lower_shadow_ratio": lower_shadow,
        "chip_compression": _safe_ratio(float((highs.tail(5) - lows.tail(5)).mean() or 0.0), float((highs.tail(20) - lows.tail(20)).mean() or 1e-9), 1.0),
        "stop_loss_distance_pct": _safe_ratio(latest_close - base_low_10, latest_close or 1.0, 0.0),
    }
    return frame, metrics


def _load_intraday_metrics(
    candidate: dict[str, Any],
    market_adapter: Any | None,
    trade_date: str | None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    frame = _load_bar_frame(candidate, market_adapter, trade_date, period="1m", count=240)
    if frame.empty or len(frame) < 30:
        return frame, {}
    closes = frame["close"].astype(float)
    opens = frame["open"].astype(float)
    highs = frame["high"].astype(float)
    lows = frame["low"].astype(float)
    volumes = frame["volume"].astype(float).clip(lower=1e-9)
    amounts = frame["amount"].astype(float).clip(lower=1e-9)
    first_price = float(opens.iloc[0] or closes.iloc[0])
    last_price = float(closes.iloc[-1] or first_price)
    low_price = float(lows.min() or last_price)
    vwap = float((closes * volumes).sum() / max(volumes.sum(), 1e-9))
    minute_returns = closes.pct_change().fillna(0.0)
    half = max(len(frame) // 2, 1)
    last_hour = min(60, len(frame))
    first_flow = float((minute_returns.iloc[:half] * amounts.iloc[:half]).sum())
    second_flow = float((minute_returns.iloc[half:] * amounts.iloc[half:]).sum())
    metrics = {
        "intraday_recovery": _safe_ratio(last_price - low_price, max(first_price - low_price, first_price * 0.01), 0.0),
        "vwap_gap": _safe_ratio(last_price - vwap, vwap, 0.0),
        "vwap_reclaim": 1.0 if low_price < vwap < last_price else 0.0,
        "morning_volume_share": _safe_ratio(float(volumes.iloc[:30].sum()), float(volumes.sum()), 0.0),
        "afternoon_return": _safe_ratio(last_price - float(closes.iloc[-last_hour] or last_price), float(closes.iloc[-last_hour] or last_price), 0.0),
        "afternoon_volume_share": _safe_ratio(float(volumes.iloc[-last_hour:].sum()), float(volumes.sum()), 0.0),
        "trend_stability": _safe_ratio(abs(_safe_ratio(last_price - first_price, first_price, 0.0)), float(minute_returns.std() or 1e-9), 0.0),
        "intraday_volatility": float(minute_returns.std() or 0.0),
        "late_flow_shift": _safe_ratio(second_flow - first_flow, abs(first_flow) + abs(second_flow) + 1e-9, 0.0),
        "opening_impulse": _safe_ratio(float((closes.iloc[min(30, len(closes) - 1)] if len(closes) > 30 else closes.iloc[-1]) - first_price), first_price, 0.0),
        "afternoon_bid_strength": _safe_ratio(float(closes.iloc[-1] - closes.iloc[-last_hour]), float(closes.iloc[-last_hour] or closes.iloc[-1]), 0.0),
    }
    return frame, metrics


def _load_sector_metrics(candidate: dict[str, Any], context: dict[str, Any], market_adapter: Any | None) -> dict[str, float]:
    sector = _resolved_sector(candidate)
    sector_profile = _sector_profile_payload(candidate)
    behavior_profile = _behavior_profile_payload(candidate)
    if not sector:
        return {}
    metrics = {
        "hot_sector_hit": 1.0 if sector in set(context.get("hot_sectors") or []) else 0.0,
        "focus_sector_hit": 1.0 if sector in set(context.get("focus_sectors", context.get("hot_sectors", [])) or []) else 0.0,
        "sector_strength": _clamp(_safe_float(sector_profile.get("strength_score", sector_profile.get("reflow_score", behavior_profile.get("leader_frequency_30d", 0.0)))) / 10.0, -1.0, 1.0),
        "leader_rank_score": 0.0,
        "breadth": _safe_float(sector_profile.get("breadth", sector_profile.get("breadth_score", sector_profile.get("rise_ratio", 0.0))), 0.0),
        "limit_up_ratio": 0.0,
    }
    leader_symbols = list(sector_profile.get("leader_symbols") or [])
    symbol = str(candidate.get("symbol") or "").strip()
    if leader_symbols and symbol:
        try:
            leader_rank = leader_symbols.index(symbol) + 1
            metrics["leader_rank_score"] = _clamp(1.0 - (leader_rank - 1) / max(len(leader_symbols), 1), 0.0, 1.0)
        except ValueError:
            metrics["leader_rank_score"] = 0.0
    elif behavior_profile.get("avg_sector_rank_30d") is not None:
        avg_rank = _safe_float(behavior_profile.get("avg_sector_rank_30d"), 99.0)
        metrics["leader_rank_score"] = _clamp(1.0 - (avg_rank - 1.0) / 10.0, 0.0, 1.0)
    limit_up_count = _safe_float(sector_profile.get("limit_up_count", sector_profile.get("limit_up_stocks", 0.0)), 0.0)
    member_count = _safe_float(sector_profile.get("member_count", sector_profile.get("stock_count", 0.0)), 0.0)
    if limit_up_count > 0 and member_count > 0:
        metrics["limit_up_ratio"] = _safe_ratio(limit_up_count, member_count, 0.0)
    if market_adapter is not None and hasattr(market_adapter, "get_sector_symbols"):
        try:
            members = list(dict.fromkeys(list(market_adapter.get_sector_symbols(sector) or [])[:18]))
            if members:
                snapshots = list(market_adapter.get_snapshots(members) or [])
                changes = [
                    _safe_ratio(float(item.last_price or 0.0) - float(item.pre_close or 0.0), float(item.pre_close or 1.0), 0.0)
                    for item in snapshots
                    if float(item.pre_close or 0.0) > 0
                ]
                if changes:
                    metrics["breadth"] = sum(1 for value in changes if value > 0.0) / len(changes)
                    metrics["sector_strength"] = _clamp(sum(changes) / len(changes) * 6.0 + metrics["hot_sector_hit"] * 0.2, -1.0, 1.0)
                    metrics["limit_up_ratio"] = max(metrics["limit_up_ratio"], sum(1 for value in changes if value > 0.095) / len(changes))
        except Exception:
            pass
    return metrics


def _load_fundamental_metrics(candidate: dict[str, Any]) -> dict[str, float]:
    fundamentals = _fundamental_payload(candidate)
    breakdown = _score_breakdown(candidate)
    return {
        "pb_ratio": _safe_float(breakdown.get("pb_ratio", fundamentals.get("pb_ratio", fundamentals.get("pb", 0.0))), 0.0),
        "pe_ratio": _safe_float(breakdown.get("pe_ratio", fundamentals.get("pe_ratio", fundamentals.get("pe_ttm", 0.0))), 0.0),
        "dividend_yield": _safe_float(fundamentals.get("dividend_yield", fundamentals.get("dividend_rate", 0.0)), 0.0),
        "cashflow_quality": _safe_float(fundamentals.get("operating_cashflow_to_profit", fundamentals.get("cashflow_quality", 0.0)), 0.0),
        "debt_to_asset": _safe_float(fundamentals.get("debt_to_asset", fundamentals.get("asset_liability_ratio", 0.0)), 0.0),
        "current_ratio": _safe_float(fundamentals.get("current_ratio", fundamentals.get("quick_ratio", 0.0)), 0.0),
        "net_profit_growth": _safe_float(fundamentals.get("net_profit_growth", fundamentals.get("profit_growth", 0.0)), 0.0),
        "order_backlog_growth": _safe_float(fundamentals.get("order_backlog_growth", fundamentals.get("backlog_growth", 0.0)), 0.0),
        "institutional_attention": _safe_float(fundamentals.get("institutional_attention", fundamentals.get("institutional_holding_change", 0.0)), 0.0),
    }


def _load_position_metrics(
    candidate: dict[str, Any],
    context: dict[str, Any],
    daily: dict[str, float],
    intraday: dict[str, float],
    sector_metrics: dict[str, float],
) -> dict[str, float]:
    holdings = [str(item).strip() for item in list(context.get("holding_symbols") or []) if str(item).strip()]
    if not holdings:
        return {}
    symbol = str(candidate.get("symbol") or "").strip()
    return {
        "holding_count": float(len(holdings)),
        "already_held": 1.0 if symbol in holdings else 0.0,
        "diversification_bonus": 0.0 if sector_metrics.get("hot_sector_hit", 0.0) > 0 else 0.6,
        "replacement_readiness": _clamp(
            max(daily.get("return_10d", 0.0), 0.0) * 6.0
            + max(daily.get("amount_ratio_5d", 1.0) - 1.0, 0.0) * 0.4
            - max(daily.get("volatility_20d", 0.0) - 0.04, 0.0) * 8.0,
            -1.0,
            1.0,
        ),
        "t0_space": _clamp(
            intraday.get("intraday_volatility", 0.0) * 80.0
            + max(intraday.get("afternoon_volume_share", 0.0) - 0.2, 0.0),
            0.0,
            1.0,
        ),
    }


def _derived_factor_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    factor_id = definition.id
    if market_adapter is not None and market_adapter.__class__.__name__ == "MockMarketDataAdapter" and factor_id in {
        "northbound_actual_flow",
        "margin_balance_change",
        "credit_spread_macro",
        "chip_turnover_rate_20d",
    }:
        return _factor_unavailable(definition, "模拟行情模式不拉取外部宏观/股本数据")
    daily_frame, daily = _load_daily_metrics(candidate, market_adapter, trade_date)
    intraday_frame, intraday = _load_intraday_metrics(candidate, market_adapter, trade_date)
    sector_metrics = _load_sector_metrics(candidate, context, market_adapter)
    fundamental = _load_fundamental_metrics(candidate)
    position_metrics = _load_position_metrics(candidate, context, daily, intraday, sector_metrics)
    positives, negatives = _event_highlights(context, candidate)
    chip_metrics: dict[str, float] | None = None
    macro_metrics: dict[str, float] | None = None
    ah_metrics: dict[str, float] | None = None
    us_metrics: dict[str, float] | None = None
    commodity_metrics: dict[str, float] | None = None
    attention_metrics: dict[str, float] | None = None
    news_metrics: dict[str, float] | None = None
    announcement_metrics: dict[str, float] | None = None
    research_metrics: dict[str, float] | None = None
    growth_metrics: dict[str, float] | None = None

    def _chip() -> dict[str, float]:
        nonlocal chip_metrics
        if chip_metrics is None:
            chip_metrics = _load_chip_metrics(candidate, daily_frame)
        return chip_metrics

    def _macro() -> dict[str, float]:
        nonlocal macro_metrics
        if macro_metrics is None:
            macro_metrics = {}
            macro_metrics.update(_fetch_market_regime_metrics(market_adapter, trade_date))
            macro_metrics.update(_fetch_margin_balance_metrics(trade_date))
            macro_metrics.update(_fetch_northbound_flow_metrics(trade_date))
            macro_metrics.update(_fetch_credit_spread_metrics(trade_date))
        return macro_metrics

    def _ah() -> dict[str, float]:
        nonlocal ah_metrics
        if ah_metrics is None:
            ah_metrics = _fetch_ah_premium_metrics(str(candidate.get("symbol") or ""))
        return ah_metrics

    def _us() -> dict[str, float]:
        nonlocal us_metrics
        if us_metrics is None:
            us_metrics = _fetch_us_tech_overnight_metrics(trade_date)
        return us_metrics

    def _commodity() -> dict[str, float]:
        nonlocal commodity_metrics
        if commodity_metrics is None:
            commodity_metrics = _fetch_global_commodity_metrics(trade_date)
        return commodity_metrics

    def _attention() -> dict[str, float]:
        nonlocal attention_metrics
        if attention_metrics is None:
            attention_metrics = _fetch_hot_attention_metrics(str(candidate.get("symbol") or ""), trade_date)
        return attention_metrics

    def _news() -> dict[str, float]:
        nonlocal news_metrics
        if news_metrics is None:
            news_metrics = _fetch_news_sentiment_metrics(str(candidate.get("symbol") or ""), trade_date)
        return news_metrics

    def _announcement() -> dict[str, float]:
        nonlocal announcement_metrics
        if announcement_metrics is None:
            announcement_metrics = _fetch_announcement_metrics(str(candidate.get("symbol") or ""), trade_date)
        return announcement_metrics

    def _research() -> dict[str, float]:
        nonlocal research_metrics
        if research_metrics is None:
            research_metrics = _fetch_research_report_metrics(str(candidate.get("symbol") or ""), trade_date)
        return research_metrics

    def _growth() -> dict[str, float]:
        nonlocal growth_metrics
        if growth_metrics is None:
            growth_metrics = _fetch_growth_quality_metrics(str(candidate.get("symbol") or ""))
        return growth_metrics

    def _daily_unavailable(reason: str = "缺少日线数据") -> dict[str, Any]:
        return _factor_unavailable(definition, reason)

    def _intraday_unavailable(reason: str = "缺少分时数据") -> dict[str, Any]:
        return _factor_unavailable(definition, reason)

    def _sector_unavailable(reason: str = "缺少板块上下文") -> dict[str, Any]:
        return _factor_unavailable(definition, reason)

    def _fundamental_unavailable(reason: str = "缺少基础面数据") -> dict[str, Any]:
        return _factor_unavailable(definition, reason)

    if factor_id == "trend_strength_10d":
        if not daily:
            return _daily_unavailable()
        score = _clamp(daily["return_10d"] * 7.0 + daily["ma_alignment"] * 0.35 + (daily["positive_ratio_10d"] - 0.5) * 0.8, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"10日收益={daily['return_10d']:.2%}", f"均线排列={daily['ma_alignment']:.2f}"]}
    if factor_id == "moving_average_alignment":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(daily["ma_alignment"], -1.0, 1.0), 4), "evidence": [definition.name, f"MA5/10/20 排列={daily['ma_alignment']:.2f}", f"20日偏离={daily['close_bias_20d']:.2%}"]}
    if factor_id == "trend_consistency_5d":
        if not daily:
            return _daily_unavailable()
        score = _clamp((daily["positive_ratio_5d"] - 0.5) * 1.4 - abs(min(daily["drawdown_10d"], 0.0)) * 3.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"5日上涨占比={daily['positive_ratio_5d']:.0%}", f"10日回撤={daily['drawdown_10d']:.2%}"]}
    if factor_id == "relative_strength_rank":
        rank_score = sector_metrics.get("leader_rank_score", 0.0)
        if rank_score <= 0 and not _behavior_profile_payload(candidate):
            return _factor_unavailable(definition, "缺少板块相对强弱数据")
        return {"score": round(_clamp(rank_score * 2.0 - 1.0, -1.0, 1.0), 4), "evidence": [definition.name, f"板块相对位置={rank_score:.2f}", f"标的={candidate.get('symbol', '')}"]}
    if factor_id == "gap_continuation_score":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(daily["gap_pct"] * 8.0 + daily["intraday_continuation"] * 8.0, -1.0, 1.0), 4), "evidence": [definition.name, f"跳空幅度={daily['gap_pct']:.2%}", f"开盘后延续={daily['intraday_continuation']:.2%}"]}
    if factor_id == "acceleration_burst_score":
        if not daily:
            return _daily_unavailable()
        accel = daily["return_3d"] - daily["return_10d"] / 3.0
        return {"score": round(_clamp(accel * 8.0 + (daily["volume_ratio_5d"] - 1.0) * 0.5, -1.0, 1.0), 4), "evidence": [definition.name, f"3日加速度={accel:.2%}", f"量能比={daily['volume_ratio_5d']:.2f}"]}
    if factor_id == "oversold_bounce_strength":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(abs(min(daily["drawdown_20d"], 0.0)) * 5.0 + max(daily["return_3d"], 0.0) * 6.0, 0.0, 1.0), 4), "evidence": [definition.name, f"20日回撤={daily['drawdown_20d']:.2%}", f"3日反弹={daily['return_3d']:.2%}"]}
    if factor_id == "mean_reversion_gap":
        if not daily:
            return _daily_unavailable()
        score = _clamp(-abs(daily["close_zscore_20d"]) / 2.5 + abs(min(daily["drawdown_10d"], 0.0)) * 1.5, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"20日Z分数={daily['close_zscore_20d']:.2f}", f"10日回撤={daily['drawdown_10d']:.2%}"]}
    if factor_id == "support_reclaim_score":
        if not daily:
            return _daily_unavailable()
        reclaimed = 1.0 if daily["ma5"] > daily["ma10"] and daily["return_1d"] > 0 else 0.0
        return {"score": round(_clamp(reclaimed * 0.45 + daily["intraday_continuation"] * 5.0 + daily["lower_shadow_ratio"] * 0.3, -1.0, 1.0), 4), "evidence": [definition.name, f"下影修复={daily['lower_shadow_ratio']:.2f}", f"当日延续={daily['intraday_continuation']:.2%}"]}
    if factor_id == "intraday_reversal_strength":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["intraday_recovery"] * 0.8 + intraday["late_flow_shift"] * 0.5, -1.0, 1.0), 4), "evidence": [definition.name, f"低点修复={intraday['intraday_recovery']:.2f}", f"后半场流向={intraday['late_flow_shift']:.2f}"]}
    if factor_id == "bear_trap_escape":
        if not daily or not intraday:
            return _factor_unavailable(definition, "缺少假摔脱困所需样本")
        return {"score": round(_clamp(daily["lower_shadow_ratio"] * 0.6 + max(intraday["intraday_recovery"] - 0.4, 0.0) * 0.8, -1.0, 1.0), 4), "evidence": [definition.name, f"长下影={daily['lower_shadow_ratio']:.2f}", f"盘中脱困={intraday['intraday_recovery']:.2f}"]}
    if factor_id == "low_volume_pullback_quality":
        if not daily:
            return _daily_unavailable()
        score = _clamp((1.0 - min(max(daily["down_volume_ratio_5d"], 0.0), 1.5)) * 0.8 + max(daily["drawdown_10d"], -0.12) * 2.5 + 0.3, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"跌日量比={daily['down_volume_ratio_5d']:.2f}", f"10日回撤={daily['drawdown_10d']:.2%}"]}
    if factor_id == "turnover_acceleration":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp((daily["amount_ratio_5d"] - 1.0) * 0.7, -1.0, 1.0), 4), "evidence": [definition.name, f"成交额放大={daily['amount_ratio_5d']:.2f}x", f"5日收益={daily['return_5d']:.2%}"]}
    if factor_id == "volume_breakout_confirmation":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(daily["breakout_pct"] * 10.0 + (daily["volume_ratio_5d"] - 1.0) * 0.5, -1.0, 1.0), 4), "evidence": [definition.name, f"突破幅度={daily['breakout_pct']:.2%}", f"量能确认={daily['volume_ratio_5d']:.2f}x"]}
    if factor_id == "volume_contraction_signal":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp((1.0 - min(daily["volume_ratio_5d"], 1.5)) * 0.6 + max(daily["close_bias_20d"], -0.05) * 6.0, -1.0, 1.0), 4), "evidence": [definition.name, f"量比={daily['volume_ratio_5d']:.2f}", f"20日偏离={daily['close_bias_20d']:.2%}"]}
    if factor_id == "liquidity_depth_score":
        turnover = _snapshot_turnover_amount(candidate, daily_frame if not daily_frame.empty else None)
        snapshot = dict(candidate.get("market_snapshot") or {})
        bid = _safe_float(snapshot.get("bid_price"), 0.0)
        ask = _safe_float(snapshot.get("ask_price"), 0.0)
        mid = ((bid + ask) / 2.0) if ask > 0 and bid > 0 else _safe_float(snapshot.get("last_price"), 1.0)
        spread = _safe_ratio(ask - bid, mid or 1.0, 0.0) if ask > 0 and bid > 0 else 0.0
        return {"score": round(_clamp(min(turnover / 30_000_000.0, 1.0) - spread * 8.0, -1.0, 1.0), 4), "evidence": [definition.name, f"成交额={turnover:.0f}", f"买卖价差={spread:.2%}"]}
    if factor_id == "opening_volume_impulse":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["opening_impulse"] * 10.0 + intraday["morning_volume_share"] - 0.2, -1.0, 1.0), 4), "evidence": [definition.name, f"开盘脉冲={intraday['opening_impulse']:.2%}", f"早盘量占比={intraday['morning_volume_share']:.0%}"]}
    if factor_id == "northbound_flow_proxy":
        if not daily:
            return _daily_unavailable()
        score = _clamp((1.0 - min(daily["volatility_20d"] / 0.04, 1.5)) * 0.5 + max(daily["amount_ratio_5d"] - 1.0, 0.0) * 0.3 + max(sector_metrics.get("sector_strength", 0.0), 0.0) * 0.2, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"20日波动={daily['volatility_20d']:.2%}", f"板块强度={sector_metrics.get('sector_strength', 0.0):.2f}"]}
    if factor_id == "large_order_persistence":
        snapshot = _latest_order_book(candidate, market_adapter)
        if snapshot is None:
            return _factor_unavailable(definition, "缺少盘口大单快照")
        flow = large_order_flow(snapshot)
        return {"score": round(_clamp(flow.score * 0.8 + max(intraday.get("late_flow_shift", 0.0), 0.0) * 0.3, -1.0, 1.0), 4), "evidence": [definition.name, *flow.evidence[:2]]}
    if factor_id == "main_fund_turning_point":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["late_flow_shift"] * 1.2 + intraday["afternoon_return"] * 6.0, -1.0, 1.0), 4), "evidence": [definition.name, f"后半场资金切换={intraday['late_flow_shift']:.2f}", f"午后收益={intraday['afternoon_return']:.2%}"]}
    if factor_id == "chip_concentration_proxy":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp((1.0 - min(daily["chip_compression"], 1.5)) * 0.6 + max(daily["return_10d"], 0.0) * 4.0, -1.0, 1.0), 4), "evidence": [definition.name, f"波动压缩={daily['chip_compression']:.2f}", f"10日收益={daily['return_10d']:.2%}"]}
    if factor_id == "chip_profit_ratio":
        metrics = _chip()
        if not metrics:
            return _factor_unavailable(definition, "缺少筹码分布样本")
        profit_ratio = float(metrics.get("profit_ratio", 0.0) or 0.0)
        score = _clamp((0.58 - profit_ratio) * 2.2, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"获利盘比例={profit_ratio:.0%}", f"标的={candidate.get('symbol', '')}"]}
    if factor_id == "chip_cost_peak_distance":
        metrics = _chip()
        if not metrics:
            return _factor_unavailable(definition, "缺少筹码峰值样本")
        peak_cost = float(metrics.get("peak_cost", 0.0) or 0.0)
        distance = float(metrics.get("peak_distance_pct", 0.0) or 0.0)
        score = _clamp(distance * 8.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"筹码峰值={peak_cost:.2f}", f"距峰值={distance:.2%}"]}
    if factor_id == "chip_concentration_20d":
        metrics = _chip()
        if not metrics:
            return _factor_unavailable(definition, "缺少筹码集中度样本")
        concentration = float(metrics.get("concentration_20d", 0.0) or 0.0)
        momentum_bonus = max(daily.get("return_10d", 0.0), 0.0) * 2.0 if daily else 0.0
        score = _clamp(concentration * 1.4 + momentum_bonus - 0.4, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"20日筹码集中度={concentration:.2f}", f"10日收益={daily.get('return_10d', 0.0):.2%}"]}
    if factor_id == "chip_turnover_rate_20d":
        metrics = _chip()
        if not metrics or float(metrics.get("float_shares_available", 0.0) or 0.0) <= 0:
            return _factor_unavailable(definition, "缺少流通股本或换手样本")
        turnover_rate = float(metrics.get("turnover_rate_20d", 0.0) or 0.0)
        score = _clamp((turnover_rate - 0.6) / 0.8, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"20日换手率={turnover_rate:.0%}", "基于真实流通股本估算"]}
    if factor_id == "intraday_capital_reflow":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["late_flow_shift"] * 0.8 + intraday["afternoon_volume_share"] * 0.4 + intraday["afternoon_return"] * 4.0, -1.0, 1.0), 4), "evidence": [definition.name, f"资金回流={intraday['late_flow_shift']:.2f}", f"午后量占比={intraday['afternoon_volume_share']:.0%}"]}
    if factor_id == "market_breadth_index":
        metrics = _macro()
        if not metrics:
            return _factor_unavailable(definition, "缺少市场广度样本")
        breadth = float(metrics.get("breadth", 0.0) or 0.0)
        avg_change = float(metrics.get("avg_change", 0.0) or 0.0)
        score = _clamp((breadth - 0.5) * 2.5 + avg_change * 4.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"市场广度={breadth:.0%}", f"平均涨幅={avg_change:.2%}"]}
    if factor_id == "northbound_actual_flow":
        metrics = _macro()
        if "northbound_net_flow" not in metrics:
            return _factor_unavailable(definition, "缺少北向真实净流入")
        net_flow = float(metrics.get("northbound_net_flow", 0.0) or 0.0)
        score = _clamp(net_flow / 80.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"北向净买额={net_flow:.2f}", "数据源=akshare"]}
    if factor_id == "margin_balance_change":
        metrics = _macro()
        if "margin_balance_change_5d" not in metrics:
            return _factor_unavailable(definition, "缺少融资余额变化数据")
        change = float(metrics.get("margin_balance_change_5d", 0.0) or 0.0)
        score = _clamp(change * 10.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"5日融资余额变化={change:.2%}", f"融资余额={float(metrics.get('margin_balance', 0.0) or 0.0):.0f}"]}
    if factor_id == "index_volatility_regime":
        metrics = _macro()
        if "volatility_rank_20d" not in metrics:
            return _factor_unavailable(definition, "缺少市场波动状态样本")
        vol_rank = float(metrics.get("volatility_rank_20d", 0.5) or 0.5)
        vol_value = float(metrics.get("market_volatility_20d", 0.0) or 0.0)
        score = _clamp((0.55 - vol_rank) * 2.4, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"20日波动分位={vol_rank:.0%}", f"市场波动率={vol_value:.2%}"]}
    if factor_id == "credit_spread_macro":
        metrics = _macro()
        if "credit_spread_10y" not in metrics:
            return _factor_unavailable(definition, "缺少信用利差数据")
        spread = float(metrics.get("credit_spread_10y", 0.0) or 0.0)
        score = _clamp((0.45 - spread) / 0.35, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"AAA债-国债10Y={spread:.2f}", f"国债10Y={float(metrics.get('gov_bond_10y', 0.0) or 0.0):.2f}"]}
    if factor_id == "ah_premium_alignment":
        metrics = _ah()
        if not metrics:
            return _factor_unavailable(definition, "当前标的无 A/H 联动数据")
        premium = float(metrics.get("ah_premium", 0.0) or 0.0)
        h_change = float(metrics.get("h_change_pct", 0.0) or 0.0)
        a_change = float(metrics.get("a_change_pct", 0.0) or 0.0)
        leadership = h_change - a_change
        score = _clamp(leadership * 10.0 - max(premium - 0.35, 0.0) * 1.8, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"AH溢价={premium:.0%}", f"H股相对强弱={leadership:.2%}"],
        }
    if factor_id == "us_tech_overnight_map":
        metrics = _us()
        if not metrics:
            return _factor_unavailable(definition, "缺少美股科技隔夜数据")
        sector_text = f"{_resolved_sector(candidate)} {_market_text(context)}".lower()
        relevance = 1.0 if any(keyword in sector_text for keyword in ("半导体", "芯片", "ai", "算力", "软件", "科技", "消费电子", "机器人", "新能源")) else 0.45
        overnight = float(metrics.get("nasdaq_overnight_gap", 0.0) or 0.0)
        session_return = float(metrics.get("nasdaq_session_return", 0.0) or 0.0)
        score = _clamp((overnight * 9.0 + session_return * 6.0) * relevance, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"纳指隔夜跳空={overnight:.2%}", f"纳指日内表现={session_return:.2%}"],
        }
    if factor_id == "commodity_sector_linkage":
        metrics = _commodity()
        if not metrics:
            return _factor_unavailable(definition, "缺少全球商品联动数据")
        sector_text = f"{_resolved_sector(candidate)} {_market_text(context)}".lower()
        commodity_signal = 0.0
        commodity_label = ""
        for label, keywords in {
            "brent_change_pct": ("石油", "油气", "化工", "煤化工"),
            "gold_change_pct": ("黄金", "贵金属"),
            "copper_change_pct": ("铜", "有色", "电网", "电力设备"),
            "aluminum_change_pct": ("铝", "有色"),
            "natural_gas_change_pct": ("天然气", "城燃"),
            "lithium_change_pct": ("锂", "锂电", "电池", "新能源车"),
        }.items():
            if any(keyword in sector_text for keyword in keywords) and label in metrics:
                commodity_signal = float(metrics.get(label, 0.0) or 0.0)
                commodity_label = label.replace("_change_pct", "")
                break
        if not commodity_label:
            return _factor_unavailable(definition, "当前板块缺少可映射商品")
        score = _clamp(commodity_signal * 12.0, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"映射商品={commodity_label}", f"商品涨跌幅={commodity_signal:.2%}"],
        }
    if factor_id == "search_heat_rank":
        metrics = _attention()
        if not metrics:
            return _factor_unavailable(definition, "缺少人气热度数据")
        hot_rank = float(metrics.get("hot_rank", 120.0) or 120.0)
        new_fans = float(metrics.get("new_fans", 0.0) or 0.0)
        core_fans = float(metrics.get("core_fans", 0.0) or 0.0)
        keyword_heat = float(metrics.get("keyword_heat_mean", 0.0) or 0.0)
        rank_component = _clamp((80.0 - hot_rank) / 60.0, -1.0, 1.0)
        fan_component = _clamp(_safe_ratio(new_fans, core_fans + 1.0, 0.0) * 1.8, -1.0, 1.0)
        heat_component = _clamp(keyword_heat / 120.0, 0.0, 1.0)
        score = _clamp(rank_component * 0.5 + fan_component * 0.3 + heat_component * 0.3, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"热榜排名={hot_rank:.0f}", f"新晋/铁杆粉丝比={_safe_ratio(new_fans, core_fans + 1.0, 0.0):.2f}"],
        }
    if factor_id == "news_sentiment_alt":
        metrics = _news()
        if not metrics:
            return _factor_unavailable(definition, "缺少替代舆情数据")
        sentiment = float(metrics.get("news_sentiment_mean", 0.0) or 0.0)
        positive_ratio = float(metrics.get("news_positive_ratio", 0.0) or 0.0)
        negative_ratio = float(metrics.get("news_negative_ratio", 0.0) or 0.0)
        score = _clamp(sentiment * 0.18 + (positive_ratio - negative_ratio) * 1.2, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"近7日正向占比={positive_ratio:.0%}", f"近7日负向占比={negative_ratio:.0%}"],
        }
    if factor_id == "announcement_catalyst_density":
        metrics = _announcement()
        if not metrics:
            return _factor_unavailable(definition, "缺少公告催化密度数据")
        notice_count = float(metrics.get("notice_count_14d", 0.0) or 0.0)
        positive_ratio = float(metrics.get("notice_positive_ratio", 0.0) or 0.0)
        negative_ratio = float(metrics.get("notice_negative_ratio", 0.0) or 0.0)
        score = _clamp(min(notice_count / 6.0, 1.0) * 0.35 + positive_ratio * 0.7 - negative_ratio * 0.8, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"14日公告数={notice_count:.0f}", f"正向公告占比={positive_ratio:.0%}"],
        }
    if factor_id == "roe_trend_quality":
        metrics = _growth()
        if "roe_latest" not in metrics:
            return _factor_unavailable(definition, "缺少 ROE 趋势数据")
        latest = float(metrics.get("roe_latest", 0.0) or 0.0)
        previous = float(metrics.get("roe_previous", latest) or latest)
        delta = latest - previous
        score = _clamp(latest * 4.0 + delta * 10.0 - 0.25, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"最新ROE={latest:.2%}", f"ROE变化={delta:.2%}"],
        }
    if factor_id == "revenue_acceleration_quality":
        metrics = _growth()
        if "revenue_growth_latest" not in metrics:
            return _factor_unavailable(definition, "缺少营收增长趋势数据")
        latest = float(metrics.get("revenue_growth_latest", 0.0) or 0.0)
        previous = float(metrics.get("revenue_growth_previous", latest) or latest)
        acceleration = latest - previous
        score = _clamp(latest * 2.2 + acceleration * 4.0, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"营收增速={latest:.2%}", f"增速变化={acceleration:.2%}"],
        }
    if factor_id == "gross_margin_expansion_quality":
        metrics = _growth()
        if "gross_margin_latest" not in metrics:
            return _factor_unavailable(definition, "缺少毛利率扩张数据")
        latest = float(metrics.get("gross_margin_latest", 0.0) or 0.0)
        previous = float(metrics.get("gross_margin_previous", latest) or latest)
        expansion = latest - previous
        score = _clamp((latest - 0.18) * 2.8 + expansion * 8.0, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"毛利率={latest:.2%}", f"毛利率变化={expansion:.2%}"],
        }
    if factor_id == "analyst_upgrade_intensity":
        metrics = _research()
        if not metrics:
            return _factor_unavailable(definition, "缺少分析师跟踪数据")
        report_count = float(metrics.get("research_report_count", 0.0) or 0.0)
        positive_ratio = float(metrics.get("research_positive_ratio", 0.0) or 0.0)
        month_count = float(metrics.get("research_month_count", 0.0) or 0.0)
        score = _clamp(min(month_count / 12.0, 1.0) * 0.4 + positive_ratio * 0.7 + min(report_count / 8.0, 0.3), -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"近一月研报数={month_count:.0f}", f"正向评级占比={positive_ratio:.0%}"],
        }
    if factor_id == "sector_leader_drive":
        if not sector_metrics:
            return _sector_unavailable()
        return {"score": round(_clamp(sector_metrics.get("sector_strength", 0.0) * 0.6 + sector_metrics.get("leader_rank_score", 0.0) * 0.5 + sector_metrics.get("hot_sector_hit", 0.0) * 0.2, -1.0, 1.0), 4), "evidence": [definition.name, f"板块强度={sector_metrics.get('sector_strength', 0.0):.2f}", f"龙头位次={sector_metrics.get('leader_rank_score', 0.0):.2f}"]}
    if factor_id == "sector_breadth_score":
        if not sector_metrics:
            return _sector_unavailable()
        return {"score": round(_clamp(sector_metrics.get("breadth", 0.0) * 1.6 - 0.5, -1.0, 1.0), 4), "evidence": [definition.name, f"板块广度={sector_metrics.get('breadth', 0.0):.0%}", f"热点命中={int(sector_metrics.get('hot_sector_hit', 0.0))}"]}
    if factor_id == "theme_rotation_speed":
        if not sector_metrics:
            return _sector_unavailable()
        profile = _market_profile_payload(candidate, context)
        rotation_hint = 1.0 if "rotation" in str(profile.get("regime") or "").lower() or "轮动" in _market_text(context) else 0.0
        focus_count = max(len(list(context.get("focus_sectors") or context.get("hot_sectors") or [])), 1)
        return {"score": round(_clamp(rotation_hint * 0.4 + sector_metrics.get("focus_sector_hit", 0.0) * 0.5 + min(focus_count / 5.0, 0.3), -1.0, 1.0), 4), "evidence": [definition.name, f"轮动提示={rotation_hint:.0f}", f"焦点板块数={focus_count}"]}
    if factor_id == "sector_limit_up_ratio":
        if not sector_metrics:
            return _sector_unavailable()
        return {"score": round(_clamp(sector_metrics.get("limit_up_ratio", 0.0) * 8.0, -1.0, 1.0), 4), "evidence": [definition.name, f"板块涨停占比={sector_metrics.get('limit_up_ratio', 0.0):.0%}", f"板块={_resolved_sector(candidate)}"]}
    if factor_id == "peer_follow_strength":
        if not sector_metrics:
            return _sector_unavailable()
        return {"score": round(_clamp(sector_metrics.get("breadth", 0.0) * 0.8 + sector_metrics.get("leader_rank_score", 0.0) * 0.5 - 0.3, -1.0, 1.0), 4), "evidence": [definition.name, f"跟风广度={sector_metrics.get('breadth', 0.0):.0%}", f"龙头位次={sector_metrics.get('leader_rank_score', 0.0):.2f}"]}
    if factor_id == "earnings_surprise_proxy":
        profit_growth = fundamental.get("net_profit_growth", 0.0)
        titles = [str(item.get("title") or "") for item in positives if any(token in str(item.get("title") or "") for token in ("业绩", "预增", "财报", "超预期"))]
        if profit_growth == 0.0 and not titles:
            return _factor_unavailable(definition, "缺少业绩催化数据")
        return {"score": round(_clamp(profit_growth / 60.0 + min(len(titles), 2) * 0.25, -1.0, 1.0), 4), "evidence": [definition.name, f"净利增速={profit_growth:.2f}", *(titles[:2] or ["未显式命中业绩事件"])]}
    if factor_id == "policy_support_score":
        titles = [str(item.get("title") or "") for item in positives if any(token in str(item.get("title") or "") for token in ("政策", "会议", "改革", "扶持"))]
        if not titles:
            return _factor_unavailable(definition, "当前无政策催化命中")
        return {"score": round(_clamp(min(len(titles), 3) * 0.35 + sector_metrics.get("hot_sector_hit", 0.0) * 0.2, 0.0, 1.0), 4), "evidence": [definition.name, *titles[:2]]}
    if factor_id == "order_backlog_catalyst":
        backlog_growth = fundamental.get("order_backlog_growth", 0.0)
        titles = [str(item.get("title") or "") for item in positives if any(token in str(item.get("title") or "") for token in ("订单", "中标", "签约", "出海"))]
        if backlog_growth == 0.0 and not titles:
            return _factor_unavailable(definition, "缺少订单催化数据")
        return {"score": round(_clamp(backlog_growth / 50.0 + min(len(titles), 2) * 0.25, -1.0, 1.0), 4), "evidence": [definition.name, f"在手订单增速={backlog_growth:.2f}", *(titles[:2] or ["未显式命中订单事件"])]}
    if factor_id == "institutional_attention_score":
        institution_metric = fundamental.get("institutional_attention", 0.0)
        titles = [str(item.get("title") or "") for item in positives if any(token in str(item.get("title") or "") for token in ("机构", "调研", "研报", "社保", "公募"))]
        if institution_metric == 0.0 and not titles:
            return _factor_unavailable(definition, "缺少机构关注数据")
        return {"score": round(_clamp(institution_metric / 10.0 + min(len(titles), 2) * 0.25, -1.0, 1.0), 4), "evidence": [definition.name, f"机构关注度={institution_metric:.2f}", *(titles[:2] or ["机构关注来自基础面字段"])]}
    if factor_id == "intraday_trend_stability":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["trend_stability"] / 10.0 - intraday["intraday_volatility"] * 12.0, -1.0, 1.0), 4), "evidence": [definition.name, f"分时稳定度={intraday['trend_stability']:.2f}", f"分时波动={intraday['intraday_volatility']:.2%}"]}
    if factor_id == "order_book_support_proxy":
        snapshot = _latest_order_book(candidate, market_adapter)
        if snapshot is None:
            return _factor_unavailable(definition, "缺少盘口承接快照")
        imbalance = order_book_imbalance(snapshot)
        return {"score": imbalance.score, "evidence": [definition.name, *imbalance.evidence[:2]]}
    if factor_id == "vwap_reclaim_strength":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["vwap_gap"] * 12.0 + intraday["vwap_reclaim"] * 0.4, -1.0, 1.0), 4), "evidence": [definition.name, f"VWAP 偏离={intraday['vwap_gap']:.2%}", f"回收标记={int(intraday['vwap_reclaim'])}"]}
    if factor_id == "afternoon_bid_strength":
        if not intraday:
            return _intraday_unavailable()
        return {"score": round(_clamp(intraday["afternoon_bid_strength"] * 8.0 + intraday["afternoon_volume_share"] * 0.4, -1.0, 1.0), 4), "evidence": [definition.name, f"午后涨幅={intraday['afternoon_bid_strength']:.2%}", f"午后量占比={intraday['afternoon_volume_share']:.0%}"]}
    if factor_id == "breakout_retest_success":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(daily["breakout_pct"] * 8.0 + max(daily["retest_buffer_pct"], -0.03) * 12.0, -1.0, 1.0), 4), "evidence": [definition.name, f"突破幅度={daily['breakout_pct']:.2%}", f"回踩缓冲={daily['retest_buffer_pct']:.2%}"]}
    if factor_id == "drawdown_repair_pressure":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(-(abs(min(daily["drawdown_20d"], 0.0)) * 4.0) - max(-daily["return_3d"], 0.0) * 4.0, -1.0, 0.0), 4), "evidence": [definition.name, f"20日回撤={daily['drawdown_20d']:.2%}", f"3日表现={daily['return_3d']:.2%}"]}
    if factor_id == "volatility_expansion_penalty":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(-(max(daily["volatility_expansion"] - 1.0, 0.0) * 0.8 + daily["volatility_5d"] * 6.0), -1.0, 0.0), 4), "evidence": [definition.name, f"波动扩张={daily['volatility_expansion']:.2f}x", f"5日波动={daily['volatility_5d']:.2%}"]}
    if factor_id == "gap_down_risk":
        if not daily:
            return _daily_unavailable()
        return {"score": round(_clamp(min(daily["gap_pct"], 0.0) * 12.0 + min(daily["intraday_continuation"], 0.0) * 8.0, -1.0, 0.0), 4), "evidence": [definition.name, f"跳空幅度={daily['gap_pct']:.2%}", f"日内延续={daily['intraday_continuation']:.2%}"]}
    if factor_id == "crowding_risk_penalty":
        if not sector_metrics and not daily:
            return _factor_unavailable(definition, "缺少拥挤度样本")
        crowding = sector_metrics.get("limit_up_ratio", 0.0) * 2.0 + max(daily.get("return_10d", 0.0), 0.0) * 4.0
        return {"score": round(_clamp(-crowding, -1.0, 0.0), 4), "evidence": [definition.name, f"板块封板密度={sector_metrics.get('limit_up_ratio', 0.0):.0%}", f"10日涨幅={daily.get('return_10d', 0.0):.2%}"]}
    if factor_id == "stop_loss_distance_penalty":
        if not daily:
            return _daily_unavailable()
        distance = max(1.0 - min(daily["stop_loss_distance_pct"] / 0.12, 1.0), 0.0)
        return {"score": round(_clamp(-distance, -1.0, 0.0), 4), "evidence": [definition.name, f"止损缓冲={daily['stop_loss_distance_pct']:.2%}", f"近10日低点参考={daily_frame['low'].tail(10).min() if not daily_frame.empty else 0.0:.2f}"]}
    if factor_id == "event_shock_penalty":
        if not negatives:
            return _factor_unavailable(definition, "当前无负面事件命中")
        return {"score": round(_clamp(-min(len(negatives), 3) * 0.3, -1.0, 0.0), 4), "evidence": [definition.name, *[str(item.get("title") or "") for item in negatives[:2]]]}
    if factor_id == "pe_percentile_filter":
        pe_value = fundamental.get("pe_ratio", 0.0)
        if pe_value <= 0:
            return _fundamental_unavailable("缺少 PE 数据")
        style = _market_style(candidate, context)
        if style == "value":
            score = _clamp((25.0 - pe_value) / 20.0, -1.0, 1.0)
        elif style == "momentum":
            score = _clamp((50.0 - abs(pe_value - 35.0)) / 35.0, -0.4, 1.0)
        else:
            score = _clamp((35.0 - pe_value) / 30.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"PE={pe_value:.2f}", f"市场风格={style}"]}
    if factor_id == "cashflow_quality_proxy":
        cashflow = fundamental.get("cashflow_quality", 0.0)
        if cashflow == 0.0:
            return _fundamental_unavailable("缺少现金流质量字段")
        return {"score": round(_clamp(cashflow / 1.5, -1.0, 1.0), 4), "evidence": [definition.name, f"经营现金流/利润={cashflow:.2f}", f"标的={candidate.get('symbol', '')}"]}
    if factor_id == "dividend_support_score":
        dividend_yield = fundamental.get("dividend_yield", 0.0)
        if dividend_yield <= 0:
            return _fundamental_unavailable("缺少分红收益率")
        return {"score": round(_clamp(dividend_yield / 0.05, 0.0, 1.0), 4), "evidence": [definition.name, f"股息率={dividend_yield:.2%}", f"标的={candidate.get('symbol', '')}"]}
    if factor_id == "balance_sheet_safety":
        debt = fundamental.get("debt_to_asset", 0.0)
        current_ratio = fundamental.get("current_ratio", 0.0)
        if debt == 0.0 and current_ratio == 0.0:
            return _fundamental_unavailable("缺少资产负债表字段")
        return {"score": round(_clamp((1.0 - min(debt, 1.0)) * 0.7 + min(current_ratio / 2.0, 1.0) * 0.5 - 0.5, -1.0, 1.0), 4), "evidence": [definition.name, f"资产负债率={debt:.2%}", f"流动比率={current_ratio:.2f}"]}
    if factor_id == "portfolio_fit_score":
        if not position_metrics:
            return _factor_unavailable(definition, "缺少持仓上下文")
        return {"score": round(_clamp(position_metrics["diversification_bonus"] + max(daily.get("amount_ratio_5d", 1.0) - 1.0, 0.0) * 0.2 - position_metrics["already_held"] * 0.6, -1.0, 1.0), 4), "evidence": [definition.name, f"持仓数={int(position_metrics['holding_count'])}", f"分散化加分={position_metrics['diversification_bonus']:.2f}"]}
    if factor_id == "correlation_hedge_score":
        if not position_metrics:
            return _factor_unavailable(definition, "缺少持仓上下文")
        return {"score": round(_clamp(position_metrics["diversification_bonus"] - sector_metrics.get("hot_sector_hit", 0.0) * 0.4, -1.0, 1.0), 4), "evidence": [definition.name, f"分散化加分={position_metrics['diversification_bonus']:.2f}", f"热点重叠={sector_metrics.get('hot_sector_hit', 0.0):.0f}"]}
    if factor_id == "replacement_priority_score":
        if not position_metrics:
            return _factor_unavailable(definition, "缺少持仓替换上下文")
        return {"score": round(_clamp(position_metrics["replacement_readiness"] - position_metrics["already_held"] * 0.5, -1.0, 1.0), 4), "evidence": [definition.name, f"替换就绪度={position_metrics['replacement_readiness']:.2f}", f"已持有={int(position_metrics['already_held'])}"]}
    if factor_id == "t0_recycle_potential":
        if not position_metrics or position_metrics["already_held"] <= 0:
            return _factor_unavailable(definition, "当前未持有该标的，无法评估做T")
        return {"score": round(_clamp(position_metrics["t0_space"], 0.0, 1.0), 4), "evidence": [definition.name, f"做T空间={position_metrics['t0_space']:.2f}", f"分时波动={intraday.get('intraday_volatility', 0.0):.2%}"]}
    return _factor_unavailable(definition, "未实现的派生因子")


def _factor_params_schema(group: str, lookback: int = 20) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "lookback": {"type": "integer", "minimum": 1, "default": lookback},
            "timeframes": {"type": "array", "items": {"type": "string"}, "default": [f"{lookback}d"]},
            "threshold": {"type": "number"},
            "group": {"type": "string", "default": group},
        },
        "additionalProperties": True,
    }


def _factor_evidence_schema(group: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "score": {"type": "number"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "group": {"type": "string", "default": group},
        },
        "required": ["score", "evidence"],
        "additionalProperties": True,
    }


def _factor_seed(
    *,
    factor_id: str,
    name: str,
    group: str,
    correlation_group: str = "",
    description: str,
    tags: list[str],
    lookback: int = 20,
) -> FactorDefinition:
    return FactorDefinition(
        id=factor_id,
        name=name,
        version="v1",
        group=group,
        correlation_group=correlation_group,
        description=description,
        params_schema=_factor_params_schema(group, lookback),
        evidence_schema=_factor_evidence_schema(group),
        tags=list(dict.fromkeys([group, *tags])),
        source="seed.factor_library.20260417",
        author="system.factor_library",
        status="active",
    )


def bootstrap_factor_registry() -> None:
    seeds = [
        _factor_seed(factor_id="momentum_slope", name="趋势斜率", group="trend_momentum", correlation_group="trend_cluster", description="衡量趋势延续性，正值表示上行", tags=["momentum", "trend"]),
        _factor_seed(factor_id="main_fund_inflow", name="主力净流入", group="capital_behavior", correlation_group="capital_flow_cluster", description="衡量资金驱动强弱", tags=["capital", "flow"]),
        _factor_seed(factor_id="sector_heat_score", name="板块热度", group="sector_heat", correlation_group="sector_heat_cluster", description="衡量主题与板块扩散强度", tags=["sector", "heat"]),
        _factor_seed(factor_id="breakout_quality", name="突破质量", group="trend_momentum", correlation_group="breakout_cluster", description="衡量放量突破与结构完整度", tags=["breakout", "technical"]),
        _factor_seed(factor_id="news_catalyst_score", name="事件催化强度", group="event_catalyst", description="衡量新闻政策公告催化", tags=["event", "news"]),
        _factor_seed(factor_id="liquidity_risk_penalty", name="流动性风险惩罚", group="risk_penalty", description="对流动性与异常波动做惩罚", tags=["risk", "liquidity"]),
        _factor_seed(factor_id="relative_volume", name="相对量比", group="volume_liquidity", description="当前成交量对比 5 日平均", tags=["volume", "activity"], lookback=5),
        _factor_seed(factor_id="limit_sentiment", name="涨跌停情绪", group="sector_heat", description="标的在涨跌停机制下的活跃情绪", tags=["limit", "sentiment"]),
        _factor_seed(factor_id="price_drawdown_20d", name="20日回撤", group="reversal_repair", description="当前价对比过去 20 日最高价的跌幅", tags=["drawdown", "reversal"]),
        _factor_seed(factor_id="volatility_20d", name="20日波动率", group="risk_penalty", description="过去 20 日日收益率的标准差", tags=["risk", "volatility"]),
        _factor_seed(factor_id="rsi_14", name="RSI-14", group="reversal_repair", description="14 日相对强弱指标", tags=["technical", "momentum"], lookback=14),
        _factor_seed(factor_id="pb_ratio", name="市净率", group="valuation_filter", description="股价相对于每股净资产的倍数", tags=["valuation", "base"]),
        _factor_seed(factor_id="smart_money_q", name="聪明钱Q指标", group="capital_behavior", description="衡量高效率成交的吸筹/派发特征", tags=["capital", "high_freq"]),
        _factor_seed(factor_id="limit_up_popularity", name="涨停人气", group="sector_heat", description="过去 20 日涨停频率", tags=["sentiment", "momentum"]),
        _factor_seed(factor_id="trend_strength_10d", name="10日趋势强度", group="trend_momentum", correlation_group="trend_cluster", description="结合近期动能与前排程度衡量短中期趋势质量", tags=["trend", "strength"], lookback=10),
        _factor_seed(factor_id="moving_average_alignment", name="均线多头排列", group="trend_momentum", correlation_group="trend_cluster", description="用价格偏离与基础分近似均线多头排列程度", tags=["ma", "trend"]),
        _factor_seed(factor_id="trend_consistency_5d", name="5日趋势一致性", group="trend_momentum", correlation_group="trend_cluster", description="衡量趋势连续性与回撤克制程度", tags=["trend", "consistency"], lookback=5),
        _factor_seed(factor_id="relative_strength_rank", name="相对强度排名", group="trend_momentum", correlation_group="trend_cluster", description="衡量标的在候选池中的强势排序", tags=["relative_strength", "ranking"]),
        _factor_seed(factor_id="gap_continuation_score", name="跳空延续度", group="trend_momentum", correlation_group="breakout_cluster", description="衡量高开后继续走强的延续概率", tags=["gap", "continuation"]),
        _factor_seed(factor_id="acceleration_burst_score", name="加速爆发度", group="trend_momentum", description="衡量动能和量能共振带来的加速质量", tags=["acceleration", "burst"]),
        _factor_seed(factor_id="oversold_bounce_strength", name="超跌反抽强度", group="reversal_repair", description="衡量超跌后出现修复反弹的力度", tags=["oversold", "bounce"]),
        _factor_seed(factor_id="mean_reversion_gap", name="均值回归偏离", group="reversal_repair", description="衡量价格偏离均值后的修复空间", tags=["reversion", "deviation"]),
        _factor_seed(factor_id="support_reclaim_score", name="支撑位收复", group="reversal_repair", description="衡量回撤后是否重新站回关键支撑", tags=["support", "reclaim"]),
        _factor_seed(factor_id="intraday_reversal_strength", name="分时反转强度", group="reversal_repair", description="衡量分时低点修复与尾盘拉回能力", tags=["intraday", "reversal"]),
        _factor_seed(factor_id="bear_trap_escape", name="假摔脱困", group="reversal_repair", description="衡量快速下探后的收复能力", tags=["bear_trap", "repair"]),
        _factor_seed(factor_id="low_volume_pullback_quality", name="缩量回调质量", group="reversal_repair", description="衡量强势股缩量回调后的再起潜力", tags=["pullback", "low_volume"]),
        _factor_seed(factor_id="turnover_acceleration", name="换手加速度", group="volume_liquidity", description="衡量换手活跃度对趋势持续性的支撑", tags=["turnover", "activity"]),
        _factor_seed(factor_id="volume_breakout_confirmation", name="放量突破确认", group="volume_liquidity", description="衡量突破时是否伴随有效量能放大", tags=["volume", "breakout"]),
        _factor_seed(factor_id="volume_contraction_signal", name="缩量企稳信号", group="volume_liquidity", description="衡量缩量后的抛压衰减与承接改善", tags=["volume", "contraction"]),
        _factor_seed(factor_id="liquidity_depth_score", name="流动性深度", group="volume_liquidity", description="衡量成交深度是否足以支撑进出场", tags=["liquidity", "depth"]),
        _factor_seed(factor_id="opening_volume_impulse", name="开盘量能脉冲", group="volume_liquidity", description="衡量开盘时段的量能冲击与关注度", tags=["opening", "volume"]),
        _factor_seed(factor_id="northbound_flow_proxy", name="北向偏好代理", group="capital_behavior", correlation_group="capital_flow_cluster", description="用流动性与稳定度近似大资金偏好", tags=["northbound", "capital"]),
        _factor_seed(factor_id="large_order_persistence", name="大单持续性", group="capital_behavior", correlation_group="capital_flow_cluster", description="衡量大资金连续推动的概率", tags=["large_order", "flow"]),
        _factor_seed(factor_id="main_fund_turning_point", name="主力拐点", group="capital_behavior", correlation_group="capital_flow_cluster", description="衡量主力资金由防守转进攻的拐点", tags=["capital", "turning_point"]),
        _factor_seed(factor_id="chip_concentration_proxy", name="筹码集中度代理", group="capital_behavior", description="衡量筹码收敛与上方抛压情况", tags=["chips", "concentration"]),
        _factor_seed(factor_id="intraday_capital_reflow", name="盘中资金回流", group="capital_behavior", description="衡量午后或尾盘资金回流强度", tags=["intraday", "reflow"]),
        _factor_seed(factor_id="sector_leader_drive", name="板块龙头带动", group="sector_heat", correlation_group="sector_heat_cluster", description="衡量板块龙头对后排扩散的带动能力", tags=["sector", "leader"]),
        _factor_seed(factor_id="sector_breadth_score", name="板块广度", group="sector_heat", correlation_group="sector_heat_cluster", description="衡量板块内上涨家数和扩散广度", tags=["sector", "breadth"]),
        _factor_seed(factor_id="theme_rotation_speed", name="主题轮动速度", group="sector_heat", description="衡量热点主题是否处于快速轮动态", tags=["theme", "rotation"]),
        _factor_seed(factor_id="sector_limit_up_ratio", name="板块涨停占比", group="sector_heat", correlation_group="sector_heat_cluster", description="衡量同题材强势股的封板密度", tags=["sector", "limit_up"]),
        _factor_seed(factor_id="peer_follow_strength", name="同伴跟随强度", group="sector_heat", correlation_group="sector_heat_cluster", description="衡量板块内部跟风股的跟随能力", tags=["peer", "follow"]),
        _factor_seed(factor_id="earnings_surprise_proxy", name="业绩惊喜代理", group="event_catalyst", description="衡量业绩超预期带来的催化强度", tags=["earnings", "surprise"]),
        _factor_seed(factor_id="policy_support_score", name="政策支持度", group="event_catalyst", description="衡量政策方向对题材的扶持力度", tags=["policy", "support"]),
        _factor_seed(factor_id="order_backlog_catalyst", name="订单催化强度", group="event_catalyst", description="衡量大订单与景气度提升的催化程度", tags=["order", "catalyst"]),
        _factor_seed(factor_id="institutional_attention_score", name="机构关注度", group="event_catalyst", description="衡量机构关注与研报催化带来的持续性", tags=["institution", "attention"]),
        _factor_seed(factor_id="intraday_trend_stability", name="分时趋势稳定度", group="micro_structure", description="衡量分时拉升过程是否平滑稳定", tags=["micro", "trend"]),
        _factor_seed(factor_id="order_book_support_proxy", name="盘口承接代理", group="micro_structure", description="用流动性和价格韧性近似盘口承接", tags=["order_book", "support"]),
        _factor_seed(factor_id="order_book_imbalance", name="盘口失衡度", group="micro_structure", description="衡量盘口买卖深度失衡", tags=["order_book", "imbalance"]),
        _factor_seed(factor_id="large_order_flow", name="大单流向", group="micro_structure", description="衡量大单买卖流向偏置", tags=["large_order", "flow"]),
        _factor_seed(factor_id="vwap_reclaim_strength", name="VWAP 收复强度", group="micro_structure", correlation_group="breakout_cluster", description="衡量跌破均价后重新站回的能力", tags=["vwap", "reclaim"]),
        _factor_seed(factor_id="afternoon_bid_strength", name="午后承接强度", group="micro_structure", description="衡量午后资金接力与回流意愿", tags=["afternoon", "bid"]),
        _factor_seed(factor_id="breakout_retest_success", name="突破回踩成功率", group="micro_structure", correlation_group="breakout_cluster", description="衡量突破后回踩不破的稳定度", tags=["breakout", "retest"]),
        _factor_seed(factor_id="drawdown_repair_pressure", name="回撤修复压力", group="risk_penalty", description="衡量深度回撤后继续承压的风险", tags=["drawdown", "pressure"]),
        _factor_seed(factor_id="volatility_expansion_penalty", name="波动扩张惩罚", group="risk_penalty", description="衡量波动骤然扩大的风险惩罚", tags=["volatility", "penalty"]),
        _factor_seed(factor_id="gap_down_risk", name="跳空低开风险", group="risk_penalty", description="衡量低开缺口和承接不足带来的风险", tags=["gap_down", "risk"]),
        _factor_seed(factor_id="crowding_risk_penalty", name="拥挤度惩罚", group="risk_penalty", description="衡量高热度拥挤交易的回撤风险", tags=["crowding", "risk"]),
        _factor_seed(factor_id="stop_loss_distance_penalty", name="止损距离惩罚", group="risk_penalty", description="衡量离合理止损位过远的交易风险", tags=["stop_loss", "risk"]),
        _factor_seed(factor_id="event_shock_penalty", name="事件冲击惩罚", group="risk_penalty", description="衡量负面消息和突发事件的冲击风险", tags=["event", "shock"]),
        _factor_seed(factor_id="pe_percentile_filter", name="PE 分位过滤", group="valuation_filter", description="衡量估值在历史分位上的安全边际", tags=["pe", "percentile"]),
        _factor_seed(factor_id="cashflow_quality_proxy", name="现金流质量代理", group="valuation_filter", description="衡量盈利质量与现金回款能力", tags=["cashflow", "quality"]),
        _factor_seed(factor_id="dividend_support_score", name="分红支撑度", group="valuation_filter", description="衡量分红稳定性对估值的支撑", tags=["dividend", "support"]),
        _factor_seed(factor_id="balance_sheet_safety", name="资产负债安全度", group="valuation_filter", description="衡量财务结构稳健程度", tags=["balance_sheet", "safety"]),
        _factor_seed(factor_id="portfolio_fit_score", name="组合适配度", group="position_management", description="衡量标的加入当前组合后的适配程度", tags=["portfolio", "fit"]),
        _factor_seed(factor_id="correlation_hedge_score", name="相关性对冲度", group="position_management", description="衡量与现有持仓的分散和对冲价值", tags=["correlation", "hedge"]),
        _factor_seed(factor_id="replacement_priority_score", name="替换优先级", group="position_management", description="衡量该票用于换仓替代的优先级", tags=["replacement", "priority"]),
        _factor_seed(factor_id="t0_recycle_potential", name="做T回转潜力", group="position_management", description="衡量盘中做 T 和滚动回转的空间", tags=["t0", "recycle"]),
        _factor_seed(factor_id="counterparty_strength", name="对手盘强度", group="micro_structure", description="识别对手盘是否偏机构主导", tags=["counterparty", "institutional"]),
        _factor_seed(factor_id="chip_profit_ratio", name="获利盘比例", group="chip_distribution", description="基于历史成交量加权估算当前筹码获利比例", tags=["chips", "profit"]),
        _factor_seed(factor_id="chip_cost_peak_distance", name="筹码峰值距离", group="chip_distribution", description="当前价相对筹码成本峰值的位置", tags=["chips", "cost_peak"]),
        _factor_seed(factor_id="chip_concentration_20d", name="20日筹码集中度", group="chip_distribution", description="过去20日成交量在价格区间上的集中程度", tags=["chips", "concentration"]),
        _factor_seed(factor_id="chip_turnover_rate_20d", name="20日换手率", group="chip_distribution", description="基于真实流通股本估算过去20日累计换手率", tags=["chips", "turnover"]),
        _factor_seed(factor_id="market_breadth_index", name="市场广度指数", group="macro_environment", description="全市场上涨家数占比与平均涨幅", tags=["macro", "breadth"]),
        _factor_seed(factor_id="northbound_actual_flow", name="北向真实净流入", group="macro_environment", description="基于港股通/沪深股通汇总的北向净买额", tags=["macro", "northbound"]),
        _factor_seed(factor_id="margin_balance_change", name="融资余额变化", group="macro_environment", description="沪深两市融资余额5日变化率", tags=["macro", "margin"]),
        _factor_seed(factor_id="index_volatility_regime", name="市场波动率状态", group="macro_environment", description="主板横截面等权收益构建的20日波动分位", tags=["macro", "volatility"]),
        _factor_seed(factor_id="credit_spread_macro", name="信用利差", group="macro_environment", description="AAA商业银行债与国债10年期收益率利差", tags=["macro", "credit_spread"]),
        _factor_seed(factor_id="ah_premium_alignment", name="AH 溢价联动", group="cross_market", description="A/H 双地上市标的的溢价与相对强弱联动", tags=["cross_market", "ah"]),
        _factor_seed(factor_id="us_tech_overnight_map", name="美股科技隔夜映射", group="cross_market", description="纳指隔夜表现向 A 股科技成长板块的映射强度", tags=["cross_market", "us_tech"]),
        _factor_seed(factor_id="commodity_sector_linkage", name="商品-板块联动", group="cross_market", description="全球商品涨跌对 A 股资源/能源/锂电链条的联动映射", tags=["cross_market", "commodity"]),
        _factor_seed(factor_id="search_heat_rank", name="搜索热度排名", group="alternative_data", description="东财热榜与关键词热度的人气信号", tags=["alternative", "attention"]),
        _factor_seed(factor_id="news_sentiment_alt", name="替代舆情情绪", group="alternative_data", description="基于新闻标题与正文关键词的替代情绪打分", tags=["alternative", "news"]),
        _factor_seed(factor_id="announcement_catalyst_density", name="公告催化密度", group="alternative_data", description="近两周公告密度与正负催化分布", tags=["alternative", "announcement"]),
        _factor_seed(factor_id="roe_trend_quality", name="ROE 趋势质量", group="growth_quality", description="净资产收益率水平与趋势变化", tags=["growth", "roe"]),
        _factor_seed(factor_id="revenue_acceleration_quality", name="营收加速度质量", group="growth_quality", description="营业收入增速与加速度变化", tags=["growth", "revenue"]),
        _factor_seed(factor_id="gross_margin_expansion_quality", name="毛利率扩张质量", group="growth_quality", description="毛利率水平及扩张趋势", tags=["growth", "gross_margin"]),
        _factor_seed(factor_id="analyst_upgrade_intensity", name="分析师上调强度", group="growth_quality", description="近期研报覆盖密度与正向评级占比", tags=["growth", "analyst"]),
    ]
    executors = {
        "momentum_slope": _momentum_slope_executor,
        "main_fund_inflow": _main_fund_inflow_executor,
        "sector_heat_score": _sector_heat_score_executor,
        "breakout_quality": _breakout_quality_executor,
        "news_catalyst_score": _news_catalyst_score_executor,
        "liquidity_risk_penalty": _liquidity_risk_penalty_executor,
        "relative_volume": _relative_volume_executor,
        "limit_sentiment": _limit_sentiment_executor,
        "price_drawdown_20d": _price_drawdown_20d_executor,
        "volatility_20d": _volatility_20d_executor,
        "rsi_14": _rsi_14_executor,
        "pb_ratio": _pb_ratio_executor,
        "smart_money_q": _smart_money_executor,
        "limit_up_popularity": _limit_up_popularity_executor,
        "trend_strength_10d": _derived_factor_executor,
        "moving_average_alignment": _derived_factor_executor,
        "trend_consistency_5d": _derived_factor_executor,
        "relative_strength_rank": _derived_factor_executor,
        "gap_continuation_score": _derived_factor_executor,
        "acceleration_burst_score": _derived_factor_executor,
        "oversold_bounce_strength": _derived_factor_executor,
        "mean_reversion_gap": _derived_factor_executor,
        "support_reclaim_score": _derived_factor_executor,
        "intraday_reversal_strength": _derived_factor_executor,
        "bear_trap_escape": _derived_factor_executor,
        "low_volume_pullback_quality": _derived_factor_executor,
        "turnover_acceleration": _derived_factor_executor,
        "volume_breakout_confirmation": _derived_factor_executor,
        "volume_contraction_signal": _derived_factor_executor,
        "liquidity_depth_score": _derived_factor_executor,
        "opening_volume_impulse": _derived_factor_executor,
        "northbound_flow_proxy": _derived_factor_executor,
        "large_order_persistence": _derived_factor_executor,
        "main_fund_turning_point": _derived_factor_executor,
        "chip_concentration_proxy": _derived_factor_executor,
        "intraday_capital_reflow": _derived_factor_executor,
        "sector_leader_drive": _derived_factor_executor,
        "sector_breadth_score": _derived_factor_executor,
        "theme_rotation_speed": _derived_factor_executor,
        "sector_limit_up_ratio": _derived_factor_executor,
        "peer_follow_strength": _derived_factor_executor,
        "earnings_surprise_proxy": _derived_factor_executor,
        "policy_support_score": _derived_factor_executor,
        "order_backlog_catalyst": _derived_factor_executor,
        "institutional_attention_score": _derived_factor_executor,
        "intraday_trend_stability": _derived_factor_executor,
        "order_book_support_proxy": _derived_factor_executor,
        "order_book_imbalance": _order_book_imbalance_executor,
        "large_order_flow": _large_order_flow_executor,
        "vwap_reclaim_strength": _derived_factor_executor,
        "afternoon_bid_strength": _derived_factor_executor,
        "breakout_retest_success": _derived_factor_executor,
        "drawdown_repair_pressure": _derived_factor_executor,
        "volatility_expansion_penalty": _derived_factor_executor,
        "gap_down_risk": _derived_factor_executor,
        "crowding_risk_penalty": _derived_factor_executor,
        "stop_loss_distance_penalty": _derived_factor_executor,
        "event_shock_penalty": _derived_factor_executor,
        "pe_percentile_filter": _derived_factor_executor,
        "cashflow_quality_proxy": _derived_factor_executor,
        "dividend_support_score": _derived_factor_executor,
        "balance_sheet_safety": _derived_factor_executor,
        "portfolio_fit_score": _derived_factor_executor,
        "correlation_hedge_score": _derived_factor_executor,
        "replacement_priority_score": _derived_factor_executor,
        "t0_recycle_potential": _derived_factor_executor,
        "counterparty_strength": _counterparty_strength_executor,
        "chip_profit_ratio": _derived_factor_executor,
        "chip_cost_peak_distance": _derived_factor_executor,
        "chip_concentration_20d": _derived_factor_executor,
        "chip_turnover_rate_20d": _derived_factor_executor,
        "market_breadth_index": _derived_factor_executor,
        "northbound_actual_flow": _derived_factor_executor,
        "margin_balance_change": _derived_factor_executor,
        "index_volatility_regime": _derived_factor_executor,
        "credit_spread_macro": _derived_factor_executor,
        "ah_premium_alignment": _derived_factor_executor,
        "us_tech_overnight_map": _derived_factor_executor,
        "commodity_sector_linkage": _derived_factor_executor,
        "search_heat_rank": _derived_factor_executor,
        "news_sentiment_alt": _derived_factor_executor,
        "announcement_catalyst_density": _derived_factor_executor,
        "roe_trend_quality": _derived_factor_executor,
        "revenue_acceleration_quality": _derived_factor_executor,
        "gross_margin_expansion_quality": _derived_factor_executor,
        "analyst_upgrade_intensity": _derived_factor_executor,
    }
    for item in seeds:
        if factor_registry.get(item.id, item.version) is None:
            factor_registry.register(item, executor=executors.get(item.id))
