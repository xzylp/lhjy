"""市场状态感知与热点链提取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MarketRegime:
    regime_label: str
    runtime_regime: str
    hot_sector_chain: list[str] = field(default_factory=list)
    sector_strength_rank: list[dict[str, Any]] = field(default_factory=list)
    money_flow_signal: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_payload(self) -> dict[str, Any]:
        return {
            "regime_label": self.regime_label,
            "runtime_regime": self.runtime_regime,
            "hot_sector_chain": list(self.hot_sector_chain),
            "sector_strength_rank": [dict(item) for item in self.sector_strength_rank],
            "money_flow_signal": dict(self.money_flow_signal),
            "confidence": round(float(self.confidence or 0.0), 4),
            "evidence": list(self.evidence),
            "generated_at": self.generated_at,
        }


@dataclass
class RegimeTransition:
    from_regime: str
    to_regime: str
    confidence: float
    transition_signals: list[str] = field(default_factory=list)
    detected_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_payload(self) -> dict[str, Any]:
        return {
            "from_regime": self.from_regime,
            "to_regime": self.to_regime,
            "confidence": round(float(self.confidence or 0.0), 4),
            "transition_signals": list(self.transition_signals),
            "detected_at": self.detected_at,
        }


def detect_market_regime(
    *,
    market_context: dict[str, Any] | None = None,
    runtime_context: dict[str, Any] | None = None,
    monitor_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_context = dict(market_context or {})
    runtime_context = dict(runtime_context or {})
    monitor_context = dict(monitor_context or {})

    sector_strength_rank = _build_sector_strength_rank(market_context, runtime_context, monitor_context)
    hot_sector_chain = [str(item.get("sector") or "") for item in sector_strength_rank if str(item.get("sector") or "").strip()][:10]
    money_flow_signal = _build_money_flow_signal(market_context, runtime_context, monitor_context)

    market_breadth = float(
        market_context.get("market_breadth")
        or runtime_context.get("market_breadth")
        or monitor_context.get("market_breadth")
        or 0.0
    )
    limit_up_count = int(
        market_context.get("limit_up_count")
        or runtime_context.get("limit_up_count")
        or monitor_context.get("limit_up_count")
        or 0
    )
    limit_down_count = int(
        market_context.get("limit_down_count")
        or runtime_context.get("limit_down_count")
        or monitor_context.get("limit_down_count")
        or 0
    )
    top_sector_score = float(sector_strength_rank[0]["strength_score"]) if sector_strength_rank else 0.0
    second_sector_score = float(sector_strength_rank[1]["strength_score"]) if len(sector_strength_rank) > 1 else 0.0

    regime_label = "index_rebound"
    runtime_regime = "rotation"
    confidence = 0.45
    evidence: list[str] = []

    if limit_down_count >= max(limit_up_count + 10, 15):
        regime_label = "panic_sell"
        runtime_regime = "chaos"
        confidence = 0.82
        evidence.append(f"跌停/重挫扩散明显，limit_down_count={limit_down_count}")
    elif market_breadth and market_breadth < 0.38 and money_flow_signal.get("direction") in {"outflow", "defensive"}:
        regime_label = "weak_defense"
        runtime_regime = "defensive"
        confidence = 0.72
        evidence.append(f"市场广度偏弱，breadth={market_breadth:.2f}")
    elif top_sector_score >= 7.5 and second_sector_score > 0 and (top_sector_score - second_sector_score) >= 1.2:
        regime_label = "sector_breakout"
        runtime_regime = "trend"
        confidence = 0.78
        evidence.append(f"单一主线显著领先，top_sector_score={top_sector_score:.2f}")
    elif len(hot_sector_chain) >= 3 and top_sector_score >= 6.5 and limit_up_count >= 10:
        regime_label = "strong_rotation"
        runtime_regime = "rotation"
        confidence = 0.76
        evidence.append(f"热点扩散明显，hot_sector_count={len(hot_sector_chain)} limit_up_count={limit_up_count}")
    elif market_breadth >= 0.52 or money_flow_signal.get("direction") == "inflow":
        regime_label = "index_rebound"
        runtime_regime = "trend"
        confidence = 0.64
        evidence.append(f"市场回暖，breadth={market_breadth:.2f}")
    else:
        evidence.append("缺少足够强的单边证据，按中性轮动处理")

    regime = MarketRegime(
        regime_label=regime_label,
        runtime_regime=runtime_regime,
        hot_sector_chain=hot_sector_chain,
        sector_strength_rank=sector_strength_rank,
        money_flow_signal=money_flow_signal,
        confidence=confidence,
        evidence=evidence,
    )
    return regime.to_payload()


def detect_regime_transition(
    *,
    previous_regime: dict[str, Any] | None,
    current_regime: dict[str, Any] | None,
) -> dict[str, Any]:
    previous = dict(previous_regime or {})
    current = dict(current_regime or {})
    from_regime = str(previous.get("regime_label") or "").strip()
    to_regime = str(current.get("regime_label") or "").strip()
    if not from_regime or not to_regime or from_regime == to_regime:
        return {"available": False, "from_regime": from_regime or None, "to_regime": to_regime or None}
    previous_hot = set(str(item) for item in list(previous.get("hot_sector_chain") or []) if str(item).strip())
    current_hot = set(str(item) for item in list(current.get("hot_sector_chain") or []) if str(item).strip())
    transition_signals: list[str] = []
    if previous_hot != current_hot:
        transition_signals.append("hot_sector_chain_changed")
    if abs(float(current.get("confidence", 0.0) or 0.0) - float(previous.get("confidence", 0.0) or 0.0)) >= 0.1:
        transition_signals.append("regime_confidence_shift")
    prev_money = str((previous.get("money_flow_signal") or {}).get("direction") or "")
    curr_money = str((current.get("money_flow_signal") or {}).get("direction") or "")
    if prev_money != curr_money:
        transition_signals.append("money_flow_direction_changed")
    transition = RegimeTransition(
        from_regime=from_regime,
        to_regime=to_regime,
        confidence=max(float(current.get("confidence", 0.0) or 0.0), float(previous.get("confidence", 0.0) or 0.0)),
        transition_signals=transition_signals or ["regime_label_changed"],
    )
    return {"available": True, **transition.to_payload()}


def _build_sector_strength_rank(
    market_context: dict[str, Any],
    runtime_context: dict[str, Any],
    monitor_context: dict[str, Any],
) -> list[dict[str, Any]]:
    sector_profiles = list(
        market_context.get("sector_profiles")
        or runtime_context.get("sector_profiles")
        or monitor_context.get("sector_profiles")
        or []
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sector_profiles:
        item = dict(raw or {})
        sector = str(item.get("sector_name") or item.get("sector") or "").strip()
        if not sector or sector in seen:
            continue
        seen.add(sector)
        items.append(
            {
                "sector": sector,
                "strength_score": float(item.get("strength_score", item.get("score", 0.0)) or 0.0),
                "turnover_amount": float(item.get("turnover_amount", item.get("amount", 0.0)) or 0.0),
                "limit_up_count": int(item.get("zt_count", item.get("limit_up_count", 0)) or 0),
                "up_ratio": float(item.get("up_ratio", item.get("breadth_score", 0.0)) or 0.0),
            }
        )
    if not items:
        hot_sectors = list(
            market_context.get("hot_sectors")
            or runtime_context.get("hot_sectors")
            or monitor_context.get("hot_sectors")
            or []
        )
        for index, sector in enumerate(hot_sectors):
            sector_name = str(sector or "").strip()
            if not sector_name or sector_name in seen:
                continue
            seen.add(sector_name)
            items.append(
                {
                    "sector": sector_name,
                    "strength_score": max(8.0 - index * 0.7, 3.0),
                    "turnover_amount": max(100000000.0 - index * 10000000.0, 0.0),
                    "limit_up_count": max(5 - index, 0),
                    "up_ratio": max(0.65 - index * 0.05, 0.2),
                }
            )
    items.sort(
        key=lambda item: (
            float(item.get("strength_score", 0.0) or 0.0),
            float(item.get("turnover_amount", 0.0) or 0.0),
            int(item.get("limit_up_count", 0) or 0),
            float(item.get("up_ratio", 0.0) or 0.0),
        ),
        reverse=True,
    )
    return items[:10]


def _build_money_flow_signal(
    market_context: dict[str, Any],
    runtime_context: dict[str, Any],
    monitor_context: dict[str, Any],
) -> dict[str, Any]:
    northbound = float(
        market_context.get("northbound_net_inflow")
        or runtime_context.get("northbound_net_inflow")
        or monitor_context.get("northbound_net_inflow")
        or 0.0
    )
    main_flow = float(
        market_context.get("main_fund_net_inflow")
        or runtime_context.get("main_fund_net_inflow")
        or monitor_context.get("main_fund_net_inflow")
        or 0.0
    )
    top_industries = list(
        market_context.get("top_industries")
        or runtime_context.get("top_industries")
        or monitor_context.get("top_industries")
        or []
    )[:5]
    direction = "neutral"
    if northbound > 0 or main_flow > 0:
        direction = "inflow"
    elif northbound < 0 or main_flow < 0:
        direction = "outflow"
    if direction == "outflow" and top_industries and any("银行" in str(item) or "公用事业" in str(item) for item in top_industries):
        direction = "defensive"
    return {
        "northbound_net_inflow": northbound,
        "main_fund_net_inflow": main_flow,
        "top_industries": [str(item) for item in top_industries],
        "direction": direction,
    }
