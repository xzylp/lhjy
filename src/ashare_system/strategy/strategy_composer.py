"""Strategy Composer - 将 Agent 的 compose 请求编排成可消费候选与解释。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING
from uuid import uuid4
import pandas as pd

from ..runtime_compose_contracts import RuntimeComposeRequest
from ..runtime_config import RuntimeConfig
from .factor_registry import FactorRegistry
from .playbook_registry import PlaybookRegistry
from ..factors.engine import FactorEngine


if TYPE_CHECKING:
    from .evaluation_ledger import EvaluationLedgerService


class StrategyComposer:
    def __init__(
        self, 
        factor_registry: FactorRegistry, 
        playbook_registry: PlaybookRegistry,
        evaluation_ledger: EvaluationLedgerService | None = None,
    ) -> None:
        self._factor_registry = factor_registry
        self._playbook_registry = playbook_registry
        self._evaluation_ledger = evaluation_ledger

    def build_output(
        self,
        request: RuntimeComposeRequest,
        pipeline_payload: dict[str, Any],
        runtime_config: RuntimeConfig,
        constraint_trace: dict[str, Any] | None = None,
        learned_asset_entries: list[dict[str, Any]] | None = None,
        event_context: dict[str, Any] | None = None,
        monitor_context: dict[str, Any] | None = None,
        market_adapter: Any | None = None,
    ) -> dict[str, Any]:
        top_picks = list(pipeline_payload.get("top_picks", []))
        filtered_out = self._build_filtered_out(pipeline_payload)
        learned_asset_plan = self._build_learned_asset_plan(request, learned_asset_entries or [])
        trade_date = str(pipeline_payload.get("trade_date") or "")
        
        # 提取市场驱动因素 (R0.4)
        market_drivers = self._build_market_drivers(
            pipeline_payload, 
            event_context=event_context, 
            monitor_context=monitor_context
        )
        
        candidates = self._build_candidates(
            request, 
            top_picks, 
            runtime_config, 
            learned_asset_plan=learned_asset_plan,
            market_drivers=market_drivers,
            market_adapter=market_adapter,
            trade_date=trade_date,
        )
        explanations = self._build_explanations(
            request,
            runtime_config,
            constraint_trace=constraint_trace,
            learned_asset_plan=learned_asset_plan,
            market_drivers=market_drivers,
        )
        return {
            "market_summary": self._build_market_summary(pipeline_payload, market_drivers=market_drivers),
            "candidates": candidates,
            "filtered_out": filtered_out if request.output.include_filtered_reasons else [],
            "explanations": explanations,
            "proposal_packet": {
                "selected_symbols": [item.get("symbol", "") for item in candidates[:3]],
                "watchlist_symbols": [item.get("symbol", "") for item in candidates[3 : request.output.max_candidates]],
                "discussion_focus": [
                    "这批候选是否贴合当前市场假设",
                    "是否优于现有持仓",
                    "是否需要风控进一步挑反证",
                    "active learned asset 是否真的改善了当前排序与证据质量",
                    "市场驱动因素(热点/事件)是否已充分反映在排序中",
                ],
            },
        }

    def _build_market_drivers(
        self, 
        pipeline_payload: dict[str, Any], 
        event_context: dict[str, Any] | None = None,
        monitor_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """构建市场驱动偏置信息。"""
        market_profile = pipeline_payload.get("market_profile") or {}
        hot_sectors = set(market_profile.get("hot_sectors") or [])
        
        # 事件加成：提取正面新闻或公告
        event_bonus_map: dict[str, float] = {}
        event_reason_map: dict[str, str] = {}
        if event_context:
            highlights = list(event_context.get("highlights") or [])
            for event in highlights:
                symbol = event.get("symbol")
                sentiment = str(event.get("sentiment") or "").lower()
                if symbol and sentiment == "positive":
                    event_bonus_map[symbol] = 1.5  # 事件加成系数
                    event_reason_map[symbol] = f"正面催化: {event.get('title')}"

        # 动能/异动加成：提取价格异动
        monitor_bonus_map: dict[str, float] = {}
        if monitor_context:
            for alert in list(monitor_context.get("recent_events") or []):
                symbol = alert.get("symbol")
                if symbol and alert.get("severity") in ("info", "warning") and float(alert.get("change_pct", 0) or 0) > 0.02:
                    monitor_bonus_map[symbol] = 0.8
        
        return {
            "hot_sectors": hot_sectors,
            "event_bonus_map": event_bonus_map,
            "event_reason_map": event_reason_map,
            "monitor_bonus_map": monitor_bonus_map,
        }

    def _build_market_summary(self, pipeline_payload: dict[str, Any], market_drivers: dict[str, Any] | None = None) -> dict[str, Any]:
        market_profile = pipeline_payload.get("market_profile") or {}
        drivers = market_drivers or {}
        return {
            "market_regime": market_profile.get("regime", ""),
            "hot_sectors": list(drivers.get("hot_sectors") or market_profile.get("hot_sectors") or []),
            "risk_flags": market_profile.get("market_risk_flags", []),
            "event_drive_active": bool(drivers.get("event_bonus_map")),
        }

    def _build_filtered_out(self, pipeline_payload: dict[str, Any]) -> list[dict[str, Any]]:
        filtered_out = []
        for item in (pipeline_payload.get("selection_preferences", {}) or {}).get("excluded_candidates", []):
            filtered_out.append(
                {
                    "symbol": item.get("symbol", ""),
                    "name": item.get("name", ""),
                    "stage": item.get("stage", "user_preferences"),
                    "reason": item.get("reason")
                    or (
                        f"命中 excluded_theme_keywords={item.get('matched_keyword', '')}"
                        if item.get("matched_keyword")
                        else "命中组合约束"
                    ),
                }
            )
        return filtered_out

    def _build_explanations(
        self,
        request: RuntimeComposeRequest,
        runtime_config: RuntimeConfig,
        *,
        constraint_trace: dict[str, Any] | None = None,
        learned_asset_plan: dict[str, Any] | None = None,
        market_drivers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_preferences = request.constraints.user_preferences or {}
        factor_specs = list(request.strategy.factors)
        playbook_specs = list(request.strategy.playbooks)
        summary_lines = list((constraint_trace or {}).get("summary_lines") or [])
        learned_summary = dict((learned_asset_plan or {}).get("summary") or {})
        drivers = market_drivers or {}
        
        driver_summary = []
        if drivers.get("hot_sectors"):
            driver_summary.append(f"正在追踪热点板块: {','.join(list(drivers['hot_sectors'])[:3])}")
        if drivers.get("event_bonus_map"):
            driver_summary.append(f"检测到 {len(drivers['event_bonus_map'])} 个正面事件催化标的")
            
        return {
            "strategy_summary": request.intent.market_hypothesis or request.intent.objective or "Agent 发起参数化扫描",
            "weight_summary": [
                f"{item.id} 权重 {round(item.weight, 4)}"
                for item in sorted(factor_specs, key=lambda entry: abs(entry.weight), reverse=True)[:5]
            ]
            + [
                f"{item.id} 战法权重 {round(item.weight, 4)}"
                for item in sorted(playbook_specs, key=lambda entry: abs(entry.weight), reverse=True)[:3]
            ],
            "constraint_summary": [
                f"排除方向: {','.join(user_preferences.get('excluded_theme_keywords', []))}"
                if user_preferences.get("excluded_theme_keywords")
                else "排除方向: 无",
                f"单票金额上限: {runtime_config.max_single_amount}",
                f"股票仓位上限: {runtime_config.equity_position_limit}",
            ]
            + summary_lines,
            "market_driver_summary": driver_summary,
            "learned_asset_summary": learned_summary,
        }

    def _build_candidates(
        self,
        request: RuntimeComposeRequest,
        top_picks: list[dict[str, Any]],
        runtime_config: RuntimeConfig,
        *,
        learned_asset_plan: dict[str, Any] | None = None,
        market_drivers: dict[str, Any] | None = None,
        market_adapter: Any | None = None,
        trade_date: str | None = None,
    ) -> list[dict[str, Any]]:
        learned_asset_plan = learned_asset_plan or {}
        drivers = market_drivers or {}
        hot_sectors = drivers.get("hot_sectors") or set()
        event_bonus_map = drivers.get("event_bonus_map") or {}
        event_reason_map = drivers.get("event_reason_map") or {}
        monitor_bonus_map = drivers.get("monitor_bonus_map") or {}
        
        # R0.9: 批量预计算因子
        all_precomputed = self._precompute_factors(top_picks, market_adapter, trade_date)
        
        playbook_weight_bias = dict(learned_asset_plan.get("playbook_weight_bias") or {})
        factor_weight_bias = dict(learned_asset_plan.get("factor_weight_bias") or {})
        symbol_score_bias = dict(learned_asset_plan.get("symbol_score_bias") or {})
        sector_score_bias = dict(learned_asset_plan.get("sector_score_bias") or {})
        score_bonus = float(learned_asset_plan.get("score_bonus", 0.0) or 0.0)
        active_asset_ids = list(learned_asset_plan.get("active_asset_ids") or [])
        
        effective_playbook_weights = {
            item.id: max(item.weight + float(playbook_weight_bias.get(item.id, 0.0) or 0.0), 0.0)
            for item in request.strategy.playbooks
        }
        effective_factor_weights = {
            item.id: item.weight + float(factor_weight_bias.get(item.id, 0.0) or 0.0)
            for item in request.strategy.factors
        }
        total_playbook_weight = sum(effective_playbook_weights.values()) or 1.0
        factor_total_weight = sum(abs(value) for value in effective_factor_weights.values()) or 1.0
        
        context = {
            "market_hypothesis": request.intent.market_hypothesis,
            "trade_horizon": request.intent.trade_horizon,
            "holding_symbols": request.market_context.get("holding_symbols", []),
            "hot_sectors": list(hot_sectors),
        }
        
        candidates: list[dict[str, Any]] = []
        for item in top_picks:
            symbol = str(item.get("symbol", "") or "")
            resolved_sector = str(item.get("resolved_sector", "") or "")
            selection_score = float(item.get("selection_score", 0.0) or 0.0)
            precomputed = all_precomputed.get(symbol, {})
            
            # 市场驱动加成 (R0.4)
            driver_bonus = 0.0
            driver_tags = []
            if resolved_sector in hot_sectors:
                driver_bonus += 2.0
                driver_tags.append("hot_sector_hit")
            if symbol in event_bonus_map:
                driver_bonus += event_bonus_map[symbol]
                driver_tags.append("event_catalyst")
            if symbol in monitor_bonus_map:
                driver_bonus += monitor_bonus_map[symbol]
                driver_tags.append("momentum_boost")

            playbook_fit = {}
            playbook_evidence: list[str] = []
            for playbook in request.strategy.playbooks:
                evaluation = self._playbook_registry.evaluate(
                    playbook.id,
                    version=playbook.version,
                    candidate=item,
                    context=context,
                    market_adapter=market_adapter,
                    trade_date=trade_date,
                    precomputed_factors=precomputed,
                )
                base_score = float(evaluation.get("score", 0.0) or 0.0)
                effective_weight = effective_playbook_weights.get(playbook.id, 0.0)
                fit_score = (effective_weight / total_playbook_weight) * base_score
                playbook_fit[playbook.id] = round(min(fit_score, 1.0), 4)
                playbook_evidence.extend(evaluation.get("evidence", [])[:1])

            factor_scores = {}
            composite_adjustment = 0.0
            factor_evidence: list[str] = []
            for factor in request.strategy.factors:
                evaluation = self._factor_registry.evaluate(
                    factor.id,
                    version=factor.version,
                    candidate=item,
                    context=context,
                    market_adapter=market_adapter,
                    trade_date=trade_date,
                    precomputed_factors=precomputed,
                )
                effective_weight = effective_factor_weights.get(factor.id, factor.weight)
                factor_score = float(evaluation.get("score", 0.0) or 0.0) * effective_weight
                factor_scores[factor.id] = round(factor_score, 4)
                composite_adjustment += factor_score / factor_total_weight
                factor_evidence.extend(evaluation.get("evidence", [])[:1])

            learned_bonus = float(symbol_score_bias.get(symbol, 0.0) or 0.0) + score_bonus
            if resolved_sector:
                learned_bonus += float(sector_score_bias.get(resolved_sector, 0.0) or 0.0)
            
            # 最终复合评分计入驱动加成
            composite_score = round(selection_score + composite_adjustment * 12.0 + learned_bonus + driver_bonus, 2)
            
            evidence = [item.get("summary", "")] if item.get("summary") else []
            if resolved_sector:
                evidence.append(f"所属方向: {resolved_sector}")
            if symbol in event_reason_map:
                evidence.append(event_reason_map[symbol])
            if "hot_sector_hit" in driver_tags:
                evidence.append(f"命中热点板块: {resolved_sector}")
                
            evidence.extend(playbook_evidence[:2])
            evidence.extend(factor_evidence[:2])
            if active_asset_ids:
                evidence.append(f"学习产物加权: {','.join(active_asset_ids[:2])}")
            
            counter_evidence = []
            if request.intent.market_hypothesis:
                counter_evidence.append("仍需结合实时市场变化复核原假设是否继续成立")
            
            candidate = {
                "symbol": symbol,
                "name": item.get("name", ""),
                "rank": item.get("rank", 0),
                "action_hint": "BUY_CANDIDATE" if (item.get("action") == "BUY" or driver_bonus > 1.0) else "WATCH_CANDIDATE",
                "composite_score": composite_score,
                "playbook_fit": playbook_fit,
                "factor_scores": factor_scores,
                "market_drivers": {
                    "bonus": driver_bonus,
                    "tags": driver_tags,
                    "details": event_reason_map.get(symbol, "")
                },
                "evidence": evidence,
                "counter_evidence": counter_evidence,
                "risk_flags": [f"动作={item.get('action', '')}"],
                "positioning_hint": {
                    "max_suggested_amount": runtime_config.max_single_amount,
                    "suggested_role": "frontline_candidate" if item.get("rank", 99) <= 3 else "watch_candidate",
                },
                "learned_asset_adjustments": {
                    "active_assets": active_asset_ids,
                    "factor_weight_bias": {
                        key: round(float(value or 0.0), 4)
                        for key, value in factor_weight_bias.items()
                        if key in factor_scores
                    },
                    "playbook_weight_bias": {
                        key: round(float(value or 0.0), 4)
                        for key, value in playbook_weight_bias.items()
                        if key in playbook_fit
                    },
                    "symbol_score_bonus": round(float(symbol_score_bias.get(symbol, 0.0) or 0.0), 4),
                    "sector_score_bonus": round(float(sector_score_bias.get(resolved_sector, 0.0) or 0.0), 4),
                    "global_score_bonus": round(score_bonus, 4),
                },
                "raw_runtime_decision": item,
            }
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                float(item.get("composite_score", 0.0) or 0.0),
                *[float((item.get("factor_scores") or {}).get(key, 0.0) or 0.0) for key in request.strategy.ranking.secondary_keys],
            ),
            reverse=True,
        )
        for index, candidate in enumerate(candidates, start=1):
            candidate["rank"] = index
        return candidates

    def _precompute_factors(
        self, 
        top_picks: list[dict[str, Any]], 
        market_adapter: Any | None, 
        trade_date: str | None
    ) -> dict[str, dict[str, Any]]:
        """批量预计算所有候选股的因子值 (R0.9)。"""
        if not market_adapter:
            return {}
            
        symbols = [str(item.get("symbol")) for item in top_picks if item.get("symbol")]
        if not symbols:
            return {}
            
        try:
            # 批量获取日线数据 (取 60 日以覆盖 return_20d, rel_vol 等)
            all_bars = market_adapter.get_bars(symbols, period="1d", count=60, end_time=trade_date)
            if not all_bars:
                return {}
                
            # 分组并构建结果
            precomputed_results: dict[str, dict[str, Any]] = {s: {} for s in symbols}
            
            for s in symbols:
                s_bars = [b for b in all_bars if b.symbol == s]
                if len(s_bars) >= 5:
                    closes = [b.close for b in s_bars]
                    volumes = [b.volume for b in s_bars]
                    
                    # 1. return_20d
                    if len(closes) >= 20:
                        ret_20d = (closes[-1] - closes[0]) / closes[0]
                        precomputed_results[s]["return_20d"] = ret_20d
                        
                        # 2. price_drawdown_20d
                        high_20d = max(closes)
                        precomputed_results[s]["price_drawdown_20d"] = (closes[-1] - high_20d) / high_20d if high_20d > 0 else 0.0
                    
                    # 3. relative_volume (当前成交量 / 5日均量)
                    avg_vol_5d = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else (sum(volumes) / len(volumes))
                    precomputed_results[s]["relative_volume"] = volumes[-1] / avg_vol_5d if avg_vol_5d > 0 else 1.0
                    
                    # 4. volatility_20d (20日收益率标准差)
                    if len(closes) >= 21:
                        rets = pd.Series(closes).pct_change().dropna()
                        precomputed_results[s]["volatility_20d"] = rets.tail(20).std()
                    
                    # 5. rsi_14 (14日相对强弱指标)
                    if len(closes) >= 15:
                        delta = pd.Series(closes).diff().dropna()
                        gain = delta.where(delta > 0, 0)
                        loss = -delta.where(delta < 0, 0)
                        avg_gain = gain.rolling(window=14).mean()
                        avg_loss = loss.rolling(window=14).mean()
                        rs = avg_gain / avg_loss.replace(0, 1e-9)
                        precomputed_results[s]["rsi_14"] = 100 - (100 / (1 + rs.iloc[-1]))
                    
                    # 6. limit_up_count_20d (20日内涨停次数)
                    if len(s_bars) >= 21:
                        # 简单判定: 涨幅 > 9.5% 视为涨停
                        rets = pd.Series(closes).pct_change().dropna()
                        precomputed_results[s]["limit_up_count_20d"] = int((rets.tail(20) > 0.095).sum())
                        
                    # 7. vol_std_20d (成交量稳定性)
                    if len(s_bars) >= 21:
                        vols = pd.Series(volumes).tail(20)
                        precomputed_results[s]["vol_std_20d"] = vols.std() / (vols.mean() + 1e-9)
                
            return precomputed_results
        except Exception:
            return {}

    def _build_learned_asset_plan(
        self,
        request: RuntimeComposeRequest,
        learned_asset_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        requested_asset_ids = {item.id for item in request.learned_assets}
        active_items = [
            item
            for item in learned_asset_entries
            if str(item.get("status", "")).strip() == "active"
        ]
        factor_ids = {item.id for item in request.strategy.factors}
        playbook_ids = {item.id for item in request.strategy.playbooks}
        factor_weight_bias: dict[str, float] = {}
        playbook_weight_bias: dict[str, float] = {}
        symbol_score_bias: dict[str, float] = {}
        sector_score_bias: dict[str, float] = {}
        score_bonus = 0.0
        summary_lines: list[str] = []
        explicit_active_ids: list[str] = []
        auto_active_ids: list[str] = []
        # R0.10: 性能驱动的权重衰减
        records = self._evaluation_ledger.list_records(limit=100) if self._evaluation_ledger else []
        
        for item in active_items:
            entry_asset_id = str(item.get("id", "")).strip()
            
            # 计算历史性能系数 (R0.10)
            performance_multiplier = 1.0
            if records:
                relevant_outcomes = [
                    rec.get("outcome", {}) for rec in records 
                    if entry_asset_id in set(rec.get("active_learned_asset_ids", []))
                ]
                if relevant_outcomes:
                    # 简单逻辑：如果最近 5 次平均胜率 < 40%，衰减
                    recent = relevant_outcomes[:5]
                    win_rate = sum(1 for o in recent if o.get("status") == "settled") / len(recent)
                    if win_rate < 0.4:
                        performance_multiplier = 0.5
                        summary_lines.append(f"{entry_asset_id} 历史表现欠佳(胜率{win_rate:.0%})，权重已衰减 50%")

            content = dict(item.get("content") or {})
            generic_weights = dict(content.get("weights") or {})
            for weighted_asset_id, value in generic_weights.items():
                numeric = float(value or 0.0) * performance_multiplier
                if weighted_asset_id in factor_ids:
                    factor_weight_bias[weighted_asset_id] = factor_weight_bias.get(weighted_asset_id, 0.0) + numeric
                if weighted_asset_id in playbook_ids:
                    playbook_weight_bias[weighted_asset_id] = playbook_weight_bias.get(weighted_asset_id, 0.0) + numeric
            for factor_asset_id, value in dict(content.get("factor_weights") or {}).items():
                if factor_asset_id in factor_ids:
                    factor_weight_bias[factor_asset_id] = factor_weight_bias.get(factor_asset_id, 0.0) + float(value or 0.0) * performance_multiplier
            for playbook_asset_id, value in dict(content.get("playbook_weights") or {}).items():
                if playbook_asset_id in playbook_ids:
                    playbook_weight_bias[playbook_asset_id] = playbook_weight_bias.get(playbook_asset_id, 0.0) + float(value or 0.0) * performance_multiplier
            for symbol, value in dict(content.get("symbol_bias") or {}).items():
                symbol_score_bias[str(symbol)] = symbol_score_bias.get(str(symbol), 0.0) + float(value or 0.0) * performance_multiplier
            for sector, value in dict(content.get("sector_bias") or {}).items():
                sector_score_bias[str(sector)] = sector_score_bias.get(str(sector), 0.0) + float(value or 0.0) * performance_multiplier
            score_bonus += float(content.get("score_bonus", 0.0) or 0.0) * performance_multiplier
            summary_lines.append(f"{entry_asset_id} 已转正并参与本次 compose 加权")
            if entry_asset_id in requested_asset_ids:
                explicit_active_ids.append(entry_asset_id)
            else:
                auto_active_ids.append(entry_asset_id)
        return {
            "active_asset_ids": [str(item.get("id", "")).strip() for item in active_items if str(item.get("id", "")).strip()],
            "factor_weight_bias": factor_weight_bias,
            "playbook_weight_bias": playbook_weight_bias,
            "symbol_score_bias": symbol_score_bias,
            "sector_score_bias": sector_score_bias,
            "score_bonus": score_bonus,
            "summary": {
                "requested_count": len(request.learned_assets),
                "active_count": len(active_items),
                "explicit_active_count": len(explicit_active_ids),
                "auto_selected_count": len(auto_active_ids),
                "active_asset_ids": [str(item.get("id", "")).strip() for item in active_items if str(item.get("id", "")).strip()],
                "explicit_active_asset_ids": explicit_active_ids,
                "auto_selected_asset_ids": auto_active_ids,
                "factor_weight_bias": {key: round(value, 4) for key, value in factor_weight_bias.items()},
                "playbook_weight_bias": {key: round(value, 4) for key, value in playbook_weight_bias.items()},
                "summary_lines": summary_lines,
            },
        }
