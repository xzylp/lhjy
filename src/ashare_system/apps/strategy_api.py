"""策略管理 API"""

from __future__ import annotations

from fastapi import APIRouter

from ..data.serving import ServingStore
from ..settings import AppSettings
from ..strategy.registry import StrategyRegistry
from ..strategy.screener import StockScreener


def _infer_hot_sectors(items: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    latest_titles: dict[str, list[str]] = {}
    for item in items:
        symbol_context = item.get("symbol_context") or {}
        sector_relative = symbol_context.get("sector_relative") or {}
        tags = sector_relative.get("sector_tags") or []
        titles = sector_relative.get("sector_latest_titles") or []
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1
            latest_titles.setdefault(tag, list(titles[:3]))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [
        {
            "sector": sector,
            "candidate_count": count,
            "latest_titles": latest_titles.get(sector, []),
        }
        for sector, count in ranked[:8]
    ]


def _sector_profile_hot_sectors(items: list[dict]) -> list[dict]:
    return [
        {
            "sector": item.get("sector_name"),
            "candidate_count": item.get("zt_count", 0),
            "latest_titles": [],
            "life_cycle": item.get("life_cycle"),
            "strength_score": item.get("strength_score"),
        }
        for item in items
        if item.get("sector_name")
    ]


def _strategy_candidate_item(item: dict, playbook_map: dict[str, dict]) -> dict:
    symbol_context = item.get("symbol_context") or {}
    sector_relative = symbol_context.get("sector_relative") or {}
    market_relative = symbol_context.get("market_relative") or {}
    playbook_context = playbook_map.get(item.get("symbol")) or item.get("playbook_context") or {}
    behavior_profile = (
        item.get("behavior_profile")
        or symbol_context.get("behavior_profile")
        or playbook_context.get("behavior_profile")
        or {}
    )
    return {
        "symbol": item.get("symbol"),
        "name": item.get("name", ""),
        "rank": item.get("rank"),
        "selection_score": item.get("selection_score"),
        "final_status": item.get("final_status"),
        "risk_gate": item.get("risk_gate"),
        "audit_gate": item.get("audit_gate"),
        "reason": item.get("reason", ""),
        "sector_tags": sector_relative.get("sector_tags", []),
        "sector_event_count": sector_relative.get("sector_event_count", 0),
        "market_posture": market_relative.get("posture"),
        "relative_strength_pct": market_relative.get("relative_strength_pct"),
        "event_count": (item.get("event_context") or {}).get("event_count", 0),
        "resolved_sector": item.get("resolved_sector") or playbook_context.get("sector"),
        "playbook": playbook_context.get("playbook"),
        "playbook_confidence": playbook_context.get("confidence"),
        "playbook_entry_window": playbook_context.get("entry_window"),
        "rank_in_sector": playbook_context.get("rank_in_sector"),
        "leader_score": playbook_context.get("leader_score"),
        "style_tag": behavior_profile.get("style_tag") or playbook_context.get("style_tag"),
        "optimal_hold_days": behavior_profile.get("optimal_hold_days"),
        "leader_frequency_30d": behavior_profile.get("leader_frequency_30d"),
        "avg_sector_rank_30d": behavior_profile.get("avg_sector_rank_30d"),
    }


def build_router(settings: AppSettings, registry: StrategyRegistry, screener: StockScreener) -> APIRouter:
    router = APIRouter(prefix="/strategy", tags=["strategy"])
    serving_store = ServingStore(settings.storage_root)

    def _strategy_context_payload() -> dict:
        market_context = serving_store.get_latest_market_context() or {}
        runtime_context = serving_store.get_latest_runtime_context() or {}
        dossier_pack = serving_store.get_latest_dossier_pack() or {}
        items = dossier_pack.get("items", [])
        runtime_market_profile = runtime_context.get("market_profile") or {}
        sector_profiles = (
            runtime_context.get("sector_profiles")
            or market_context.get("sector_profiles")
            or dossier_pack.get("sector_profiles")
            or []
        )
        hot_sectors = _sector_profile_hot_sectors(sector_profiles) or _infer_hot_sectors(items)
        playbook_contexts = runtime_context.get("playbook_contexts") or dossier_pack.get("playbook_contexts") or []
        behavior_profiles = runtime_context.get("behavior_profiles") or dossier_pack.get("behavior_profiles") or []
        playbook_map = {item["symbol"]: item for item in playbook_contexts if item.get("symbol")}
        active_strategies = [{"name": s.name, "weight": s.weight} for s in registry.list_active()]
        regime_value = runtime_market_profile.get("regime") or market_context.get("regime")
        allowed_playbooks = runtime_market_profile.get("allowed_playbooks") or market_context.get("allowed_playbooks") or []
        gaps: list[str] = []
        if not (runtime_market_profile or regime_value):
            gaps.append("market_regime_not_persisted")
        if not playbook_contexts:
            gaps.append("playbook_assignment_not_persisted")
        if not hot_sectors:
            gaps.append("sector_heat_not_available")
        generated_at = (
            dossier_pack.get("generated_at")
            or runtime_context.get("generated_at")
            or market_context.get("generated_at")
        )
        trade_date = (
            dossier_pack.get("trade_date")
            or runtime_context.get("trade_date")
            or market_context.get("trade_date")
        )
        return {
            "available": bool(dossier_pack or runtime_context or market_context),
            "trade_date": trade_date,
            "generated_at": generated_at,
            "regime": {
                "available": bool(regime_value),
                "value": regime_value or "unknown",
                "source": (
                    runtime_market_profile.get("source")
                    or ("market_context" if regime_value else "unavailable")
                ),
            },
            "allowed_playbooks": allowed_playbooks,
            "active_strategies": active_strategies,
            "hot_sectors": hot_sectors,
            "sector_profiles": sector_profiles,
            "playbook_contexts": playbook_contexts,
            "behavior_profiles": behavior_profiles,
            "candidate_count": len(items),
            "candidates": [_strategy_candidate_item(item, playbook_map) for item in items[:20]],
            "runtime_context_ref": (
                {
                    "job_id": runtime_context.get("job_id"),
                    "decision_count": runtime_context.get("decision_count", 0),
                    "buy_count": runtime_context.get("buy_count", 0),
                    "hold_count": runtime_context.get("hold_count", 0),
                    "playbook_count": runtime_context.get("playbook_count", 0),
                    "regime": runtime_market_profile.get("regime"),
                    "latest_endpoint": "/runtime/context/latest",
                }
                if runtime_context
                else None
            ),
            "dossier_ref": (
                {
                    "pack_id": dossier_pack.get("pack_id"),
                    "symbol_count": dossier_pack.get("symbol_count", len(items)),
                    "latest_endpoint": "/runtime/dossiers/latest",
                }
                if dossier_pack
                else None
            ),
            "gaps": gaps,
        }

    @router.get("/strategies")
    async def list_strategies():
        return {"strategies": [{"name": s.name, "weight": s.weight} for s in registry.list_active()]}

    @router.post("/screen")
    async def screen(candidates: list[str], top_n: int = 10):
        result = screener.run(candidates, top_n=top_n)
        return {"passed": result.passed, "rejected": result.rejected, "stats": result.stage_stats}

    @router.get("/context/latest")
    async def get_latest_strategy_context():
        return _strategy_context_payload()

    @router.get("/candidates/latest")
    async def get_latest_strategy_candidates():
        payload = _strategy_context_payload()
        return {
            "available": payload["available"],
            "trade_date": payload["trade_date"],
            "generated_at": payload["generated_at"],
            "candidate_count": payload["candidate_count"],
            "items": payload["candidates"],
            "hot_sectors": payload["hot_sectors"],
            "sector_profiles": payload["sector_profiles"],
            "playbook_contexts": payload["playbook_contexts"],
            "behavior_profiles": payload["behavior_profiles"],
            "regime": payload["regime"],
            "allowed_playbooks": payload["allowed_playbooks"],
            "gaps": payload["gaps"],
        }

    return router
