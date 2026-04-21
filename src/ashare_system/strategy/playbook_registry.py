"""战法注册表 - 为 runtime compose 提供可发现的战法菜单。"""

from __future__ import annotations

import numpy as np
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..logging_config import get_logger
from .atomic_repository import StrategyRepositoryEntry, strategy_atomic_repository

logger = get_logger("strategy.playbook_registry")


class PlaybookDefinition(BaseModel):
    id: str
    name: str
    version: str = "v1"
    description: str = ""
    market_phases: list[str] = Field(default_factory=list)
    applicable_regimes: list[str] = Field(default_factory=list)
    priority_score: float = 0.5
    probation: bool = False
    params_schema: dict[str, Any] = Field(default_factory=dict)
    evidence_schema: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source: str = "seed"
    author: str = "system"
    status: str = "active"


class PlaybookRegistry:
    def __init__(self) -> None:
        self._playbooks: dict[str, PlaybookDefinition] = {}
        self._executors: dict[str, Callable[[PlaybookDefinition, dict[str, Any], dict[str, Any], Any | None, str | None, dict[str, Any] | None], dict[str, Any]]] = {}

    def register(
        self,
        definition: PlaybookDefinition,
        *,
        executor: Callable[[PlaybookDefinition, dict[str, Any], dict[str, Any], Any | None, str | None, dict[str, Any] | None], dict[str, Any]] | None = None,
    ) -> PlaybookDefinition:
        key = f"{definition.id}:{definition.version}"
        self._playbooks[key] = definition.model_copy()
        if executor is not None:
            self._executors[key] = executor
        if strategy_atomic_repository.get(definition.id, definition.version) is None:
            strategy_atomic_repository.register(
                StrategyRepositoryEntry(
                    id=definition.id,
                    name=definition.name,
                    type="playbook",
                    status=definition.status,  # type: ignore[arg-type]
                    version=definition.version,
                    author=definition.author,
                    source=definition.source,
                    params_schema=definition.params_schema,
                    evidence_schema=definition.evidence_schema,
                    tags=definition.tags,
                    content={
                        "description": definition.description,
                        "market_phases": definition.market_phases,
                    },
                )
            )
        logger.info("战法注册: %s", key)
        return definition.model_copy()

    def get(self, playbook_id: str, version: str = "v1") -> PlaybookDefinition | None:
        item = self._playbooks.get(f"{playbook_id}:{version}")
        return item.model_copy() if item else None

    def list_all(self) -> list[PlaybookDefinition]:
        return [item.model_copy() for item in self._playbooks.values()]

    def suggest_playbooks(
        self,
        *,
        market_hypothesis: str = "",
        market_regime: str = "",
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        text = str(market_hypothesis or "").lower()
        ranked: list[tuple[float, PlaybookDefinition]] = []
        for definition in self.list_all():
            if bool(definition.probation):
                continue
            score = float(definition.priority_score or 0.0)
            if market_regime and market_regime in list(definition.applicable_regimes or []):
                score += 3.0
            if any(tag.lower() in text for tag in list(definition.tags or [])):
                score += 1.5
            if any(token in text for token in ("龙头", "加速", "主升")) and definition.id in {"leader_chase", "trend_acceleration", "trend_breakout_retest"}:
                score += 1.5
            if any(token in text for token in ("防守", "回撤", "低吸")) and definition.id in {"defensive_low_absorb", "oversold_rebound", "position_replacement"}:
                score += 1.5
            if score <= 0:
                continue
            ranked.append((score, definition))
        ranked.sort(key=lambda item: (item[0], item[1].priority_score, item[1].id), reverse=True)
        return [
            {
                **definition.model_dump(),
                "suggestion_score": round(score, 4),
                "suggested_by_regime": market_regime in list(definition.applicable_regimes or []),
            }
            for score, definition in ranked[: max(int(limit or 0), 1)]
        ]

    def apply_priority_updates(self, updates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        for key, definition in list(self._playbooks.items()):
            update = dict(updates.get(definition.id) or {})
            if not update:
                continue
            next_priority = max(0.0, min(float(update.get("priority_score", definition.priority_score) or definition.priority_score), 1.5))
            probation = bool(update.get("probation", False))
            self._playbooks[key] = definition.model_copy(
                update={
                    "priority_score": round(next_priority, 4),
                    "probation": probation,
                }
            )
            applied.append(
                {
                    "playbook_id": definition.id,
                    "priority_score": round(next_priority, 4),
                    "probation": probation,
                    "reason": str(update.get("reason") or ""),
                }
            )
        return applied

    def evaluate(
        self,
        playbook_id: str,
        *,
        version: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        market_adapter: Any | None = None,
        trade_date: str | None = None,
        precomputed_factors: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = f"{playbook_id}:{version}"
        definition = self._playbooks.get(key)
        if definition is None:
            return {"score": 0.0, "evidence": [f"战法未注册: {playbook_id}:{version}"]}
        executor = self._executors.get(key)
        if executor is None:
            return {"score": 0.0, "evidence": [f"战法缺少执行器: {playbook_id}:{version}"]}
        return executor(definition.model_copy(), dict(candidate), dict(context), market_adapter, trade_date, precomputed_factors)


playbook_registry = PlaybookRegistry()


def _selection_score(candidate: dict[str, Any]) -> float:
    return float(candidate.get("selection_score", 0.0) or 0.0)


def _rank(candidate: dict[str, Any]) -> int:
    return int(candidate.get("rank", 99) or 99)


def _score_breakdown(candidate: dict[str, Any]) -> dict[str, Any]:
    return dict(candidate.get("score_breakdown") or {})


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _simple_playbook_executor(score_fn: Callable[[dict[str, Any], dict[str, Any]], float], evidence: str):
    def _executor(
        definition: PlaybookDefinition, 
        candidate: dict[str, Any], 
        context: dict[str, Any],
        market_adapter: Any | None = None,
        trade_date: str | None = None,
        precomputed_factors: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        score = max(min(score_fn(candidate, context), 1.0), 0.0)
        return {
            "score": round(score, 4),
            "evidence": [definition.name, evidence],
        }

    return _executor


def _weak_to_strong_executor(
    definition: PlaybookDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "")
    if market_adapter and symbol:
        try:
            # 获取 1m 分时线做弱转强分析
            bars = market_adapter.get_bars([symbol], period="1m", count=240, end_time=trade_date)
            if len(bars) >= 60:
                closes = [b.close for b in bars]
                # 寻找早盘前 60 分钟的低点
                morning_low = min(closes[:60])
                current_price = closes[-1]
                day_high = max(closes)
                
                # 判定: 当前价接近日高，且显著高于早盘低点
                recovery_pct = (current_price - morning_low) / morning_low if morning_low > 0 else 0.0
                dist_to_high = (day_high - current_price) / current_price if current_price > 0 else 1.0
                
                if recovery_pct > 0.03 and dist_to_high < 0.01:
                    score = _clamp(recovery_pct * 15.0, 0.0, 1.0)
                    return {
                        "score": round(score, 4),
                        "evidence": [definition.name, f"检测到分时弱转强: 振幅修复 {recovery_pct:.2%}, 当前接近日高"],
                    }
        except Exception as e:
            logger.warning("weak_to_strong 真实计算失败: %s", e)

    # 简化逻辑
    score = _clamp(
        (
            (0.35 if _rank(candidate) <= 3 else 0.15)
            + (0.25 if candidate.get("action") == "BUY" else 0.05)
            + (
                max(float(_score_breakdown(candidate).get("momentum_pct", 0.0) or 0.0), 0.0) / 8.0
                if _score_breakdown(candidate).get("momentum_pct") is not None
                else _selection_score(candidate) / 100.0
            )
            + min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)
        )
        / 2.6,
        0.0,
        1.0,
    )
    return {
        "score": round(score, 4),
        "evidence": [definition.name, "基于前排程度与动能联合评估 (简化)"],
    }


def _sector_resonance_executor(
    definition: PlaybookDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sector = str(candidate.get("resolved_sector") or "")
    if market_adapter and sector:
        try:
            # 真实计算：获取板块内成员表现
            symbols = market_adapter.get_sector_symbols(sector)
            if len(symbols) >= 3:
                snaps = market_adapter.get_snapshots(symbols[:20]) # 取前20只看共振
                positives = sum(1 for s in snaps if (s.last_price > s.pre_close if s.pre_close > 0 else False))
                resonance_ratio = positives / len(snaps)
                if resonance_ratio >= 0.6:
                    score = _clamp(resonance_ratio, 0.0, 1.0)
                    return {
                        "score": round(score, 4),
                        "evidence": [definition.name, f"板块 {sector} 正在共振: 成员上涨比例 {resonance_ratio:.0%}"],
                    }
        except Exception as e:
            logger.warning("sector_resonance 真实计算失败: %s", e)

    # 简化逻辑
    score = _clamp(
        (
            (0.45 if sector in set(context.get("hot_sectors", [])) else 0.15)
            + (0.2 if _rank(candidate) <= 3 else 0.08)
            + min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)
        )
        / 1.65,
        0.0,
        1.0,
    )
    return {
        "score": round(score, 4),
        "evidence": [definition.name, "基于热点板块命中与前排程度评估 (简化)"],
    }


def _timeframe_resonance_executor(
    definition: PlaybookDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "")
    if not market_adapter or not symbol:
        return {"score": 0.0, "evidence": [definition.name, "缺少多周期行情数据"]}
    params = dict(context.get("playbook_params") or {})
    raw = list(params.get("timeframes") or ["5d", "20d", "60d"])
    lookbacks: list[int] = []
    for item in raw:
        digits = "".join(ch for ch in str(item) if ch.isdigit())
        if digits:
            lookbacks.append(max(int(digits), 3))
    lookbacks = lookbacks or [5, 20, 60]
    bars = market_adapter.get_bars([symbol], period="1d", count=max(lookbacks) + 2, end_time=trade_date)
    if len(bars) < max(lookbacks) + 1:
        return {"score": 0.0, "evidence": [definition.name, "多周期样本不足"]}
    closes = [bar.close for bar in bars]
    scores: list[float] = []
    evidence = [definition.name]
    for lookback in lookbacks:
        window = closes[-lookback:]
        ret = (window[-1] - window[0]) / max(window[0], 1e-9)
        evidence.append(f"{lookback}日收益={ret:.2%}")
        scores.append(max(min(ret * 4.0, 1.0), -1.0))
    positive_count = sum(1 for item in scores if item > 0)
    score = max(min(sum(scores) / max(len(scores), 1) + positive_count / max(len(scores), 1) * 0.2, 1.0), 0.0)
    return {"score": round(score, 4), "evidence": evidence[:4]}


def _leader_chase_executor(
    definition: PlaybookDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """龙头战法: 追求极强势标的的连板或主升博弈。要求高评分、板块共振且处于主升浪。"""
    selection_score = _selection_score(candidate)
    rank = _rank(candidate)
    sector = str(candidate.get("resolved_sector") or "")
    hot_sectors = set(context.get("hot_sectors") or [])
    
    score = 0.0
    evidence = [definition.name]
    
    # 基础门槛: 必须是前排
    if rank <= 5:
        score += 0.4
        evidence.append(f"排名靠前(#{rank})")
    
    # 板块加成
    if sector in hot_sectors:
        score += 0.3
        evidence.append(f"命中热点板块: {sector}")
        
    # 动能加成
    momentum = float(_score_breakdown(candidate).get("momentum_pct", 0.0) or 0.0)
    if momentum > 5.0:
        score += 0.2
        evidence.append(f"极强动能: {momentum:.1f}%")
        
    # 真实数据核验: 是否接近涨停
    snapshot = dict(candidate.get("market_snapshot") or {})
    last_price = float(snapshot.get("last_price", 0.0) or 0.0)
    pre_close = float(snapshot.get("pre_close", 0.0) or 0.0)
    if last_price > 0 and pre_close > 0:
        change_pct = (last_price - pre_close) / pre_close
        if change_pct > 0.07:
            score += 0.1
            evidence.append(f"分时强势: {change_pct:.2%}")

    return {
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "evidence": evidence,
    }


def _oversold_rebound_executor(
    definition: PlaybookDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """超跌反弹战法: 寻找跌幅过大且出现止跌信号的标的。"""
    symbol = str(candidate.get("symbol") or "")
    score = 0.0
    evidence = [definition.name]
    
    if market_adapter and symbol:
        try:
            # 获取 20 日数据看跌幅
            bars = market_adapter.get_bars([symbol], period="1d", count=20, end_time=trade_date)
            if len(bars) >= 10:
                closes = [b.close for b in bars]
                high_20d = max(closes)
                current = closes[-1]
                drawdown = (current - high_20d) / high_20d
                
                if drawdown < -0.20: # 跌幅超过 20%
                    score += 0.6
                    evidence.append(f"超跌严重: 20日回撤 {drawdown:.2%}")
                    
                    # 检查最近 2 日是否止跌
                    if closes[-1] > closes[-2]:
                        score += 0.3
                        evidence.append("出现止跌回升信号")
        except Exception:
            pass
            
    if score == 0:
        # 简化逻辑: 基于 stability_score (负相关)
        stability = float(_score_breakdown(candidate).get("stability_score", 0.0) or 5.0)
        if stability < 3.0:
            score = 0.4
            evidence.append("较低稳定性暗示潜在反弹机会(简化判断)")
            
    return {
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "evidence": evidence,
    }


def _dragon_returns_executor(
    definition: PlaybookDefinition,
    candidate: dict[str, Any],
    context: dict[str, Any],
    market_adapter: Any | None = None,
    trade_date: str | None = None,
    precomputed_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """龙回头战法: 前期强势股回调后二次启动。要求高涨停人气 + 缩量回调支撑位。"""
    symbol = str(candidate.get("symbol") or "")
    limit_ups = int(_score_breakdown(candidate).get("limit_up_count_20d", 0) or 0)
    drawdown = float(_score_breakdown(candidate).get("price_drawdown_20d", 0.0) or 0.0)
    
    score = 0.0
    evidence = [definition.name]
    
    if limit_ups >= 3:
        score += 0.5
        evidence.append(f"前期强人气: 20日内 {limit_ups} 次涨停")
        
        # 检查回调深度 (龙回头黄金区间: 15%-30%)
        if -0.30 <= drawdown <= -0.15:
            score += 0.4
            evidence.append(f"处于黄金回调区间: {drawdown:.2%}")
            
            # 真实数据核验: 是否缩量
            if market_adapter and symbol:
                try:
                    bars = market_adapter.get_bars([symbol], period="1d", count=5, end_time=trade_date)
                    if len(bars) >= 3:
                        vols = [b.volume for b in bars]
                        if vols[-1] < vols[-2] < vols[-3]: # 连续缩量
                            score += 0.1
                            evidence.append("缩量回调迹象明显")
                except Exception:
                    pass
    
    return {
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "evidence": evidence,
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


def _market_text(context: dict[str, Any]) -> str:
    return " ".join(
        [
            str(context.get("market_hypothesis") or ""),
            str(context.get("trade_horizon") or ""),
            " ".join(str(item) for item in list(context.get("hot_sectors") or [])),
        ]
    ).lower()


def _playbook_params_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "holding_period": {"type": "string"},
            "max_positions": {"type": "integer", "minimum": 1},
            "risk_budget": {"type": "number", "minimum": 0.0},
        },
        "additionalProperties": True,
    }


def _playbook_evidence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "score": {"type": "number"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "market_phases": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["score", "evidence"],
        "additionalProperties": True,
    }


def _playbook_seed(
    *,
    playbook_id: str,
    name: str,
    description: str,
    market_phases: list[str],
    applicable_regimes: list[str],
    tags: list[str],
) -> PlaybookDefinition:
    return PlaybookDefinition(
        id=playbook_id,
        name=name,
        version="v1",
        description=description,
        market_phases=market_phases,
        applicable_regimes=applicable_regimes,
        params_schema=_playbook_params_schema(),
        evidence_schema=_playbook_evidence_schema(),
        tags=list(dict.fromkeys(tags)),
        source="seed.playbook_library.20260417",
        author="system.playbook_library",
        status="active",
    )


def bootstrap_playbook_registry() -> None:
    seeds = [
        _playbook_seed(playbook_id="sector_resonance", name="板块共振", description="围绕热点板块扩散与龙头映射", market_phases=["回暖", "主升"], applicable_regimes=["strong_rotation", "sector_breakout"], tags=["sector", "resonance"]),
        _playbook_seed(playbook_id="trend_acceleration", name="趋势加速", description="围绕趋势延续与突破后的加速", market_phases=["主升"], applicable_regimes=["sector_breakout", "index_rebound"], tags=["trend", "acceleration"]),
        _playbook_seed(playbook_id="timeframe_resonance", name="周期共振", description="多周期同向共振时放大趋势把握", market_phases=["回暖", "主升"], applicable_regimes=["sector_breakout", "index_rebound"], tags=["timeframe", "resonance"]),
        _playbook_seed(playbook_id="weak_to_strong_intraday", name="盘中弱转强", description="围绕分时转强与量能修复", market_phases=["回暖", "主升"], applicable_regimes=["strong_rotation", "sector_breakout"], tags=["intraday", "rotation"]),
        _playbook_seed(playbook_id="tail_close_ambush", name="尾盘潜伏", description="围绕尾盘承接与隔夜博弈", market_phases=["回暖", "震荡"], applicable_regimes=["weak_defense", "panic_sell"], tags=["tail", "overnight"]),
        _playbook_seed(playbook_id="position_replacement", name="持仓替换", description="围绕更优机会替换持仓", market_phases=["回暖", "主升", "震荡"], applicable_regimes=["weak_defense", "panic_sell", "strong_rotation"], tags=["position", "replacement"]),
        _playbook_seed(playbook_id="leader_chase", name="龙头博弈", description="追求极强势标的的连板或主升博弈", market_phases=["主升"], applicable_regimes=["sector_breakout", "strong_rotation"], tags=["leader", "high_momentum"]),
        _playbook_seed(playbook_id="oversold_rebound", name="超跌反抽", description="博弈严重偏离均值后的修复性反弹", market_phases=["普跌", "震荡"], applicable_regimes=["panic_sell", "weak_defense", "index_rebound"], tags=["reversion", "oversold"]),
        _playbook_seed(playbook_id="index_enhancement", name="指数增强", description="在跟踪基准指数的基础上寻求超额收益", market_phases=["回暖", "主升"], applicable_regimes=["index_rebound"], tags=["index", "alpha"]),
        _playbook_seed(playbook_id="statistical_arbitrage", name="统计套利", description="基于历史相关性博弈配对或组合的均值回归", market_phases=["震荡"], applicable_regimes=["weak_defense"], tags=["arbitrage", "reversion"]),
        _playbook_seed(playbook_id="dragon_returns", name="龙回头", description="前期强势妖股回调至关键位后的反抽博弈", market_phases=["震荡", "回暖"], applicable_regimes=["strong_rotation", "index_rebound"], tags=["short_term", "reversion"]),
        _playbook_seed(playbook_id="trend_breakout_retest", name="突破回踩再上", description="围绕突破后的回踩确认与二次加速", market_phases=["回暖", "主升"], applicable_regimes=["sector_breakout", "index_rebound"], tags=["trend", "breakout"]),
        _playbook_seed(playbook_id="sector_rotation_relay", name="板块轮动接力", description="围绕热点板块内部的龙头切换与接力扩散", market_phases=["回暖", "主升"], applicable_regimes=["strong_rotation"], tags=["sector", "rotation"]),
        _playbook_seed(playbook_id="news_catalyst_follow", name="事件催化跟随", description="围绕政策、公告、业绩等事件催化的跟随交易", market_phases=["回暖", "主升", "震荡"], applicable_regimes=["sector_breakout", "strong_rotation", "index_rebound"], tags=["event", "follow"]),
        _playbook_seed(playbook_id="opening_auction_pilot", name="竞价先手", description="围绕集合竞价强弱和封单质量做盘前先手", market_phases=["竞价", "回暖"], applicable_regimes=["strong_rotation", "sector_breakout"], tags=["auction", "opening"]),
        _playbook_seed(playbook_id="midday_rebound_t", name="午间回转做T", description="围绕已有持仓在午间和午后做回转与降本", market_phases=["震荡", "回暖"], applicable_regimes=["index_rebound", "weak_defense"], tags=["midday", "t0"]),
        _playbook_seed(playbook_id="late_session_reclaim", name="尾盘收复", description="围绕尾盘承接转强和次日预期收口", market_phases=["回暖", "震荡"], applicable_regimes=["index_rebound", "weak_defense"], tags=["tail", "reclaim"]),
        _playbook_seed(playbook_id="defensive_low_absorb", name="防守型低吸", description="围绕低波低估和承接韧性做防守型低吸", market_phases=["普跌", "震荡"], applicable_regimes=["weak_defense", "panic_sell"], tags=["defensive", "low_absorb"]),
        _playbook_seed(playbook_id="position_trim_redeploy", name="减弱换强", description="围绕持仓修剪和换仓再部署做组合优化", market_phases=["主升", "震荡", "尾盘"], applicable_regimes=["strong_rotation", "weak_defense"], tags=["position", "redeploy"]),
        _playbook_seed(playbook_id="event_driven_reversal", name="事件反转", description="围绕利空落地、澄清公告后的情绪反转博弈", market_phases=["震荡", "普跌", "回暖"], applicable_regimes=["panic_sell", "index_rebound"], tags=["event", "reversal"]),
        _playbook_seed(playbook_id="swing_mean_repair", name="波段均值修复", description="围绕趋势票的中继回踩和波段均值修复", market_phases=["回暖", "震荡"], applicable_regimes=["index_rebound", "weak_defense"], tags=["swing", "repair"]),
    ]
    executors = {
        "sector_resonance": _sector_resonance_executor,
        "timeframe_resonance": _timeframe_resonance_executor,
        "trend_acceleration": _simple_playbook_executor(
            lambda candidate, _context: _clamp(
                (
                    (
                        max(float(_score_breakdown(candidate).get("momentum_pct", 0.0) or 0.0), 0.0) / 8.0
                        if _score_breakdown(candidate).get("momentum_pct") is not None
                        else _selection_score(candidate) / 100.0
                    )
                    + min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)
                    + (0.15 if candidate.get("action") == "BUY" else 0.0)
                )
                / 2.15,
                0.0,
                1.0,
            ),
            "基于动能、量能与动作信号联合评估趋势加速适配度",
        ),
        "weak_to_strong_intraday": _weak_to_strong_executor,
        "tail_close_ambush": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.4 if context.get("trade_horizon") == "intraday_to_overnight" else 0.18)
                    + (0.2 if candidate.get("action") == "BUY" else 0.08)
                    + min(float(_score_breakdown(candidate).get("liquidity_score", 0.0) or 0.0) / 14.0, 1.0)
                )
                / 1.6,
                0.0,
                1.0,
            ),
            "基于交易周期、动作信号与量能联合评估尾盘潜伏适配度",
        ),
        "position_replacement": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.45 if context.get("holding_symbols") else 0.15)
                    + (0.25 if _rank(candidate) <= 3 else 0.1)
                    + min(_selection_score(candidate) / 100.0, 1.0)
                )
                / 1.7,
                0.0,
                1.0,
            ),
            "基于是否有持仓、候选前排程度与基础分联合评估替换价值",
        ),
        "leader_chase": _leader_chase_executor,
        "oversold_rebound": _oversold_rebound_executor,
        "index_enhancement": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (0.4 if _rank(candidate) <= 10 else 0.15)
                + min(float(_score_breakdown(candidate).get("stability_score", 0.0) or 0.0) / 14.0, 1.0) * 0.4
                + (0.2 if str(candidate.get("symbol")).startswith("60") else 0.1), # 偏向沪市蓝筹示例
                0.0,
                1.0,
            ),
            "基于排名、稳定度与蓝筹属性评估指数增强适配度",
        ),
        "statistical_arbitrage": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (0.5 if abs(float(_score_breakdown(candidate).get("price_bias_score", 0.0) or 0.0)) > 2.0 else 0.1)
                + min(float(_score_breakdown(candidate).get("stability_score", 0.0) or 0.0) / 14.0, 1.0) * 0.3,
                0.0,
                1.0,
            ),
            "基于价格偏移与波动稳定性评估统计套利/均值回归价值",
        ),
        "dragon_returns": _dragon_returns_executor,
        "trend_breakout_retest": _simple_playbook_executor(
            lambda candidate, _context: _clamp(
                (
                    _score_breakdown_float(candidate, "price_bias_score") / 5.0
                    + _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 14.0
                    + _selection_quality(candidate)
                )
                / 2.4,
                0.0,
                1.0,
            ),
            "基于突破质量、量能与基础分评估回踩再上适配度",
        ),
        "sector_rotation_relay": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.55 if str(candidate.get("resolved_sector") or "") in set(context.get("hot_sectors", [])) else 0.18)
                    + (0.2 if _rank(candidate) <= 5 else 0.08)
                    + _selection_quality(candidate) * 0.3
                ),
                0.0,
                1.0,
            ),
            "基于热点命中、前排程度与基础分评估板块轮动接力",
        ),
        "news_catalyst_follow": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.55 if any(token in _market_text(context) for token in ("事件", "公告", "政策", "业绩", "催化")) else 0.2)
                    + max(_market_change_pct(candidate), 0.0) * 4.0
                    + _selection_quality(candidate) * 0.2
                ),
                0.0,
                1.0,
            ),
            "基于事件文本、当日强势与基础分评估催化跟随适配度",
        ),
        "opening_auction_pilot": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.45 if "auction" in _market_text(context) or "竞价" in _market_text(context) else 0.18)
                    + max(_market_change_pct(candidate), 0.0) * 5.0
                    + _score_breakdown_float(candidate, "liquidity_score", default=7.0) / 20.0
                ),
                0.0,
                1.0,
            ),
            "基于竞价语境、开盘强势与量能评估竞价先手价值",
        ),
        "midday_rebound_t": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.5 if context.get("holding_symbols") else 0.15)
                    + (0.2 if str(context.get("trade_horizon") or "") in {"intraday", "intraday_to_overnight"} else 0.05)
                    + abs(_market_change_pct(candidate)) * 2.5
                ),
                0.0,
                1.0,
            ),
            "基于持仓上下文、交易周期与波动评估午间回转做T",
        ),
        "late_session_reclaim": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.45 if str(context.get("trade_horizon") or "") == "intraday_to_overnight" else 0.18)
                    + max(_market_change_pct(candidate), 0.0) * 4.0
                    + _selection_quality(candidate) * 0.2
                ),
                0.0,
                1.0,
            ),
            "基于隔夜周期、尾盘修复与基础分评估尾盘收复打法",
        ),
        "defensive_low_absorb": _simple_playbook_executor(
            lambda candidate, _context: _clamp(
                (
                    1.0 / (1.0 + max(_score_breakdown_float(candidate, "pb_ratio", default=2.0), 0.5))
                    + _score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0
                    + max(0.0, -_score_breakdown_float(candidate, "price_drawdown_20d")) * 1.5
                )
                / 2.4,
                0.0,
                1.0,
            ),
            "基于低估值、稳定度和回撤幅度评估防守型低吸",
        ),
        "position_trim_redeploy": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.55 if context.get("holding_symbols") else 0.15)
                    + (0.2 if _rank(candidate) <= 5 else 0.05)
                    + _selection_quality(candidate) * 0.25
                ),
                0.0,
                1.0,
            ),
            "基于持仓上下文、前排程度与基础分评估减弱换强",
        ),
        "event_driven_reversal": _simple_playbook_executor(
            lambda candidate, context: _clamp(
                (
                    (0.5 if any(token in _market_text(context) for token in ("澄清", "落地", "反转", "修复")) else 0.18)
                    + max(0.0, -_score_breakdown_float(candidate, "price_drawdown_20d")) * 1.5
                    + max(_market_change_pct(candidate), 0.0) * 3.0
                ),
                0.0,
                1.0,
            ),
            "基于事件修复文本、回撤深度与拉回幅度评估事件反转",
        ),
        "swing_mean_repair": _simple_playbook_executor(
            lambda candidate, _context: _clamp(
                (
                    max(0.0, -_score_breakdown_float(candidate, "price_drawdown_20d")) * 1.8
                    + _score_breakdown_float(candidate, "stability_score", default=6.0) / 14.0
                    + _selection_quality(candidate) * 0.25
                ),
                0.0,
                1.0,
            ),
            "基于回撤、稳定度与基础分评估波段均值修复",
        ),
    }
    for item in seeds:
        if playbook_registry.get(item.id, item.version) is None:
            playbook_registry.register(item, executor=executors.get(item.id))
