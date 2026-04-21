"""Strategy Composer - 将 Agent 的 compose 请求编排成可消费候选与解释。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING
from uuid import uuid4
import pandas as pd

from ..data.cache import KlineCache
from ..runtime_compose_contracts import RuntimeComposeFactorSpec, RuntimeComposeRequest
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
        kline_cache: KlineCache | None = None,
    ) -> None:
        self._factor_registry = factor_registry
        self._playbook_registry = playbook_registry
        self._evaluation_ledger = evaluation_ledger
        self._kline_cache = kline_cache

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
        factor_policy = dict(request.market_context.get("factor_policy") or {})
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
            factor_policy=factor_policy,
        )
        return {
            "market_summary": self._build_market_summary(pipeline_payload, market_drivers=market_drivers),
            "candidates": candidates,
            "filtered_out": filtered_out if request.output.include_filtered_reasons else [],
            "explanations": explanations,
            "factor_policy_summary": dict(factor_policy.get("factor_policy_summary") or {}),
            "factor_portfolio_health": dict(factor_policy.get("factor_portfolio_health") or {}),
            "factor_mix_validation": dict(factor_policy.get("factor_mix_validation") or {}),
            "factor_effectiveness_trace": dict(factor_policy.get("factor_effectiveness_trace") or {}),
            "factor_auto_downgrades": [dict(item) for item in list(factor_policy.get("factor_auto_downgrades") or [])],
            "factor_rejections": [dict(item) for item in list(factor_policy.get("factor_rejections") or [])],
            "effectiveness_warnings": [str(item) for item in list(factor_policy.get("effectiveness_warnings") or []) if str(item).strip()],
            "diversification_warnings": [str(item) for item in list(factor_policy.get("diversification_warnings") or []) if str(item).strip()],
            "requested_factor_weights": dict(factor_policy.get("requested_factor_weights") or {}),
            "adjusted_factor_weights": dict(factor_policy.get("adjusted_factor_weights") or {}),
            "needs_review": bool(factor_policy.get("needs_review")),
            "review_action": dict(factor_policy.get("review_action") or {}),
            "proposal_packet": {
                "selected_symbols": [item.get("symbol", "") for item in candidates[:3]],
                "watchlist_symbols": [item.get("symbol", "") for item in candidates[3 : request.output.max_candidates]],
                "discussion_focus": [
                    "这批候选是否贴合当前市场假设",
                    "是否优于现有持仓",
                    "是否需要风控进一步挑反证",
                    "active learned asset 是否真的改善了当前排序与证据质量",
                    "市场驱动因素(热点/事件)是否已充分反映在排序中",
                    "因子守门是否已提示 needs_review，以及是否需要立即重组组合",
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
        sector_strength_map: dict[str, float] = {}
        for index, sector_name in enumerate(list(market_profile.get("hot_sectors") or [])):
            if not str(sector_name).strip():
                continue
            sector_strength_map[str(sector_name)] = max(0.3, 1.0 - index * 0.12)
        for item in list(market_profile.get("sector_profiles") or []):
            sector_name = str((item or {}).get("sector_name") or "").strip()
            if not sector_name:
                continue
            strength_score = float((item or {}).get("strength_score", 0.0) or 0.0)
            sector_strength_map[sector_name] = max(
                sector_strength_map.get(sector_name, 0.0),
                min(max(strength_score / 10.0, 0.0), 1.2),
            )
        
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
            "sector_strength_map": sector_strength_map,
            "event_bonus_map": event_bonus_map,
            "event_reason_map": event_reason_map,
            "monitor_bonus_map": monitor_bonus_map,
            "event_highlights": list((event_context or {}).get("highlights") or []),
        }

    def _build_market_summary(self, pipeline_payload: dict[str, Any], market_drivers: dict[str, Any] | None = None) -> dict[str, Any]:
        market_profile = pipeline_payload.get("market_profile") or {}
        drivers = market_drivers or {}
        detected_market_regime = dict(pipeline_payload.get("detected_market_regime") or {})
        return {
            "market_regime": detected_market_regime.get("runtime_regime") or market_profile.get("regime", ""),
            "market_regime_label": detected_market_regime.get("regime_label", ""),
            "hot_sectors": list(drivers.get("hot_sectors") or market_profile.get("hot_sectors") or []),
            "risk_flags": market_profile.get("market_risk_flags", []),
            "event_drive_active": bool(drivers.get("event_bonus_map")),
            "sector_strength_map": dict(drivers.get("sector_strength_map") or {}),
            "regime_confidence": float(detected_market_regime.get("confidence", 0.0) or 0.0),
            "regime_mismatch_warning": str(pipeline_payload.get("regime_mismatch_warning") or ""),
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
        factor_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_preferences = request.constraints.user_preferences or {}
        factor_specs = list(request.strategy.factors)
        playbook_specs = list(request.strategy.playbooks)
        summary_lines = list((constraint_trace or {}).get("summary_lines") or [])
        learned_summary = dict((learned_asset_plan or {}).get("summary") or {})
        drivers = market_drivers or {}
        factor_policy = factor_policy or {}
        portfolio_health = dict(factor_policy.get("factor_portfolio_health") or {})
        policy_summary = dict(factor_policy.get("factor_policy_summary") or {})
        review_action = dict(factor_policy.get("review_action") or {})
        auto_downgrades = [dict(item) for item in list(factor_policy.get("factor_auto_downgrades") or [])]
        rejections = [dict(item) for item in list(factor_policy.get("factor_rejections") or [])]
        regime_warning = str(request.market_context.get("regime_mismatch_warning") or "").strip()
        
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
                f"复合调整倍数配置: {getattr(runtime_config, 'composite_adjustment_multiplier', 0.0)}",
            ]
            + summary_lines,
            "market_driver_summary": driver_summary,
            "market_regime_summary": [
                f"服务端检测市场状态={request.market_context.get('market_regime') or 'unknown'}"
            ] + ([regime_warning] if regime_warning else []),
            "learned_asset_summary": learned_summary,
            "factor_policy_summary": [str(item) for item in list(policy_summary.get("summary_lines") or []) if str(item).strip()],
            "factor_portfolio_health": portfolio_health,
            "review_action_summary": [
                f"review_action={review_action.get('action')} auto_dispatch_allowed={review_action.get('auto_dispatch_allowed')} auto_execution_allowed={review_action.get('auto_execution_allowed')}"
            ]
            if review_action
            else [],
            "factor_adjustment_summary": [
                f"{item.get('factor_id')} 因 {item.get('rule')} 自动降权到 {round(float(item.get('adjusted_weight', 0.0) or 0.0), 4)}"
                for item in auto_downgrades[:5]
            ]
            + [
                f"{item.get('factor_id')} 因 {item.get('rule')} 被剔除"
                for item in rejections[:3]
            ],
        }

    def _resolve_composite_multiplier(self, runtime_config: RuntimeConfig) -> tuple[float, dict[str, Any]]:
        configured = float(getattr(runtime_config, "composite_adjustment_multiplier", 0.0) or 0.0)
        if configured > 0 and abs(configured - 10.0) > 1e-9:
            return configured, {"source": "runtime_config", "configured_value": round(configured, 4), "auto_estimate": None}
        auto_estimate = (
            self._evaluation_ledger.estimate_composite_multiplier()
            if self._evaluation_ledger is not None
            else {
                "available": False,
                "resolved_multiplier": 4.0,
                "source": "conservative_fallback",
                "reason": "evaluation_ledger_missing",
            }
        )
        resolved = float(auto_estimate.get("resolved_multiplier", 4.0) or 4.0)
        return resolved, {
            "source": str(auto_estimate.get("source") or "conservative_fallback"),
            "configured_value": round(configured, 4),
            "auto_estimate": auto_estimate,
        }

    def _orthogonalize_factor_weights(
        self,
        factors: list[RuntimeComposeFactorSpec],
        raw_weights: dict[str, float],
    ) -> tuple[dict[str, float], dict[str, Any]]:
        normalized = dict(raw_weights)
        grouped: dict[str, list[str]] = {}
        for factor in factors:
            definition = self._factor_registry.get(factor.id, factor.version)
            correlation_group = str(getattr(definition, "correlation_group", "") or "").strip()
            if not correlation_group:
                continue
            grouped.setdefault(correlation_group, []).append(factor.id)

        meta: dict[str, Any] = {}
        for correlation_group, factor_ids in grouped.items():
            group_abs_sum = sum(abs(float(raw_weights.get(factor_id, 0.0) or 0.0)) for factor_id in factor_ids)
            group_cap = max((abs(float(raw_weights.get(factor_id, 0.0) or 0.0)) for factor_id in factor_ids), default=0.0)
            if group_abs_sum <= group_cap or group_cap <= 0:
                continue
            scale = group_cap / group_abs_sum
            for factor_id in factor_ids:
                normalized[factor_id] = float(raw_weights.get(factor_id, 0.0) or 0.0) * scale
            meta[correlation_group] = {
                "factor_ids": factor_ids,
                "raw_abs_sum": round(group_abs_sum, 4),
                "capped_abs_sum": round(group_cap, 4),
                "scale": round(scale, 4),
            }
        return normalized, meta

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
        sector_strength_map = dict(drivers.get("sector_strength_map") or {})
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
        factor_policy = dict(request.market_context.get("factor_policy") or {})
        
        effective_playbook_weights = {
            item.id: max(item.weight + float(playbook_weight_bias.get(item.id, 0.0) or 0.0), 0.0)
            for item in request.strategy.playbooks
        }
        effective_factor_weights = {
            item.id: item.weight + float(factor_weight_bias.get(item.id, 0.0) or 0.0)
            for item in request.strategy.factors
        }
        effective_factor_weights, orthogonalization_meta = self._orthogonalize_factor_weights(
            request.strategy.factors,
            effective_factor_weights,
        )
        total_playbook_weight = sum(effective_playbook_weights.values()) or 1.0
        factor_total_weight = sum(max(abs(value), 0.0) for value in effective_factor_weights.values()) or 1.0
        composite_multiplier, multiplier_meta = self._resolve_composite_multiplier(runtime_config)
        
        context = {
            "market_hypothesis": request.intent.market_hypothesis,
            "trade_horizon": request.intent.trade_horizon,
            "holding_symbols": request.market_context.get("holding_symbols", []),
            "hot_sectors": list(hot_sectors),
            "focus_sectors": list(request.market_context.get("focus_topics") or []),
            "event_highlights": list(drivers.get("event_highlights") or []),
            "highlights": list(drivers.get("event_highlights") or []),
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
                driver_bonus += 1.2 + float(sector_strength_map.get(resolved_sector, 0.5) or 0.0)
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
                    context={**context, "playbook_params": dict(playbook.params or {})},
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
                    context={**context, "factor_params": dict(factor.params or {})},
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
            composite_score = round(selection_score + composite_adjustment * composite_multiplier + learned_bonus + driver_bonus, 2)
            
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
                "selection_score": round(selection_score, 4),
                "composite_adjustment": round(composite_adjustment, 4),
                "resolved_sector": resolved_sector,
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
                "scoring_meta": {
                    "composite_adjustment_multiplier": round(composite_multiplier, 4),
                    "composite_adjustment_meta": multiplier_meta,
                    "driver_bonus": round(driver_bonus, 4),
                    "learned_bonus": round(learned_bonus, 4),
                    "factor_orthogonalization": {
                        "applied": bool(orthogonalization_meta),
                        "groups": orthogonalization_meta,
                        "effective_factor_weights": {
                            key: round(float(value or 0.0), 4)
                            for key, value in effective_factor_weights.items()
                        },
                    },
                    "factor_policy": {
                        "needs_review": bool(factor_policy.get("needs_review")),
                        "health_status": str((factor_policy.get("factor_portfolio_health") or {}).get("health_status") or ""),
                        "requested_factor_weights": dict(factor_policy.get("requested_factor_weights") or {}),
                        "adjusted_factor_weights": dict(factor_policy.get("adjusted_factor_weights") or {}),
                        "review_action": dict(factor_policy.get("review_action") or {}),
                    },
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
            all_bars = []
            if self._kline_cache is not None:
                for symbol in symbols:
                    all_bars.extend(
                        self._kline_cache.get_or_fetch(
                            symbol,
                            "1d",
                            60,
                            market_adapter,
                            end_time=trade_date,
                        )
                    )
            else:
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
                    returns: list[float] = []
                    for record in records:
                        if entry_asset_id not in set(record.get("active_learned_asset_ids", []) or []):
                            continue
                        if self._evaluation_ledger is not None:
                            returns.append(float(self._evaluation_ledger._extract_return_metric(record)))  # type: ignore[attr-defined]
                    positive_returns = [value for value in returns if value > 0]
                    negative_returns = [abs(value) for value in returns if value < 0]
                    sample_count = len(returns)
                    win_rate = len(positive_returns) / sample_count if sample_count > 0 else 0.0
                    avg_win = sum(positive_returns) / len(positive_returns) if positive_returns else 0.0
                    avg_loss = sum(negative_returns) / len(negative_returns) if negative_returns else 0.0
                    profit_loss_ratio = avg_win / max(avg_loss, 1e-9) if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0)
                    expected_value = win_rate * avg_win - (1.0 - win_rate) * avg_loss
                    if expected_value < 0 and sample_count >= 10:
                        performance_multiplier = 0.5
                        summary_lines.append(
                            f"{entry_asset_id} 期望收益为负(EV={expected_value:.4f}, 样本={sample_count})，权重已衰减 50%"
                        )
                    elif expected_value > 0 and win_rate < 0.4 and sample_count >= 10:
                        summary_lines.append(
                            f"{entry_asset_id} 低胜率但正期望(win_rate={win_rate:.0%}, 盈亏比={profit_loss_ratio:.2f})，标记 high_volatility_strategy"
                        )

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
