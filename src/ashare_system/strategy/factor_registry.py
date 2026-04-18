"""因子注册表 - 为 runtime compose 提供可发现的因子菜单。"""

from __future__ import annotations

import numpy as np
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..logging_config import get_logger
from .atomic_repository import StrategyRepositoryEntry, strategy_atomic_repository

logger = get_logger("strategy.factor_registry")


class FactorDefinition(BaseModel):
    id: str
    name: str
    version: str = "v1"
    group: str
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


def _selection_score(candidate: dict[str, Any]) -> float:
    return float(candidate.get("selection_score", 0.0) or 0.0)


def _score_breakdown(candidate: dict[str, Any]) -> dict[str, Any]:
    return dict(candidate.get("score_breakdown") or {})


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


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
    symbol = str(candidate.get("symbol") or "")
    # R0.9: 优先使用预计算的因子值
    if precomputed_factors and "return_20d" in precomputed_factors:
        ret_20d = float(precomputed_factors["return_20d"] or 0.0)
        score = _clamp(ret_20d * 5.0, -1.0, 1.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, f"基于预计算20日收益率: {ret_20d:.2%}"],
        }

    if market_adapter and symbol:
        try:
            # 真实计算：获取20日日线计算斜率
            bars = market_adapter.get_bars([symbol], period="1d", count=20, end_time=trade_date)
            if len(bars) >= 5:
                closes = [b.close for b in bars]
                ret_20d = (closes[-1] - closes[0]) / closes[0]
                score = _clamp(ret_20d * 5.0, -1.0, 1.0) # 放大系数
                return {
                    "score": round(score, 4),
                    "evidence": [definition.name, f"基于真实20日日线计算收益率: {ret_20d:.2%}"],
                }
        except Exception as e:
            logger.warning("momentum_slope 真实计算失败: %s", e)

    # 回落到简化逻辑
    score = _clamp(
        (
            (
                float(_score_breakdown(candidate).get("momentum_pct", 0.0) or 0.0) / 8.0
                if _score_breakdown(candidate).get("momentum_pct") is not None
                else _selection_score(candidate) / 100.0
            )
            + (
                float(_score_breakdown(candidate).get("stability_score", 0.0) or 0.0) / 4.0
                if _score_breakdown(candidate).get("stability_score") is not None
                else _selection_score(candidate) / 100.0
            )
        )
        / 2.0,
        -1.0,
        1.0,
    )
    return {
        "score": round(score, 4),
        "evidence": [definition.name, "基于动能百分比与稳定度联合评估趋势斜率 (简化)"],
    }


def _relative_volume_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """量比因子: 当前成交量相对于过去 5 日平均水平。"""
    symbol = str(candidate.get("symbol") or "")
    if precomputed_factors and "relative_volume" in precomputed_factors:
        rel_vol = float(precomputed_factors["relative_volume"] or 1.0)
        score = _clamp((rel_vol - 1.0) / 2.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"量比: {rel_vol:.2f}"]}

    if market_adapter and symbol:
        try:
            bars = market_adapter.get_bars([symbol], period="1d", count=6, end_time=trade_date)
            if len(bars) >= 5:
                volumes = [b.volume for b in bars]
                current_vol = volumes[-1]
                avg_vol = sum(volumes[:-1]) / (len(volumes) - 1)
                rel_vol = current_vol / avg_vol if avg_vol > 0 else 1.0
                score = _clamp((rel_vol - 1.0) / 2.0, -1.0, 1.0)
                return {"score": round(score, 4), "evidence": [definition.name, f"实时量比: {rel_vol:.2f}"]}
        except Exception:
            pass
    
    score = _clamp((float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 7.0) - 7.0) / 7.0, -1.0, 1.0)
    return {"score": round(score, 4), "evidence": [definition.name, "基于流动性得分估计量比"]}


def _limit_sentiment_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """涨跌停情绪: 衡量标的在 A 股特有的涨停/跌停机制下的活跃度。"""
    snapshot = dict(candidate.get("market_snapshot") or {})
    last_price = float(snapshot.get("last_price", 0.0) or 0.0)
    pre_close = float(snapshot.get("pre_close", 0.0) or 0.0)
    
    if last_price > 0 and pre_close > 0:
        change_pct = (last_price - pre_close) / pre_close
        # 接近 10% 涨停时分数极高，接近 -10% 跌停时分数极低
        score = _clamp(change_pct * 10.0, -1.0, 1.0)
        return {"score": round(score, 4), "evidence": [definition.name, f"当前涨跌幅: {change_pct:.2%}"]}
    
    return {"score": 0.0, "evidence": [definition.name, "缺少价格快照，无法评估涨跌停情绪"]}


def _smart_money_executor(
    definition: FactorDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """聪明钱Q指标: 衡量高效率分钟成交的价位。Q < 1 表示聪明钱在低位成交。"""
    symbol = str(candidate.get("symbol") or "")
    if market_adapter and symbol:
        try:
            # 获取分时线
            bars = market_adapter.get_bars([symbol], period="1m", count=240, end_time=trade_date)
            if len(bars) >= 100:
                closes = np.array([b.close for b in bars])
                volumes = np.array([b.volume for b in bars])
                returns = np.abs(np.diff(closes) / closes[:-1])
                # S = |ret| / sqrt(vol)
                s_idx = returns / np.sqrt(volumes[1:] + 1e-9)
                
                # 排序并取前 20%
                threshold = np.percentile(s_idx, 80)
                smart_mask = s_idx >= threshold
                
                # 计算 VWAP
                vwap_smart = np.sum(closes[1:][smart_mask] * volumes[1:][smart_mask]) / (np.sum(volumes[1:][smart_mask]) + 1e-9)
                vwap_all = np.sum(closes * volumes) / (np.sum(volumes) + 1e-9)
                
                q_value = vwap_smart / vwap_all if vwap_all > 0 else 1.0
                # Q 越小分数越高
                score = _clamp((1.0 - q_value) * 50.0, -1.0, 1.0)
                
                return {
                    "score": round(score, 4),
                    "evidence": [definition.name, f"Q指标: {q_value:.4f} (越小代表主力低位吸筹)"],
                }
        except Exception as e:
            logger.warning("smart_money 真实计算失败: %s", e)

    return {"score": 0.0, "evidence": [definition.name, "缺少分时数据或计算失败"]}


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


def _factor_params_schema(group: str, lookback: int = 20) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "lookback": {"type": "integer", "minimum": 1, "default": lookback},
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
    description: str,
    tags: list[str],
    lookback: int = 20,
) -> FactorDefinition:
    return FactorDefinition(
        id=factor_id,
        name=name,
        version="v1",
        group=group,
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
        _factor_seed(factor_id="momentum_slope", name="趋势斜率", group="trend_momentum", description="衡量趋势延续性，正值表示上行", tags=["momentum", "trend"]),
        _factor_seed(factor_id="main_fund_inflow", name="主力净流入", group="capital_behavior", description="衡量资金驱动强弱", tags=["capital", "flow"]),
        _factor_seed(factor_id="sector_heat_score", name="板块热度", group="sector_heat", description="衡量主题与板块扩散强度", tags=["sector", "heat"]),
        _factor_seed(factor_id="breakout_quality", name="突破质量", group="trend_momentum", description="衡量放量突破与结构完整度", tags=["breakout", "technical"]),
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
        _factor_seed(factor_id="trend_strength_10d", name="10日趋势强度", group="trend_momentum", description="结合近期动能与前排程度衡量短中期趋势质量", tags=["trend", "strength"], lookback=10),
        _factor_seed(factor_id="moving_average_alignment", name="均线多头排列", group="trend_momentum", description="用价格偏离与基础分近似均线多头排列程度", tags=["ma", "trend"]),
        _factor_seed(factor_id="trend_consistency_5d", name="5日趋势一致性", group="trend_momentum", description="衡量趋势连续性与回撤克制程度", tags=["trend", "consistency"], lookback=5),
        _factor_seed(factor_id="relative_strength_rank", name="相对强度排名", group="trend_momentum", description="衡量标的在候选池中的强势排序", tags=["relative_strength", "ranking"]),
        _factor_seed(factor_id="gap_continuation_score", name="跳空延续度", group="trend_momentum", description="衡量高开后继续走强的延续概率", tags=["gap", "continuation"]),
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
        _factor_seed(factor_id="northbound_flow_proxy", name="北向偏好代理", group="capital_behavior", description="用流动性与稳定度近似大资金偏好", tags=["northbound", "capital"]),
        _factor_seed(factor_id="large_order_persistence", name="大单持续性", group="capital_behavior", description="衡量大资金连续推动的概率", tags=["large_order", "flow"]),
        _factor_seed(factor_id="main_fund_turning_point", name="主力拐点", group="capital_behavior", description="衡量主力资金由防守转进攻的拐点", tags=["capital", "turning_point"]),
        _factor_seed(factor_id="chip_concentration_proxy", name="筹码集中度代理", group="capital_behavior", description="衡量筹码收敛与上方抛压情况", tags=["chips", "concentration"]),
        _factor_seed(factor_id="intraday_capital_reflow", name="盘中资金回流", group="capital_behavior", description="衡量午后或尾盘资金回流强度", tags=["intraday", "reflow"]),
        _factor_seed(factor_id="sector_leader_drive", name="板块龙头带动", group="sector_heat", description="衡量板块龙头对后排扩散的带动能力", tags=["sector", "leader"]),
        _factor_seed(factor_id="sector_breadth_score", name="板块广度", group="sector_heat", description="衡量板块内上涨家数和扩散广度", tags=["sector", "breadth"]),
        _factor_seed(factor_id="theme_rotation_speed", name="主题轮动速度", group="sector_heat", description="衡量热点主题是否处于快速轮动态", tags=["theme", "rotation"]),
        _factor_seed(factor_id="sector_limit_up_ratio", name="板块涨停占比", group="sector_heat", description="衡量同题材强势股的封板密度", tags=["sector", "limit_up"]),
        _factor_seed(factor_id="peer_follow_strength", name="同伴跟随强度", group="sector_heat", description="衡量板块内部跟风股的跟随能力", tags=["peer", "follow"]),
        _factor_seed(factor_id="earnings_surprise_proxy", name="业绩惊喜代理", group="event_catalyst", description="衡量业绩超预期带来的催化强度", tags=["earnings", "surprise"]),
        _factor_seed(factor_id="policy_support_score", name="政策支持度", group="event_catalyst", description="衡量政策方向对题材的扶持力度", tags=["policy", "support"]),
        _factor_seed(factor_id="order_backlog_catalyst", name="订单催化强度", group="event_catalyst", description="衡量大订单与景气度提升的催化程度", tags=["order", "catalyst"]),
        _factor_seed(factor_id="institutional_attention_score", name="机构关注度", group="event_catalyst", description="衡量机构关注与研报催化带来的持续性", tags=["institution", "attention"]),
        _factor_seed(factor_id="intraday_trend_stability", name="分时趋势稳定度", group="micro_structure", description="衡量分时拉升过程是否平滑稳定", tags=["micro", "trend"]),
        _factor_seed(factor_id="order_book_support_proxy", name="盘口承接代理", group="micro_structure", description="用流动性和价格韧性近似盘口承接", tags=["order_book", "support"]),
        _factor_seed(factor_id="vwap_reclaim_strength", name="VWAP 收复强度", group="micro_structure", description="衡量跌破均价后重新站回的能力", tags=["vwap", "reclaim"]),
        _factor_seed(factor_id="afternoon_bid_strength", name="午后承接强度", group="micro_structure", description="衡量午后资金接力与回流意愿", tags=["afternoon", "bid"]),
        _factor_seed(factor_id="breakout_retest_success", name="突破回踩成功率", group="micro_structure", description="衡量突破后回踩不破的稳定度", tags=["breakout", "retest"]),
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
    ]
    executors = {
        "momentum_slope": _momentum_slope_executor,
        "main_fund_inflow": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (
                    min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)
                    + min(float(candidate.get("rank", 99) == 1), 1.0) * 0.15
                ),
                0.0,
                1.0,
            ),
            "基于流动性得分与前排强度近似主力资金强度",
        ),
        "sector_heat_score": _simple_factor_executor(
            lambda candidate, context: 0.85 if str(candidate.get("resolved_sector") or "") in set(context.get("hot_sectors", [])) else 0.45,
            "基于候选所属方向是否命中热点板块",
        ),
        "breakout_quality": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (
                    min(max(float(_score_breakdown(candidate).get("price_bias_score", 0.0) or 0.0) / 6.0, 0.0), 1.0)
                    + min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)
                )
                / 2.0,
                0.0,
                1.0,
            ),
            "基于价格偏移与量能共振联合评估突破质量",
        ),
        "news_catalyst_score": _simple_factor_executor(
            lambda _candidate, context: 0.7 if context.get("market_hypothesis") else 0.35,
            "基于当前市场假设与主题推断事件催化强度",
        ),
        "liquidity_risk_penalty": _simple_factor_executor(
            lambda candidate, _context: -max(0.0, 1.0 - min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)),
            "基于流动性不足时增加风险惩罚",
        ),
        "relative_volume": _relative_volume_executor,
        "limit_sentiment": _limit_sentiment_executor,
        "price_drawdown_20d": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                float(_score_breakdown(candidate).get("price_drawdown_20d", 0.0) or 0.0) * -2.0,
                -1.0,
                1.0,
            ),
            "基于20日价格回撤幅度评估风险",
        ),
        "volatility_20d": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (float(_score_breakdown(candidate).get("volatility_20d", 0.0) or 0.03) - 0.03) / 0.03,
                -1.0,
                1.0,
            ),
            "衡量近期收益率波动的稳定性",
        ),
        "rsi_14": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (float(_score_breakdown(candidate).get("rsi_14", 50.0) or 50.0) - 50.0) / 30.0,
                -1.0,
                1.0,
            ),
            "基于 RSI 指标评估超买超卖状态",
        ),
        "pb_ratio": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                1.0 / (float(_score_breakdown(candidate).get("pb_ratio", 2.0) or 2.0) + 1e-9),
                0.0,
                1.0,
            ),
            "偏好低市净率标的 (价值属性)",
        ),
        "smart_money_q": _smart_money_executor,
        "limit_up_popularity": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                float(_score_breakdown(candidate).get("limit_up_count_20d", 0.0) or 0.0) / 5.0,
                0.0,
                1.0,
            ),
            "基于近期涨停次数评估市场人气与连板潜力",
        ),
        "trend_strength_10d": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "momentum_pct") / 6.0 + _rank_strength(candidate, 8)) / 2.0,
                0.0,
                1.0,
            ),
            "基于近端动能与候选排序评估 10 日趋势强度",
        ),
        "moving_average_alignment": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "price_bias_score") / 4.0 + _selection_quality(candidate)) / 2.0,
                -1.0,
                1.0,
            ),
            "基于价格偏移与基础分近似均线多头排列",
        ),
        "trend_consistency_5d": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "stability_score", default=7.0) / 10.0 + _selection_quality(candidate)) / 2.0,
                0.0,
                1.0,
            ),
            "基于稳定度与基础分评估趋势连续性",
        ),
        "relative_strength_rank": _simple_factor_executor(
            lambda candidate, _context: _clamp(_rank_strength(candidate, 15), 0.0, 1.0),
            "基于候选池排名衡量相对强度",
        ),
        "gap_continuation_score": _simple_factor_executor(
            lambda candidate, _context: _clamp((_market_change_pct(candidate) + 0.02) / 0.08, -1.0, 1.0),
            "基于当日涨跌幅近似跳空后的延续力度",
        ),
        "acceleration_burst_score": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (
                    _score_breakdown_float(candidate, "momentum_pct") / 8.0
                    + _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0
                )
                / 2.0,
                0.0,
                1.0,
            ),
            "基于动能与量能共振评估加速爆发度",
        ),
        "oversold_bounce_strength": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                abs(min(_score_breakdown_float(candidate, "price_drawdown_20d"), 0.0)) * 3.0
                + max(_market_change_pct(candidate), 0.0) * 4.0,
                0.0,
                1.0,
            ),
            "基于超跌幅度与当日修复幅度评估反抽强度",
        ),
        "mean_reversion_gap": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                abs(_score_breakdown_float(candidate, "price_bias_score")) / 4.0,
                0.0,
                1.0,
            ),
            "基于价格偏离均值程度评估均值回归空间",
        ),
        "support_reclaim_score": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (max(_market_change_pct(candidate), 0.0) * 4.0 + _score_breakdown_float(candidate, "stability_score", default=5.0) / 12.0),
                0.0,
                1.0,
            ),
            "基于当日修复与稳定度近似支撑位收复质量",
        ),
        "intraday_reversal_strength": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (max(_market_change_pct(candidate), -0.03) + 0.03) / 0.08,
                -1.0,
                1.0,
            ),
            "基于分时涨跌幅修复近似反转强度",
        ),
        "bear_trap_escape": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                abs(min(_score_breakdown_float(candidate, "price_drawdown_20d"), 0.0)) * 2.0
                + max(_market_change_pct(candidate), 0.0) * 3.0,
                0.0,
                1.0,
            ),
            "基于回撤后拉回能力评估假摔脱困概率",
        ),
        "low_volume_pullback_quality": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (
                    abs(min(_score_breakdown_float(candidate, "price_drawdown_20d"), 0.0)) * 1.5
                    + max(0.0, 1.0 - _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0)
                )
                / 1.5,
                0.0,
                1.0,
            ),
            "基于回调深度与量能收缩近似缩量回调质量",
        ),
        "turnover_acceleration": _simple_factor_executor(
            lambda candidate, _context: _clamp(_score_breakdown_float(candidate, "liquidity_score", default=7.0) / 10.0, 0.0, 1.0),
            "基于流动性得分近似换手加速度",
        ),
        "volume_breakout_confirmation": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (
                    _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0
                    + _score_breakdown_float(candidate, "price_bias_score") / 5.0
                )
                / 2.0,
                -1.0,
                1.0,
            ),
            "基于量能与价格偏移联合确认放量突破",
        ),
        "volume_contraction_signal": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                max(0.0, 1.0 - _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0)
                + _score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0,
                0.0,
                1.0,
            ),
            "基于量能收缩与稳定度评估缩量企稳信号",
        ),
        "liquidity_depth_score": _simple_factor_executor(
            lambda candidate, _context: _clamp(_score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0, 0.0, 1.0),
            "基于流动性得分评估成交深度",
        ),
        "opening_volume_impulse": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0 + max(_market_change_pct(candidate), 0.0) * 5.0) / 2.0,
                0.0,
                1.0,
            ),
            "基于开盘强势幅度与量能近似开盘脉冲",
        ),
        "northbound_flow_proxy": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0 + _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0) / 2.0,
                0.0,
                1.0,
            ),
            "基于稳定度与流动性近似北向偏好",
        ),
        "large_order_persistence": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_rank_strength(candidate, 6) + _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0) / 2.0,
                0.0,
                1.0,
            ),
            "基于前排程度与量能近似大单持续性",
        ),
        "main_fund_turning_point": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                max(_market_change_pct(candidate), 0.0) * 5.0 + _score_breakdown_float(candidate, "price_bias_score") / 8.0,
                -1.0,
                1.0,
            ),
            "基于当日转强幅度与价格偏移评估主力拐点",
        ),
        "chip_concentration_proxy": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0 + _selection_quality(candidate)) / 2.0,
                0.0,
                1.0,
            ),
            "基于稳定度与基础分近似筹码集中度",
        ),
        "intraday_capital_reflow": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (max(_market_change_pct(candidate), 0.0) * 4.0 + _score_breakdown_float(candidate, "momentum_pct") / 8.0) / 2.0,
                0.0,
                1.0,
            ),
            "基于日内涨幅修复与动能近似盘中资金回流",
        ),
        "sector_leader_drive": _simple_factor_executor(
            lambda candidate, context: 0.85 if str(candidate.get("resolved_sector") or "") in set(context.get("hot_sectors", [])) else 0.35,
            "基于是否命中热点板块评估龙头带动能力",
        ),
        "sector_breadth_score": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.8 if str(candidate.get("resolved_sector") or "") in set(context.get("hot_sectors", [])) else 0.3)
                + _rank_strength(candidate, 12) * 0.2,
                0.0,
                1.0,
            ),
            "基于热点命中和前排程度近似板块广度",
        ),
        "theme_rotation_speed": _simple_factor_executor(
            lambda candidate, context: 0.75 if str(candidate.get("resolved_sector") or "") in set(context.get("focus_sectors", context.get("hot_sectors", []))) else 0.3,
            "基于焦点板块命中情况评估主题轮动速度",
        ),
        "sector_limit_up_ratio": _simple_factor_executor(
            lambda candidate, _context: _clamp(_score_breakdown_float(candidate, "limit_up_count_20d") / 4.0, 0.0, 1.0),
            "基于近期涨停频次近似板块涨停占比",
        ),
        "peer_follow_strength": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.7 if str(candidate.get("resolved_sector") or "") in set(context.get("hot_sectors", [])) else 0.25)
                + _selection_quality(candidate) * 0.2,
                0.0,
                1.0,
            ),
            "基于板块热度与候选质量评估同伴跟随强度",
        ),
        "earnings_surprise_proxy": _simple_factor_executor(
            lambda candidate, context: 0.8 if any(token in _market_text(context) for token in ("业绩", "预增", "财报", "超预期")) else 0.35,
            "基于市场假设文本近似业绩惊喜催化",
        ),
        "policy_support_score": _simple_factor_executor(
            lambda candidate, context: 0.85 if any(token in _market_text(context) for token in ("政策", "扶持", "会议", "改革")) else 0.3,
            "基于市场假设文本评估政策支持度",
        ),
        "order_backlog_catalyst": _simple_factor_executor(
            lambda candidate, context: 0.8 if any(token in _market_text(context) for token in ("订单", "中标", "景气", "出海")) else 0.32,
            "基于市场假设文本近似订单催化强度",
        ),
        "institutional_attention_score": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.7 if any(token in _market_text(context) for token in ("机构", "调研", "研报")) else 0.3) + _selection_quality(candidate) * 0.2,
                0.0,
                1.0,
            ),
            "基于市场假设与候选质量评估机构关注度",
        ),
        "intraday_trend_stability": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0 + max(0.0, 1.0 - abs(_market_change_pct(candidate)) * 4.0)) / 2.0,
                0.0,
                1.0,
            ),
            "基于稳定度与日内波动克制评估分时趋势稳定度",
        ),
        "order_book_support_proxy": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0 + _score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0) / 2.0,
                0.0,
                1.0,
            ),
            "基于流动性与稳定度近似盘口承接",
        ),
        "vwap_reclaim_strength": _simple_factor_executor(
            lambda candidate, _context: _clamp((max(_market_change_pct(candidate), -0.02) + 0.02) / 0.06, -1.0, 1.0),
            "基于日内涨跌幅修复近似 VWAP 收复强度",
        ),
        "afternoon_bid_strength": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (max(_market_change_pct(candidate), 0.0) * 4.0 + _selection_quality(candidate)) / 2.0,
                0.0,
                1.0,
            ),
            "基于当日回升与基础分评估午后承接强度",
        ),
        "breakout_retest_success": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "price_bias_score") / 5.0 + _score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0) / 2.0,
                -1.0,
                1.0,
            ),
            "基于价格偏移与稳定度评估突破回踩成功率",
        ),
        "drawdown_repair_pressure": _simple_factor_executor(
            lambda candidate, _context: _clamp(abs(min(_score_breakdown_float(candidate, "price_drawdown_20d"), 0.0)) * -2.5, -1.0, 0.0),
            "基于回撤深度增加修复压力惩罚",
        ),
        "volatility_expansion_penalty": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                -abs(_score_breakdown_float(candidate, "volatility_20d", default=0.03) - 0.03) / 0.03,
                -1.0,
                0.0,
            ),
            "基于波动扩张幅度增加惩罚",
        ),
        "gap_down_risk": _simple_factor_executor(
            lambda candidate, _context: _clamp(min(_market_change_pct(candidate), 0.0) * 8.0, -1.0, 0.0),
            "基于低开低走幅度评估跳空低开风险",
        ),
        "crowding_risk_penalty": _simple_factor_executor(
            lambda candidate, _context: _clamp(-_score_breakdown_float(candidate, "limit_up_count_20d") / 6.0, -1.0, 0.0),
            "基于高热度拥挤交易增加风险惩罚",
        ),
        "stop_loss_distance_penalty": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                -(abs(min(_score_breakdown_float(candidate, "price_drawdown_20d"), 0.0)) * 2.0 + max(0.0, _market_change_pct(candidate)) * 0.5),
                -1.0,
                0.0,
            ),
            "基于回撤与追高幅度近似止损距离惩罚",
        ),
        "event_shock_penalty": _simple_factor_executor(
            lambda candidate, context: -0.8 if any(token in _market_text(context) for token in ("问询", "减持", "处罚", "风险", "利空")) else -0.2,
            "基于市场假设中的负面事件文本增加冲击惩罚",
        ),
        "pe_percentile_filter": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                1.0 / (1.0 + max(_score_breakdown_float(candidate, "pb_ratio", default=2.0), 0.2)),
                0.0,
                1.0,
            ),
            "基于估值水平近似 PE 分位过滤",
        ),
        "cashflow_quality_proxy": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0 + _selection_quality(candidate)) / 2.0,
                0.0,
                1.0,
            ),
            "基于稳定度与基础分近似现金流质量",
        ),
        "dividend_support_score": _simple_factor_executor(
            lambda candidate, _context: _clamp(1.0 / (1.0 + max(_score_breakdown_float(candidate, "pb_ratio", default=2.0), 0.5)), 0.0, 1.0),
            "基于偏低估值近似分红支撑度",
        ),
        "balance_sheet_safety": _simple_factor_executor(
            lambda candidate, _context: _clamp(
                (_score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0 + 1.0 / (1.0 + max(_score_breakdown_float(candidate, "pb_ratio", default=2.0), 0.5))) / 2.0,
                0.0,
                1.0,
            ),
            "基于稳定度与估值近似资产负债安全度",
        ),
        "portfolio_fit_score": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.75 if context.get("holding_symbols") else 0.4) + _rank_strength(candidate, 8) * 0.2,
                0.0,
                1.0,
            ),
            "基于是否已有持仓和前排程度评估组合适配度",
        ),
        "correlation_hedge_score": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.8 if context.get("holding_symbols") else 0.45) - (_score_breakdown_float(candidate, "limit_up_count_20d") / 10.0),
                -1.0,
                1.0,
            ),
            "基于持仓上下文与拥挤度近似相关性对冲价值",
        ),
        "replacement_priority_score": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.85 if context.get("holding_symbols") else 0.35) + _rank_strength(candidate, 6) * 0.2,
                0.0,
                1.0,
            ),
            "基于持仓上下文与前排程度评估替换优先级",
        ),
        "t0_recycle_potential": _simple_factor_executor(
            lambda candidate, context: _clamp(
                (0.75 if context.get("holding_symbols") else 0.3) + abs(_market_change_pct(candidate)) * 2.0,
                0.0,
                1.0,
            ),
            "基于持仓上下文与日内波动评估做 T 回转潜力",
        ),
    }
    for item in seeds:
        if factor_registry.get(item.id, item.version) is None:
            factor_registry.register(item, executor=executors.get(item.id))
