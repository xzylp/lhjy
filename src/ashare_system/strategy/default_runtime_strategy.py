"""默认 runtime / scheduler 共享的候选排序与策略上下文构建。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..contracts import MarketProfile, SectorProfile, StockBehaviorProfile

_GENERIC_SECTOR_TAG_KEYWORDS = (
    "持股",
    "溢价股",
    "中盘",
    "大盘",
    "小盘",
    "微盘",
    "低价股",
    "沪股通",
    "深股通",
    "融资融券",
    "转融券",
    "中字头",
    "成分股",
    "指数",
    "标的",
)
_GENERIC_SECTOR_TAG_EXACT = {
    "QFII持股",
    "AH溢价股",
    "中盘",
}


def safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def resolve_symbol_limit_pct(symbol: str) -> float:
    normalized = str(symbol or "").replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if normalized.startswith(("300", "301", "688", "689")):
        return 0.2
    if normalized.startswith(("43", "83", "87", "88", "92")):
        return 0.3
    return 0.1


def score_runtime_snapshot(
    *,
    symbol: str,
    last_price: float,
    pre_close: float,
    volume: float,
    rank: int,
) -> dict[str, Any]:
    """默认 runtime 粗评分，优先贴近超短活跃度，压低冷门与逆势票。"""

    base_price = pre_close or last_price or 1.0
    change_pct = (last_price - base_price) / max(base_price, 1e-9)
    momentum_pct = change_pct * 100.0
    limit_pct = resolve_symbol_limit_pct(symbol)

    liquidity_score = min(max(volume / 1_500_000.0, 0.0), 1.4) * 8.5
    price_bias_score = clamp(momentum_pct * 1.1, -12.0, 12.0)

    if change_pct >= min(limit_pct - 0.012, 0.085):
        trend_quality_score = 10.0
    elif change_pct >= 0.05:
        trend_quality_score = 8.0
    elif change_pct >= 0.025:
        trend_quality_score = 5.5
    elif change_pct >= 0.01:
        trend_quality_score = 2.8
    elif change_pct >= -0.005:
        trend_quality_score = 0.4
    elif change_pct >= -0.02:
        trend_quality_score = -3.5
    else:
        trend_quality_score = -7.5

    stability_anchor = 0.035 if change_pct >= 0 else -0.005
    stability_score = max(0.0, 4.5 - abs(change_pct - stability_anchor) * 90.0)

    limit_proximity_bonus = 0.0
    if 0.06 <= change_pct < max(limit_pct - 0.012, 0.075):
        limit_proximity_bonus = 3.8
    elif 0.04 <= change_pct < 0.06:
        limit_proximity_bonus = 1.8

    cold_penalty = 0.0
    if change_pct < -0.03:
        cold_penalty = -7.0
    elif change_pct < -0.015:
        cold_penalty = -4.2
    elif change_pct < 0:
        cold_penalty = -1.8

    rank_penalty_score = min(max(rank - 1, 0), 19) * 0.7

    selection_score = round(
        clamp(
            52.0
            + liquidity_score
            + price_bias_score
            + trend_quality_score
            + stability_score
            + limit_proximity_bonus
            + cold_penalty
            - rank_penalty_score,
            36.0,
            96.0,
        ),
        2,
    )
    action = "BUY" if selection_score >= 72.0 else "HOLD"
    return {
        "symbol": symbol,
        "rank": rank,
        "selection_score": selection_score,
        "action": action,
        "score_breakdown": {
            "coarse_score": selection_score,
            "momentum_pct": round(momentum_pct, 2),
            "liquidity_score": round(liquidity_score, 2),
            "price_bias_score": round(price_bias_score, 2),
            "trend_quality_score": round(trend_quality_score, 2),
            "stability_score": round(stability_score, 2),
            "limit_proximity_bonus": round(limit_proximity_bonus, 2),
            "cold_penalty": round(cold_penalty, 2),
            "rank_penalty_score": round(rank_penalty_score, 2),
        },
        "summary": (
            f"{symbol} 默认评分 {selection_score}，"
            f"涨幅 {momentum_pct:.2f}% ，"
            f"{'接近强势区间' if limit_proximity_bonus > 0 else '观察量价强度'}"
        ),
    }


def _sector_tag_priority(tag: str) -> float:
    text = str(tag or "").strip()
    if not text:
        return -999.0
    score = float(min(len(text), 12))
    if text in _GENERIC_SECTOR_TAG_EXACT:
        score -= 8.0
    if any(keyword in text for keyword in _GENERIC_SECTOR_TAG_KEYWORDS):
        score -= 4.0
    if text.endswith("股"):
        score -= 1.0
    if text.endswith("概念") or text.endswith("产业") or text.endswith("链") or text.endswith("题材"):
        score += 1.5
    return score


def pick_preferred_sector_tag(tags: list[Any]) -> str:
    normalized = [str(item).strip() for item in list(tags or []) if str(item).strip()]
    if not normalized:
        return ""
    ordered = sorted(normalized, key=lambda item: (_sector_tag_priority(item), len(item)), reverse=True)
    return ordered[0]


def resolve_symbol_sector_map(
    pack_items: list[dict[str, Any]],
    selected_symbols: list[str],
    market_adapter: Any,
    *,
    fallback_top_picks: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    unresolved = set(selected_symbols)
    item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
    fallback_map = {
        str(item.get("symbol")): str(item.get("resolved_sector") or "").strip()
        for item in list(fallback_top_picks or [])
        if str(item.get("symbol") or "").strip()
    }

    for symbol in list(unresolved):
        item = item_map.get(symbol) or {}
        tags = (((item.get("symbol_context") or {}).get("sector_relative") or {}).get("sector_tags") or [])
        preferred_tag = pick_preferred_sector_tag(tags)
        if preferred_tag:
            resolved[symbol] = preferred_tag
            unresolved.discard(symbol)
            continue
        dossier_sector = str(
            item.get("resolved_sector")
            or item.get("sector")
            or item.get("sector_name")
            or ""
        ).strip()
        if dossier_sector:
            resolved[symbol] = dossier_sector
            unresolved.discard(symbol)
            continue
        fallback_sector = fallback_map.get(symbol, "")
        if fallback_sector:
            resolved[symbol] = fallback_sector
            unresolved.discard(symbol)

    if unresolved:
        try:
            for sector_name in market_adapter.get_sectors():
                if not unresolved:
                    break
                try:
                    members = set(market_adapter.get_sector_symbols(sector_name))
                except Exception:
                    continue
                matched = unresolved.intersection(members)
                for symbol in matched:
                    resolved[symbol] = str(sector_name)
                unresolved -= matched
        except Exception:
            pass

    for symbol in unresolved:
        resolved[symbol] = fallback_map.get(symbol, "") or "候选池聚类"
    return resolved


def build_routing_market_profile(
    profile: MarketProfile,
    sector_profiles: list[SectorProfile],
) -> tuple[MarketProfile, dict[str, Any]]:
    """当情绪侧未放开战法，但热点链已经出现时，给默认链路一个保守路由提示。"""

    base_payload = profile.model_dump()
    if profile.allowed_playbooks:
        return profile, {
            "source": "market_profile",
            "allow_probe_routing": False,
            "playbooks": list(profile.allowed_playbooks),
        }

    ordered = sorted(sector_profiles, key=lambda item: (item.strength_score, item.reflow_score), reverse=True)
    suggested: list[str] = []
    if any(item.life_cycle in {"start", "ferment"} and item.strength_score >= 0.56 for item in ordered[:3]):
        suggested.append("sector_reflow_first_board")
    if any(item.life_cycle in {"ferment", "climax"} and item.strength_score >= 0.62 for item in ordered[:3]):
        suggested.append("divergence_reseal")
    if any(item.life_cycle == "ferment" and item.strength_score >= 0.72 for item in ordered[:2]):
        suggested.append("leader_chase")

    if not suggested:
        return profile, {
            "source": "market_profile_without_probe",
            "allow_probe_routing": False,
            "playbooks": [],
        }

    top_strength = max((item.strength_score for item in ordered), default=0.0)
    routing_regime = "trend" if top_strength >= 0.72 else "rotation"
    routing_score = 0.9 if routing_regime == "trend" else 0.65
    routing_profile = profile.model_copy(
        update={
            "regime": routing_regime,
            "regime_score": routing_score,
            "allowed_playbooks": suggested,
        }
    )
    return routing_profile, {
        "source": "conservative_probe_routing",
        "allow_probe_routing": True,
        "routing_regime": routing_regime,
        "playbooks": suggested,
    }


def infer_sector_profiles(
    pack_items: list[dict[str, Any]],
    selected_symbols: list[str],
    sector_map: dict[str, str],
    market_profile: MarketProfile,
) -> list[SectorProfile]:
    item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
    sector_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in selected_symbols:
        sector_name = sector_map.get(symbol, "")
        if sector_name:
            sector_items[sector_name].append(item_map.get(symbol, {"symbol": symbol}))

    profiles: list[SectorProfile] = []
    total_candidates = max(len(selected_symbols), 1)
    for sector_name, items in sector_items.items():
        scored = sorted(items, key=lambda item: float(item.get("selection_score", 0.0) or 0.0), reverse=True)
        leader_symbols = [str(item.get("symbol")) for item in scored if item.get("symbol")]
        relative_strengths = [
            float(value)
            for value in [
                (((item.get("symbol_context") or {}).get("market_relative") or {}).get("relative_strength_pct"))
                for item in items
            ]
            if value is not None
        ]
        sector_event_count = sum(
            int((((item.get("symbol_context") or {}).get("sector_relative") or {}).get("sector_event_count") or 0))
            for item in items
        )
        avg_relative_strength = safe_mean(relative_strengths)
        candidate_count = len(items)
        if avg_relative_strength <= -0.02:
            life_cycle = "retreat"
        elif candidate_count >= 4 or sector_event_count >= 4:
            life_cycle = "climax"
        elif candidate_count >= 2 or avg_relative_strength >= 0 or market_profile.regime == "trend":
            life_cycle = "ferment"
        else:
            life_cycle = "start"
        strength_score = min(
            max(
                0.45
                + candidate_count * 0.08
                + max(avg_relative_strength, 0.0) * 3.0
                + min(sector_event_count, 5) * 0.03,
                0.0,
            ),
            1.0,
        )
        profiles.append(
            SectorProfile(
                sector_name=sector_name,
                life_cycle=life_cycle,
                strength_score=round(strength_score, 4),
                zt_count=candidate_count,
                up_ratio=1.0 if avg_relative_strength >= 0 else 0.4,
                breadth_score=round(min(candidate_count / total_candidates, 1.0), 4),
                reflow_score=round(min(0.3 + sector_event_count * 0.1 + max(avg_relative_strength, 0.0) * 2.0, 1.0), 4),
                leader_symbols=leader_symbols[:5],
                active_days=max(1, min(candidate_count, 5)),
                zt_count_delta=max(candidate_count - 1, 0),
            )
        )
    return sorted(profiles, key=lambda item: (item.strength_score, len(item.leader_symbols)), reverse=True)


def infer_behavior_profiles(
    pack_items: list[dict[str, Any]],
    selected_symbols: list[str],
    sector_map: dict[str, str],
    sector_profiles: list[SectorProfile],
    decisions: list[dict[str, Any]],
    market_profile: MarketProfile,
) -> dict[str, StockBehaviorProfile]:
    item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
    decision_map = {str(item.get("symbol")): item for item in decisions if item.get("symbol")}
    sector_profile_map = {item.sector_name: item for item in sector_profiles}
    sector_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in selected_symbols:
        sector_name = sector_map.get(symbol, "")
        if sector_name:
            sector_items[sector_name].append(decision_map.get(symbol, {"symbol": symbol}))

    sector_rank_map: dict[str, int] = {}
    sector_count_map: dict[str, int] = {}
    for sector_name, items in sector_items.items():
        ranked = sorted(items, key=lambda item: float(item.get("selection_score", 0.0) or 0.0), reverse=True)
        sector_count_map[sector_name] = len(ranked)
        for index, item in enumerate(ranked, start=1):
            symbol = str(item.get("symbol") or "")
            if symbol:
                sector_rank_map[symbol] = index

    profiles: dict[str, StockBehaviorProfile] = {}
    for symbol in selected_symbols:
        decision = decision_map.get(symbol, {})
        dossier_item = item_map.get(symbol, {})
        symbol_context = dossier_item.get("symbol_context") or {}
        existing_profile = dossier_item.get("behavior_profile") or symbol_context.get("behavior_profile")
        if existing_profile:
            profiles[symbol] = StockBehaviorProfile.model_validate(existing_profile)
            continue
        sector_name = sector_map.get(symbol, "")
        sector_profile = sector_profile_map.get(sector_name)
        market_relative = (symbol_context.get("market_relative") or {}).get("relative_strength_pct")
        try:
            relative_strength = float(market_relative if market_relative is not None else 0.0)
        except (TypeError, ValueError):
            relative_strength = 0.0
        selection_score = float(decision.get("selection_score", 0.0) or 0.0)
        normalized_score = clamp((selection_score - 60.0) / 30.0, 0.0, 1.0)
        sector_rank = sector_rank_map.get(symbol, 99)
        sector_count = max(sector_count_map.get(sector_name, 1), 1)
        sector_event_count = int((((symbol_context.get("sector_relative") or {}).get("sector_event_count")) or 0))
        if sector_rank == 1 and selection_score >= 78 and market_profile.regime == "trend":
            style_tag = "leader"
        elif sector_profile is not None and sector_profile.life_cycle == "climax":
            style_tag = "reseal"
        elif relative_strength >= 0.01 and selection_score >= 72:
            style_tag = "momentum"
        elif relative_strength <= -0.005 or market_profile.regime == "defensive":
            style_tag = "defensive"
        else:
            style_tag = "mixed"

        board_success_rate = clamp(
            0.38
            + normalized_score * 0.34
            + max(relative_strength, 0.0) * 3.5
            + (0.08 if sector_rank == 1 else 0.03 if sector_rank <= 2 else 0.0),
            0.12,
            0.95,
        )
        bomb_rate = clamp(
            0.42
            - normalized_score * 0.18
            - max(relative_strength, 0.0) * 2.0
            + (0.08 if sector_profile is not None and sector_profile.life_cycle == "retreat" else 0.0),
            0.05,
            0.75,
        )
        next_day_premium = clamp(relative_strength * 1.2 + (normalized_score - 0.4) * 0.035, -0.03, 0.08)
        reseal_rate = clamp(
            0.16 + (0.18 if style_tag in {"leader", "reseal"} else 0.05) + min(sector_event_count, 3) * 0.04,
            0.05,
            0.9,
        )
        if style_tag == "leader":
            optimal_hold_days = 1 if market_profile.regime == "trend" else 2
        elif style_tag in {"momentum", "reseal"}:
            optimal_hold_days = 2
        elif style_tag == "defensive":
            optimal_hold_days = 3
        else:
            optimal_hold_days = 2
        leader_frequency = clamp(
            0.12
            + (0.42 if sector_rank == 1 else 0.22 if sector_rank <= 2 else 0.0)
            + max(relative_strength, 0.0) * 4.0,
            0.0,
            0.95,
        )
        profiles[symbol] = StockBehaviorProfile(
            symbol=symbol,
            board_success_rate_20d=round(board_success_rate, 4),
            bomb_rate_20d=round(bomb_rate, 4),
            next_day_premium_20d=round(next_day_premium, 4),
            reseal_rate_20d=round(reseal_rate, 4),
            optimal_hold_days=optimal_hold_days,
            style_tag=style_tag,
            avg_sector_rank_30d=round(float(min(sector_rank, sector_count)), 4),
            leader_frequency_30d=round(leader_frequency, 4),
        )
    return profiles


def build_leader_ranks(
    *,
    leader_ranker: Any,
    pack_items: list[dict[str, Any]],
    selected_symbols: list[str],
    sector_map: dict[str, str],
    decisions: list[dict[str, Any]],
    behavior_profiles: dict[str, StockBehaviorProfile],
) -> dict[str, Any]:
    item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
    decision_map = {str(item.get("symbol")): item for item in decisions if item.get("symbol")}
    sector_data: dict[str, dict[str, Any]] = {}
    for symbol in selected_symbols:
        sector_name = sector_map.get(symbol, "")
        peers = sorted(
            [target for target in selected_symbols if sector_map.get(target, "") == sector_name],
            key=lambda target: float((decision_map.get(target, {}) or {}).get("selection_score", 0.0) or 0.0),
            reverse=True,
        )
        market_snapshot = (item_map.get(symbol, {}) or {}).get("market_snapshot", {}) or {}
        volume = float(market_snapshot.get("volume", 0.0) or 0.0)
        sector_data[symbol] = {
            "sector": sector_name,
            "zt_order_rank": peers.index(symbol) + 1 if symbol in peers else 99,
            "seal_ratio": behavior_profiles[symbol].board_success_rate_20d if symbol in behavior_profiles else 0.5,
            "diffusion_count": len(peers),
            "turnover_rate": min(volume / 200_000.0, 10.0),
        }
    return leader_ranker.to_map(selected_symbols, sector_data, behavior_profiles)


def playbook_ctx_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return dict(item or {})


def resolve_playbook_match_payload(ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ctx:
        return None
    payload = ctx.get("playbook_match_score")
    if payload is None:
        return None
    if isinstance(payload, dict):
        return {
            "qualified": bool(payload.get("qualified", False)),
            "score": float(payload.get("score", 0.0) or 0.0),
            **payload,
        }
    return {
        "qualified": bool(getattr(payload, "qualified", False)),
        "score": float(getattr(payload, "score", payload) or 0.0),
    }


def top_pick_sort_key(item: dict[str, Any], playbook_map: dict[str, dict[str, Any]]) -> tuple[int, float, float, float, int, float]:
    symbol = str(item.get("symbol") or "")
    ctx = playbook_map.get(symbol, {})
    match_payload = resolve_playbook_match_payload(ctx)
    confidence = float(ctx.get("confidence", 0.0) or 0.0)
    leader_score = float(ctx.get("leader_score", 0.0) or 0.0)
    rank_in_sector = int(ctx.get("rank_in_sector", 99) or 99)
    selection_score = float(item.get("selection_score", 0.0) or 0.0)
    return (
        1 if match_payload is not None else 0,
        float((match_payload or {}).get("score", 0.0) or 0.0),
        confidence,
        leader_score,
        -rank_in_sector,
        selection_score,
    )


def apply_playbook_order(decisions: list[dict[str, Any]], playbook_contexts: list[Any]) -> list[dict[str, Any]]:
    if not decisions or not playbook_contexts:
        return decisions
    playbook_payloads = [playbook_ctx_payload(item) for item in playbook_contexts]
    playbook_map = {str(item.get("symbol") or ""): item for item in playbook_payloads if item.get("symbol")}
    enriched: list[dict[str, Any]] = []
    for item in decisions:
        updated = dict(item)
        symbol = str(updated.get("symbol") or "")
        ctx = playbook_map.get(symbol)
        if ctx:
            updated["assigned_playbook"] = ctx.get("playbook", updated.get("assigned_playbook"))
            updated["playbook_context"] = ctx
            match_payload = resolve_playbook_match_payload(ctx)
            if match_payload is not None:
                updated["playbook_match_score"] = match_payload
        enriched.append(updated)
    ordered = sorted(enriched, key=lambda item: top_pick_sort_key(item, playbook_map), reverse=True)
    for index, item in enumerate(ordered, start=1):
        item["rank"] = index
    return ordered


def _style_tag_priority(style_tag: str) -> float:
    return {
        "leader": 1.2,
        "reseal": 0.9,
        "momentum": 0.6,
        "mixed": 0.2,
        "defensive": -0.5,
    }.get(str(style_tag or "").strip(), 0.0)


def _resolve_sector_strength_map(market_profile_payload: dict[str, Any]) -> dict[str, float]:
    strength_map: dict[str, float] = {}
    for item in list(market_profile_payload.get("sector_profiles") or []):
        if not isinstance(item, dict):
            continue
        sector_name = str(item.get("sector_name") or "").strip()
        if not sector_name:
            continue
        strength_map[sector_name] = float(item.get("strength_score", 0.0) or 0.0)
    return strength_map


def apply_market_alignment_order(
    decisions: list[dict[str, Any]],
    *,
    pack_items: list[dict[str, Any]],
    playbook_contexts: list[Any],
    behavior_profiles: dict[str, StockBehaviorProfile],
    sector_map: dict[str, str],
    market_profile_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if not decisions:
        return decisions
    pack_map = {str(item.get("symbol") or ""): dict(item) for item in pack_items if isinstance(item, dict) and item.get("symbol")}
    playbook_payloads = [playbook_ctx_payload(item) for item in playbook_contexts]
    playbook_map = {str(item.get("symbol") or ""): item for item in playbook_payloads if item.get("symbol")}
    hot_sectors = {str(item).strip() for item in list(market_profile_payload.get("hot_sectors") or []) if str(item).strip()}
    sector_strength_map = _resolve_sector_strength_map(market_profile_payload)
    aligned: list[dict[str, Any]] = []
    for item in decisions:
        updated = dict(item)
        symbol = str(updated.get("symbol") or "").strip()
        sector_name = str(sector_map.get(symbol) or updated.get("resolved_sector") or "").strip()
        behavior_profile = behavior_profiles.get(symbol)
        playbook_context = playbook_map.get(symbol, {})
        symbol_context = dict((pack_map.get(symbol) or {}).get("symbol_context") or {})
        market_relative = dict(symbol_context.get("market_relative") or {})
        sector_relative = dict(symbol_context.get("sector_relative") or {})
        relative_strength = float(market_relative.get("relative_strength_pct", 0.0) or 0.0)
        sector_event_count = int(sector_relative.get("sector_event_count", 0) or 0)
        board_success_rate = float(getattr(behavior_profile, "board_success_rate_20d", 0.0) if behavior_profile is not None else 0.0)
        leader_frequency = float(getattr(behavior_profile, "leader_frequency_30d", 0.0) if behavior_profile is not None else 0.0)
        style_tag = str(getattr(behavior_profile, "style_tag", "") if behavior_profile is not None else "").strip()
        playbook_confidence = float(playbook_context.get("confidence", 0.0) or 0.0)
        leader_score = float(playbook_context.get("leader_score", 0.0) or 0.0)
        selection_score = float(updated.get("selection_score", 0.0) or 0.0)
        hot_sector_hit = sector_name in hot_sectors
        sector_strength = float(sector_strength_map.get(sector_name, 0.0) or 0.0)
        alignment_score = (
            (2.2 if hot_sector_hit else -0.5)
            + min(sector_strength / 2.5, 1.2)
            + clamp(relative_strength * 24.0, -1.8, 1.8)
            + board_success_rate * 2.0
            + leader_frequency * 1.4
            + _style_tag_priority(style_tag)
            + playbook_confidence * 0.8
            + leader_score * 0.6
            + min(sector_event_count, 5) * 0.2
            + clamp((selection_score - 60.0) / 12.0, -1.0, 1.2)
        )
        cold_penalty = 0.0
        if not hot_sector_hit and relative_strength < -0.01 and board_success_rate < 0.25 and style_tag in {"defensive", "mixed", ""}:
            cold_penalty -= 2.2
        if selection_score < 58.0 and sector_event_count <= 0 and leader_frequency < 0.1:
            cold_penalty -= 1.0
        market_alignment_score = round(alignment_score + cold_penalty, 4)
        updated["resolved_sector"] = sector_name or updated.get("resolved_sector")
        updated["market_alignment"] = {
            "score": market_alignment_score,
            "hot_sector_hit": hot_sector_hit,
            "sector_strength": round(sector_strength, 4),
            "relative_strength_pct": round(relative_strength, 4),
            "sector_event_count": sector_event_count,
            "board_success_rate_20d": round(board_success_rate, 4),
            "leader_frequency_30d": round(leader_frequency, 4),
            "style_tag": style_tag or "unknown",
            "playbook_confidence": round(playbook_confidence, 4),
        }
        summary = str(updated.get("summary") or "").strip()
        if hot_sector_hit:
            summary = f"{summary}，命中主线={sector_name}" if summary else f"命中主线={sector_name}"
        elif market_alignment_score <= 0.2:
            summary = f"{summary}，主线贴合度偏弱" if summary else "主线贴合度偏弱"
        updated["summary"] = summary
        if market_alignment_score <= 0.2:
            updated["action"] = "HOLD"
        elif market_alignment_score >= 2.2:
            updated["action"] = "BUY"
        aligned.append(updated)

    ordered = sorted(
        aligned,
        key=lambda item: (
            float(((item.get("market_alignment") or {}).get("score") or 0.0)),
            float(((item.get("playbook_match_score") or {}).get("score") or 0.0)),
            float(item.get("selection_score", 0.0) or 0.0),
        ),
        reverse=True,
    )
    for index, item in enumerate(ordered, start=1):
        item["rank"] = index
    return ordered
