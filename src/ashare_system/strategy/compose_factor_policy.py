"""compose 因子策略守门模块。"""

from __future__ import annotations

from typing import Any

from ..runtime_compose_contracts import RuntimeComposeFactorSpec
from .factor_registry import FactorRegistry


DEFAULT_FACTOR_SELECTION_POLICY: dict[str, Any] = {
    "ic_linear_slope": 5.0,
    "min_monitor_weight_multiplier": 0.1,
    "max_monitor_weight_multiplier": 1.5,
    "significance_level": 0.1,
    "significance_penalty_floor": 0.25,
    "same_correlation_group_soft_limit": 2,
    "same_correlation_secondary_multiplier": 0.5,
    "same_correlation_overflow_multiplier": 0.0,
    "max_ineffective_factors": 1,
    "max_unknown_factors": 2,
    "min_factor_count_for_healthy": 3,
    "max_dominant_dimension_ratio": 0.65,
    "max_effective_weight_concentration": 0.65,
    "trend_max_dominant_dimension_ratio": 0.9,
    "trend_max_effective_weight_concentration": 0.85,
    "rotation_max_dominant_dimension_ratio": 0.72,
    "rotation_max_effective_weight_concentration": 0.72,
    "defensive_max_dominant_dimension_ratio": 0.68,
    "defensive_max_effective_weight_concentration": 0.68,
    "selloff_max_dominant_dimension_ratio": 0.62,
    "selloff_max_effective_weight_concentration": 0.62,
    "unknown_budget_penalty_multiplier": 0.45,
}

NON_PENALIZED_EFFECTIVENESS = {"unsupported_for_monitor", "unavailable", "unknown"}
UNKNOWN_LIKE_EFFECTIVENESS = {"unavailable", "unknown"}


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def build_factor_effectiveness_map(effectiveness_snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("factor_id") or "").strip(): dict(item)
        for item in list((effectiveness_snapshot or {}).get("items") or [])
        if str(item.get("factor_id") or "").strip()
    }


def build_factor_selection_policy_contract() -> dict[str, Any]:
    config = dict(DEFAULT_FACTOR_SELECTION_POLICY)
    return {
        "config": config,
        "direct_compose_guard_enabled": True,
        "diversification_rules": [
            "不再按维度数量做硬限制，允许单维度集中，但必须暴露集中度与复核要求。",
            f"同一相关组最多 {config['same_correlation_group_soft_limit']} 个，第二个自动降权，更多直接剔除。",
            f"ineffective 因子预算最多 {config['max_ineffective_factors']} 个，其余自动剔除。",
            f"unknown/unavailable 因子预算最多 {config['max_unknown_factors']} 个，超限后自动压权并进入 review。",
            "监控可用因子按 mean_rank_ic 与 p_value 连续调权，不再固定乘 0.3。",
            "unsupported_for_monitor 因子不做 IC 惩罚，但必须进入 outcome attribution 事后归因链。",
        ],
        "rejection_conditions": [
            "降权与剔除后无可用因子。",
            "同相关组堆叠严重且仅剩单一主驱动，无法构成可解释组合。",
            "全部活动因子都处于 ineffective 且无任何可条件使用因子，组合噪声过高。",
        ],
        "warning_conditions": [
            "出现 ineffective 因子，会按 IC 与显著性连续降权，并占用无效因子预算。",
            "出现 unsupported_for_monitor/unavailable/unknown 因子，会保留但显式提示监控盲区或需看事后归因。",
            "主维度过度集中、单因子过度集中、或相关组冲突时，会标记 needs_review 并禁止自动派发。",
        ],
    }


def resolve_regime_policy_profile(
    policy_config: dict[str, Any],
    *,
    regime: str,
) -> dict[str, float | str]:
    normalized = str(regime or "").strip().lower()
    dominant = float(policy_config["max_dominant_dimension_ratio"])
    concentration = float(policy_config["max_effective_weight_concentration"])
    label = "generic"
    if normalized in {"trend", "recovery"}:
        dominant = float(policy_config["trend_max_dominant_dimension_ratio"])
        concentration = float(policy_config["trend_max_effective_weight_concentration"])
        label = "trend_like"
    elif normalized in {"rotation", "range"}:
        dominant = float(policy_config["rotation_max_dominant_dimension_ratio"])
        concentration = float(policy_config["rotation_max_effective_weight_concentration"])
        label = "rotation_like"
    elif normalized in {"defensive"}:
        dominant = float(policy_config["defensive_max_dominant_dimension_ratio"])
        concentration = float(policy_config["defensive_max_effective_weight_concentration"])
        label = "defensive"
    elif normalized in {"selloff", "chaos"}:
        dominant = float(policy_config["selloff_max_dominant_dimension_ratio"])
        concentration = float(policy_config["selloff_max_effective_weight_concentration"])
        label = "selloff_or_chaos"
    return {
        "regime": normalized or "unknown",
        "profile": label,
        "max_dominant_dimension_ratio": dominant,
        "max_effective_weight_concentration": concentration,
    }


def _resolve_monitor_weight_multiplier(
    *,
    mean_rank_ic: float,
    p_value: float,
    policy_config: dict[str, Any],
) -> float:
    base = _clamp(
        1.0 + float(mean_rank_ic) * float(policy_config["ic_linear_slope"]),
        float(policy_config["min_monitor_weight_multiplier"]),
        float(policy_config["max_monitor_weight_multiplier"]),
    )
    significance_level = float(policy_config["significance_level"])
    if p_value <= significance_level:
        return round(base, 4)
    significance_penalty = _clamp(
        1.0 - ((float(p_value) - significance_level) / max(1.0 - significance_level, 1e-9)),
        float(policy_config["significance_penalty_floor"]),
        1.0,
    )
    return round(
        max(float(policy_config["min_monitor_weight_multiplier"]), base * significance_penalty),
        4,
    )


def evaluate_compose_factor_policy(
    factors: list[RuntimeComposeFactorSpec],
    *,
    factor_registry: FactorRegistry,
    effectiveness_snapshot: dict[str, Any] | None = None,
    policy_config: dict[str, Any] | None = None,
    requested_weights_override: dict[str, float] | None = None,
    regime: str = "",
) -> dict[str, Any]:
    config = {**DEFAULT_FACTOR_SELECTION_POLICY, **dict(policy_config or {})}
    effectiveness_map = build_factor_effectiveness_map(effectiveness_snapshot)
    regime_profile = resolve_regime_policy_profile(config, regime=regime)

    adjusted_specs: list[RuntimeComposeFactorSpec] = []
    requested_factor_weights: dict[str, float] = {}
    adjusted_factor_weights: dict[str, float] = {}
    factor_effectiveness_trace: dict[str, dict[str, Any]] = {}
    factor_meta: list[dict[str, Any]] = []
    factor_auto_downgrades: list[dict[str, Any]] = []
    factor_rejections: list[dict[str, Any]] = []
    effect_warnings: list[str] = []
    diversification_warnings: list[str] = []

    for item in factors:
        cloned = item.model_copy(deep=True)
        base_weight = float(
            (requested_weights_override or {}).get(cloned.id, cloned.weight if cloned.weight is not None else 0.0) or 0.0
        )
        cloned.weight = base_weight
        definition = factor_registry.get(cloned.id, cloned.version)
        resolved_group = str(cloned.group or (definition.group if definition is not None else "") or "").strip()
        correlation_group = str(getattr(definition, "correlation_group", "") or "").strip()
        cloned.group = resolved_group
        adjusted_specs.append(cloned)
        requested_factor_weights[cloned.id] = round(base_weight, 4)
        trace = dict(effectiveness_map.get(cloned.id) or {})
        status = str(trace.get("status") or "unknown").strip() or "unknown"
        factor_effectiveness_trace[cloned.id] = {
            **trace,
            "factor_id": cloned.id,
            "group": resolved_group,
            "correlation_group": correlation_group,
            "requested_weight": round(base_weight, 4),
            "adjusted_weight": round(base_weight, 4),
            "monitor_weight_multiplier": 1.0,
        }
        factor_meta.append(
            {
                "id": cloned.id,
                "version": cloned.version,
                "group": resolved_group,
                "correlation_group": correlation_group,
                "status": status,
            }
        )

    meta_by_id = {item["id"]: item for item in factor_meta}
    spec_by_id = {item.id: item for item in adjusted_specs}

    def _record_downgrade(
        factor_id: str,
        *,
        rule: str,
        previous_weight: float,
        current_weight: float,
        detail: str,
    ) -> None:
        factor_auto_downgrades.append(
            {
                "factor_id": factor_id,
                "rule": rule,
                "detail": detail,
                "previous_weight": round(previous_weight, 4),
                "adjusted_weight": round(current_weight, 4),
            }
        )

    def _record_rejection(
        factor_id: str,
        *,
        rule: str,
        previous_weight: float,
        detail: str,
    ) -> None:
        factor_rejections.append(
            {
                "factor_id": factor_id,
                "rule": rule,
                "detail": detail,
                "previous_weight": round(previous_weight, 4),
                "adjusted_weight": 0.0,
            }
        )

    for factor in sorted(adjusted_specs, key=lambda item: abs(float(item.weight or 0.0)), reverse=True):
        meta = meta_by_id[factor.id]
        trace = factor_effectiveness_trace[factor.id]
        status = str(meta["status"] or "unknown")
        mean_rank_ic = float(trace.get("mean_rank_ic", 0.0) or 0.0)
        p_value = float(trace.get("p_value", 1.0) or 1.0)
        if status in {"effective", "ineffective"}:
            previous = float(factor.weight or 0.0)
            multiplier = _resolve_monitor_weight_multiplier(
                mean_rank_ic=mean_rank_ic,
                p_value=p_value,
                policy_config=config,
            )
            factor.weight = previous * multiplier
            trace["monitor_weight_multiplier"] = multiplier
            if float(factor.weight or 0.0) < previous - 1e-9:
                _record_downgrade(
                    factor.id,
                    rule="monitor_ic_weighting",
                    previous_weight=previous,
                    current_weight=float(factor.weight or 0.0),
                    detail=(
                        f"基于 mean_rank_ic={round(mean_rank_ic, 4)} 与 p_value={round(p_value, 4)} "
                        f"执行连续权重调整，乘数={multiplier}。"
                    ),
                )
            if status == "ineffective":
                consecutive_invalid_days = int(trace.get("consecutive_invalid_days", 0) or 0)
                if consecutive_invalid_days >= 5:
                    previous_invalid_weight = float(factor.weight or 0.0)
                    factor.weight = 0.0
                    trace["auto_disabled"] = True
                    _record_rejection(
                        factor.id,
                        rule="consecutive_invalid_auto_disable",
                        previous_weight=previous_invalid_weight,
                        detail=f"连续 {consecutive_invalid_days} 天 ineffective，已自动禁用。",
                    )
                else:
                    previous_invalid_weight = float(factor.weight or 0.0)
                    capped_weight = min(previous_invalid_weight, previous * 0.5)
                    if capped_weight < previous_invalid_weight - 1e-9:
                        factor.weight = capped_weight
                        _record_downgrade(
                            factor.id,
                            rule="ineffective_half_weight",
                            previous_weight=previous_invalid_weight,
                            current_weight=float(factor.weight or 0.0),
                            detail=f"连续 {consecutive_invalid_days} 天 ineffective，执行半权保留。",
                        )
                    trace["degraded"] = True
                effect_warnings.append(
                    f"{factor.id} 当前监控结论为 ineffective，已按连续 IC 规则降权，并根据连续失效天数执行降级/禁用。"
                )
        elif status in NON_PENALIZED_EFFECTIVENESS:
            effect_warnings.append(
                f"{factor.id} 当前状态={status}，不做 IC 惩罚；如为 unsupported_for_monitor，将依赖 outcome attribution 做后验归因。"
            )

    correlation_map: dict[str, list[str]] = {}
    for factor in adjusted_specs:
        correlation_group = str(meta_by_id[factor.id]["correlation_group"] or "").strip()
        if not correlation_group:
            continue
        correlation_map.setdefault(correlation_group, []).append(factor.id)

    for correlation_group, factor_ids in correlation_map.items():
        ranked_ids = sorted(
            factor_ids,
            key=lambda factor_id: abs(float(spec_by_id[factor_id].weight or 0.0)),
            reverse=True,
        )
        if len(ranked_ids) <= 1:
            continue
        diversification_warnings.append(
            f"相关组 {correlation_group} 存在 {len(ranked_ids)} 个因子，已做正交化约束。"
        )
        secondary_id = ranked_ids[1]
        secondary_factor = spec_by_id[secondary_id]
        secondary_previous = float(secondary_factor.weight or 0.0)
        secondary_factor.weight = secondary_previous * float(config["same_correlation_secondary_multiplier"])
        _record_downgrade(
            secondary_id,
            rule="correlation_secondary_penalty",
            previous_weight=secondary_previous,
            current_weight=float(secondary_factor.weight or 0.0),
            detail=f"相关组 {correlation_group} 中仅保留一个主因子，第二个按辅助权重保留。",
        )
        for factor_id in ranked_ids[int(config["same_correlation_group_soft_limit"]) :]:
            factor = spec_by_id[factor_id]
            previous = float(factor.weight or 0.0)
            factor.weight = previous * float(config["same_correlation_overflow_multiplier"])
            _record_rejection(
                factor_id,
                rule="correlation_overflow_reject",
                previous_weight=previous,
                detail=f"相关组 {correlation_group} 超出上限，已从执行层剔除。",
            )

    ineffective_ids = [
        factor.id
        for factor in sorted(adjusted_specs, key=lambda item: abs(float(item.weight or 0.0)), reverse=True)
        if meta_by_id[factor.id]["status"] == "ineffective" and abs(float(factor.weight or 0.0)) > 0
    ]
    for factor_id in ineffective_ids[int(config["max_ineffective_factors"]) :]:
        factor = spec_by_id[factor_id]
        previous = float(factor.weight or 0.0)
        factor.weight = 0.0
        _record_rejection(
            factor_id,
            rule="ineffective_budget_reject",
            previous_weight=previous,
            detail="ineffective 因子预算已耗尽，超额部分已剔除。",
        )

    unknown_ids = [
        factor.id
        for factor in sorted(adjusted_specs, key=lambda item: abs(float(item.weight or 0.0)), reverse=True)
        if meta_by_id[factor.id]["status"] in UNKNOWN_LIKE_EFFECTIVENESS and abs(float(factor.weight or 0.0)) > 0
    ]
    if len(unknown_ids) > int(config["max_unknown_factors"]):
        diversification_warnings.append("unknown/unavailable 因子数量超预算，组合解释性下降。")
    for factor_id in unknown_ids[int(config["max_unknown_factors"]) :]:
        factor = spec_by_id[factor_id]
        previous = float(factor.weight or 0.0)
        factor.weight = previous * float(config["unknown_budget_penalty_multiplier"])
        _record_downgrade(
            factor_id,
            rule="unknown_budget_penalty",
            previous_weight=previous,
            current_weight=float(factor.weight or 0.0),
            detail="监控结论不足的因子超预算，已自动压缩权重。",
        )

    for factor in adjusted_specs:
        adjusted_factor_weights[factor.id] = round(float(factor.weight or 0.0), 4)
        factor_effectiveness_trace[factor.id]["adjusted_weight"] = adjusted_factor_weights[factor.id]
        factor_effectiveness_trace[factor.id]["status"] = meta_by_id[factor.id]["status"]

    active_ids = [
        factor.id
        for factor in adjusted_specs
        if abs(float(factor.weight or 0.0)) > 1e-9
    ]
    total_abs_weight = sum(abs(float(spec_by_id[factor_id].weight or 0.0)) for factor_id in active_ids) or 1.0
    dimension_weight_map: dict[str, float] = {}
    active_effective_count = 0
    active_ineffective_count = 0
    active_unknown_count = 0
    active_unsupported_count = 0
    active_usable_count = 0
    for factor_id in active_ids:
        meta = meta_by_id[factor_id]
        status = str(meta["status"] or "unknown")
        weight = abs(float(spec_by_id[factor_id].weight or 0.0))
        group = str(meta["group"] or "ungrouped").strip() or "ungrouped"
        dimension_weight_map[group] = dimension_weight_map.get(group, 0.0) + weight
        if status == "effective":
            active_effective_count += 1
            active_usable_count += 1
        elif status == "ineffective":
            active_ineffective_count += 1
        elif status == "unsupported_for_monitor":
            active_unsupported_count += 1
            active_usable_count += 1
        else:
            active_unknown_count += 1
            active_usable_count += 1

    dimension_count = len(dimension_weight_map)
    dominant_dimension_ratio = max(dimension_weight_map.values(), default=0.0) / total_abs_weight
    effective_weight_concentration = (
        max((abs(float(spec_by_id[factor_id].weight or 0.0)) for factor_id in active_ids), default=0.0) / total_abs_weight
    )
    correlation_conflict_count = sum(
        max(
            len(
                [
                    factor_id
                    for factor_id in factor_ids
                    if abs(float(spec_by_id[factor_id].weight or 0.0)) > 1e-9
                ]
            )
            - 1,
            0,
        )
        for factor_ids in correlation_map.values()
    )

    health_status = "healthy"
    if not active_ids or (active_effective_count == 0 and active_usable_count == 0):
        health_status = "noise_heavy"
    elif (
        dominant_dimension_ratio > float(regime_profile["max_dominant_dimension_ratio"])
        or effective_weight_concentration > float(regime_profile["max_effective_weight_concentration"])
    ):
        health_status = "concentrated"
    elif active_ineffective_count + active_unknown_count > max(active_effective_count + active_usable_count, 1):
        health_status = "noise_heavy"
    elif correlation_conflict_count > 0 or (
        len(active_ids) >= int(config["min_factor_count_for_healthy"])
        and dimension_count <= 1
        and effective_weight_concentration < float(regime_profile["max_effective_weight_concentration"])
    ):
        health_status = "pseudo_diversified"

    needs_review = bool(
        health_status != "healthy"
        or factor_rejections
        or active_effective_count < 1
        or len(adjusted_specs) < int(config["min_factor_count_for_healthy"])
    )
    hard_reject = bool(
        not active_ids
        or (
            len(adjusted_specs) >= 3
            and active_effective_count == 0
            and active_ineffective_count == len(active_ids)
            and len(active_ids) > 0
        )
        or (
            len(factor_rejections) >= max(len(adjusted_specs) - 1, 2)
            and len(adjusted_specs) >= 3
            and active_effective_count == 0
            and active_usable_count <= 1
        )
    )

    factor_mix_validation = {
        "passed": not hard_reject,
        "needs_review": needs_review,
        "min_factor_count_met": len(active_ids) >= min(int(config["min_factor_count_for_healthy"]), len(adjusted_specs) or 1),
        "min_dimension_count_met": True,
        "ineffective_budget_ok": active_ineffective_count <= int(config["max_ineffective_factors"]),
        "unknown_budget_ok": active_unknown_count <= int(config["max_unknown_factors"]),
        "correlation_conflict_count": correlation_conflict_count,
        "dimension_overweight_groups": [
            group
            for group, weight in dimension_weight_map.items()
            if total_abs_weight > 0 and weight / total_abs_weight > float(regime_profile["max_dominant_dimension_ratio"])
        ],
    }
    factor_portfolio_health = {
        "factor_count": len(adjusted_specs),
        "active_factor_count": len(active_ids),
        "effective_factor_count": active_effective_count,
        "usable_factor_count": active_usable_count,
        "dimension_count": dimension_count,
        "ineffective_factor_count": active_ineffective_count,
        "unknown_factor_count": active_unknown_count,
        "unsupported_factor_count": active_unsupported_count,
        "correlation_conflict_count": correlation_conflict_count,
        "dominant_dimension_ratio": round(dominant_dimension_ratio, 4),
        "effective_weight_concentration": round(effective_weight_concentration, 4),
        "regime_profile": regime_profile,
        "health_status": health_status,
    }

    summary_lines = [
        f"请求 {len(adjusted_specs)} 个因子，执行层保留 {len(active_ids)} 个有效权重因子。",
        f"有效因子 {active_effective_count} 个，可条件使用因子 {active_usable_count} 个，维度覆盖 {dimension_count} 个。",
        (
            f"组合健康度={health_status}，主维度集中度={round(dominant_dimension_ratio, 4)}，"
            f"单因子集中度={round(effective_weight_concentration, 4)}，当前 regime 守门档位={regime_profile['profile']}。"
        ),
    ]
    if factor_auto_downgrades:
        summary_lines.append(f"本轮共自动调权 {len(factor_auto_downgrades)} 次。")
    if factor_rejections:
        summary_lines.append(f"本轮共剔除 {len(factor_rejections)} 个超限因子。")
    if effect_warnings or diversification_warnings:
        summary_lines.extend((effect_warnings + diversification_warnings)[:4])

    return {
        "factors": adjusted_specs,
        "policy_config": config,
        "regime_profile": regime_profile,
        "requested_factor_weights": requested_factor_weights,
        "adjusted_factor_weights": adjusted_factor_weights,
        "factor_effectiveness_trace": factor_effectiveness_trace,
        "factor_mix_validation": factor_mix_validation,
        "factor_portfolio_health": factor_portfolio_health,
        "factor_auto_downgrades": factor_auto_downgrades,
        "factor_rejections": factor_rejections,
        "effectiveness_warnings": effect_warnings,
        "diversification_warnings": diversification_warnings,
        "needs_review": needs_review,
        "hard_reject": hard_reject,
        "factor_policy_summary": {
            "summary_lines": summary_lines,
            "health_status": health_status,
            "needs_review": needs_review,
        },
    }
