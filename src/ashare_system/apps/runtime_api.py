"""运行时 API - OpenClaw 盘中调用入口"""

from __future__ import annotations

from collections import defaultdict
import json
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..contracts import MarketProfile, SectorProfile, StockBehaviorProfile
from ..data.archive import DataArchiveStore
from ..data.serving import ServingStore
from ..discussion.candidate_case import CandidateCaseService
from ..governance.param_service import ParameterService
from ..infra.audit_store import AuditStore, StateStore
from ..infra.adapters import ExecutionAdapter
from ..infra.market_adapter import MarketDataAdapter
from ..monitor.stock_pool import StockPoolManager
from ..monitor.persistence import MonitorStateService
from ..notify.dispatcher import MessageDispatcher
from ..notify.monitor_changes import MonitorChangeNotifier
from ..precompute import DossierPrecomputeService
from ..runtime_config import RuntimeConfig, RuntimeConfigManager
from ..sentiment.calculator import SentimentCalculator
from ..settings import AppSettings
from ..strategy.registry import StrategyRegistry
from ..strategy.leader_rank import LeaderRanker
from ..strategy.router import StrategyRouter
from ..strategy.screener import StockScreener


class RuntimeJobRequest(BaseModel):
    """运行时选股任务请求"""

    symbols: list[str] = Field(default_factory=list)
    universe_scope: str = "main-board"
    max_candidates: int | None = Field(default=None, ge=1, le=50)
    auto_trade: bool = False
    account_id: str = "8890130545"


def build_router(
    settings: AppSettings,
    market_adapter: MarketDataAdapter,
    execution_adapter: ExecutionAdapter,
    strategy_registry: StrategyRegistry,
    screener: StockScreener,
    pool_mgr: StockPoolManager,
    audit_store: AuditStore,
    runtime_state_store: StateStore,
    config_mgr: RuntimeConfigManager | None = None,
    parameter_service: ParameterService | None = None,
    candidate_case_service: CandidateCaseService | None = None,
    monitor_state_service: MonitorStateService | None = None,
    message_dispatcher: MessageDispatcher | None = None,
    dossier_precompute_service: DossierPrecomputeService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/runtime", tags=["runtime"])
    reports_dir = settings.logs_dir / "reports"
    latest_report = reports_dir / "runtime_latest.json"
    archive_store = DataArchiveStore(settings.storage_root)
    serving_store = ServingStore(settings.storage_root)
    sentiment_calculator = SentimentCalculator()
    strategy_router = StrategyRouter()
    leader_ranker = LeaderRanker()

    def _pick_universe(request: RuntimeJobRequest) -> list[str]:
        if request.symbols:
            return request.symbols
        if request.universe_scope == "a-share":
            return market_adapter.get_a_share_universe()
        return market_adapter.get_main_board_universe()

    def _runtime_config() -> RuntimeConfig:
        return config_mgr.get() if config_mgr else RuntimeConfig()

    monitor_change_notifier = (
        MonitorChangeNotifier(monitor_state_service, message_dispatcher)
        if monitor_state_service
        else None
    )

    def _score_snapshot(symbol: str, last_price: float, pre_close: float, volume: float, rank: int) -> dict:
        base_price = pre_close or last_price or 1.0
        momentum = ((last_price - base_price) / base_price) * 100.0
        liquidity_score = min(volume / 800_000.0, 1.0) * 18.0
        price_bias = min(max(last_price / max(base_price, 1.0) - 1.0, -0.08), 0.08) * 180.0
        selection_score = round(max(45.0, min(96.0, 62.0 + liquidity_score + price_bias - rank * 1.8)), 2)
        action = "BUY" if selection_score >= 72.0 else "HOLD"
        return {
            "symbol": symbol,
            "rank": rank,
            "selection_score": selection_score,
            "action": action,
            "score_breakdown": {
                "momentum_pct": round(momentum, 2),
                "liquidity_score": round(liquidity_score, 2),
                "price_bias_score": round(price_bias, 2),
            },
            "summary": f"{symbol} 评分 {selection_score}，量价动能 {'偏强' if action == 'BUY' else '一般'}",
        }

    def _write_report(payload: dict) -> None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        latest_report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _safe_mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)

    def _build_market_profile(decisions: list[dict], snapshots: list) -> tuple[MarketProfile, dict]:
        profile = sentiment_calculator.calc_from_market_data(market_adapter)
        has_valid_reference = any((getattr(item, "pre_close", 0.0) or 0.0) > 0 for item in snapshots)
        if profile is not None and (has_valid_reference or profile.allowed_playbooks):
            payload = profile.model_dump()
            payload["source"] = "sentiment_calculator"
            payload["inferred"] = False
            return profile, payload

        top_score = max((float(item.get("selection_score", 0.0) or 0.0) for item in decisions), default=0.0)
        avg_score = _safe_mean([float(item.get("selection_score", 0.0) or 0.0) for item in decisions])
        if top_score >= 72.0 or avg_score >= 70.0:
            profile = MarketProfile(
                sentiment_phase="主升",
                sentiment_score=max(top_score, 78.0),
                position_ceiling=0.8,
                regime="trend",
                regime_score=0.9,
                allowed_playbooks=["leader_chase", "divergence_reseal"],
                market_risk_flags=["runtime_profile_fallback"],
            )
        elif decisions:
            profile = MarketProfile(
                sentiment_phase="回暖",
                sentiment_score=max(avg_score, 62.0),
                position_ceiling=0.6,
                regime="rotation",
                regime_score=0.65,
                allowed_playbooks=["sector_reflow_first_board", "divergence_reseal"],
                market_risk_flags=["runtime_profile_fallback"],
            )
        else:
            profile = MarketProfile(
                sentiment_phase="冰点",
                sentiment_score=30.0,
                position_ceiling=0.2,
                regime="defensive",
                regime_score=0.35,
                allowed_playbooks=[],
                market_risk_flags=["runtime_profile_fallback", "no_runtime_candidates"],
            )
        payload = profile.model_dump()
        payload["source"] = "runtime_heuristic_fallback"
        payload["inferred"] = True
        return profile, payload

    def _resolve_symbol_sector_map(pack_items: list[dict], selected_symbols: list[str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        unresolved = set(selected_symbols)
        item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}

        for symbol in list(unresolved):
            item = item_map.get(symbol) or {}
            tags = (((item.get("symbol_context") or {}).get("sector_relative") or {}).get("sector_tags") or [])
            if tags:
                resolved[symbol] = str(tags[0])
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
            resolved[symbol] = "候选池聚类"
        return resolved

    def _infer_sector_profiles(
        pack_items: list[dict],
        selected_symbols: list[str],
        sector_map: dict[str, str],
        market_profile: MarketProfile,
    ) -> list[SectorProfile]:
        item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
        sector_items: dict[str, list[dict]] = defaultdict(list)
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
            avg_relative_strength = _safe_mean(relative_strengths)
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

    def _infer_behavior_profiles(
        pack_items: list[dict],
        selected_symbols: list[str],
        sector_map: dict[str, str],
        sector_profiles: list[SectorProfile],
        decisions: list[dict],
        market_profile: MarketProfile,
    ) -> dict[str, StockBehaviorProfile]:
        item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
        decision_map = {str(item.get("symbol")): item for item in decisions if item.get("symbol")}
        sector_profile_map = {item.sector_name: item for item in sector_profiles}
        sector_items: dict[str, list[dict]] = defaultdict(list)
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
            normalized_score = _clamp((selection_score - 60.0) / 30.0, 0.0, 1.0)
            sector_rank = sector_rank_map.get(symbol, 99)
            sector_count = max(sector_count_map.get(sector_name, 1), 1)
            sector_event_count = int(
                (((symbol_context.get("sector_relative") or {}).get("sector_event_count")) or 0)
            )
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

            board_success_rate = _clamp(
                0.38
                + normalized_score * 0.34
                + max(relative_strength, 0.0) * 3.5
                + (0.08 if sector_rank == 1 else 0.03 if sector_rank <= 2 else 0.0),
                0.12,
                0.95,
            )
            bomb_rate = _clamp(
                0.42
                - normalized_score * 0.18
                - max(relative_strength, 0.0) * 2.0
                + (0.08 if sector_profile is not None and sector_profile.life_cycle == "retreat" else 0.0),
                0.05,
                0.75,
            )
            next_day_premium = _clamp(relative_strength * 1.2 + (normalized_score - 0.4) * 0.035, -0.03, 0.08)
            reseal_rate = _clamp(
                0.16
                + (0.18 if style_tag in {"leader", "reseal"} else 0.05)
                + min(sector_event_count, 3) * 0.04,
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
            leader_frequency = _clamp(
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

    def _build_leader_ranks(
        pack_items: list[dict],
        selected_symbols: list[str],
        sector_map: dict[str, str],
        decisions: list[dict],
        behavior_profiles: dict[str, StockBehaviorProfile],
    ) -> dict:
        item_map = {str(item.get("symbol")): item for item in pack_items if item.get("symbol")}
        decision_map = {str(item.get("symbol")): item for item in decisions if item.get("symbol")}
        sector_data: dict[str, dict] = {}
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

    def _enrich_runtime_dossier(
        pack: dict,
        market_profile_payload: dict,
        sector_profiles: list[SectorProfile],
        playbook_contexts: list,
        sector_map: dict[str, str],
        behavior_profiles: dict[str, StockBehaviorProfile] | None = None,
    ) -> dict:
        sector_payloads = [item.model_dump() for item in sector_profiles]
        playbook_payloads = [item.model_dump() for item in playbook_contexts]
        playbook_map = {item["symbol"]: item for item in playbook_payloads}
        behavior_payloads = {
            symbol: item.model_dump()
            for symbol, item in (behavior_profiles or {}).items()
        }
        enriched_market_context = dict(pack.get("market_context") or {})
        enriched_market_context.update(
            {
                "trade_date": pack.get("trade_date"),
                "regime": market_profile_payload.get("regime"),
                "regime_score": market_profile_payload.get("regime_score"),
                "sentiment_phase": market_profile_payload.get("sentiment_phase"),
                "sentiment_score": market_profile_payload.get("sentiment_score"),
                "position_ceiling": market_profile_payload.get("position_ceiling"),
                "allowed_playbooks": market_profile_payload.get("allowed_playbooks", []),
                "market_risk_flags": market_profile_payload.get("market_risk_flags", []),
                "hot_sectors": market_profile_payload.get("hot_sectors", []),
                "market_profile_source": market_profile_payload.get("source"),
                "sector_profiles": sector_payloads,
                "playbook_contexts": playbook_payloads,
            }
        )

        items = []
        for item in pack.get("items", []):
            symbol = item.get("symbol")
            enriched_item = dict(item)
            symbol_context = dict(enriched_item.get("symbol_context") or {})
            behavior_profile = behavior_payloads.get(symbol)
            if symbol:
                enriched_item["resolved_sector"] = sector_map.get(symbol, "")
                if symbol in playbook_map:
                    playbook_context = dict(playbook_map[symbol])
                    if behavior_profile:
                        playbook_context["behavior_profile"] = behavior_profile
                    enriched_item["playbook_context"] = playbook_context
                    enriched_item["assigned_playbook"] = playbook_map[symbol]["playbook"]
                if behavior_profile:
                    symbol_context["behavior_profile"] = behavior_profile
                    enriched_item["behavior_profile"] = behavior_profile
            if symbol_context:
                enriched_item["symbol_context"] = symbol_context
            enriched_item["market_context"] = enriched_market_context
            items.append(enriched_item)

        return {
            **pack,
            "market_context": enriched_market_context,
            "sector_profiles": sector_payloads,
            "playbook_contexts": playbook_payloads,
            "behavior_profiles": list(behavior_payloads.values()),
            "items": items,
        }

    def _persist_runtime_context(payload: dict, snapshot: dict, strategy_context: dict | None = None) -> dict:
        generated_at = payload.get("generated_at") or snapshot.get("generated_at") or datetime.now().isoformat()
        trade_date = datetime.fromisoformat(generated_at).date().isoformat()
        pool = pool_mgr.get()
        strategy_context = strategy_context or {}
        playbook_contexts = strategy_context.get("playbook_contexts", [])
        behavior_profiles = strategy_context.get("behavior_profiles", [])
        runtime_context = {
            "available": True,
            "resource": "runtime_context",
            "trade_date": trade_date,
            "generated_at": generated_at,
            "job_id": snapshot.get("job_id"),
            "job_type": snapshot.get("job_type"),
            "run_mode": settings.run_mode,
            "market_mode": getattr(market_adapter, "mode", settings.market_mode),
            "execution_mode": getattr(execution_adapter, "mode", settings.execution_mode),
            "account_id": snapshot.get("account_id"),
            "auto_trade": snapshot.get("auto_trade"),
            "universe_scope": snapshot.get("universe_scope"),
            "decision_count": snapshot.get("decision_count", 0),
            "buy_count": snapshot.get("buy_count", 0),
            "hold_count": snapshot.get("hold_count", 0),
            "selected_symbols": snapshot.get("selected_symbols", []),
            "top_picks": snapshot.get("top_picks", []),
            "summary": payload.get("summary", {}),
            "execution": payload.get("execution"),
            "report_path": snapshot.get("report_path"),
            "market_profile": strategy_context.get("market_profile") or payload.get("market_profile"),
            "sector_profiles": strategy_context.get("sector_profiles", []),
            "playbook_contexts": playbook_contexts,
            "playbook_count": len(playbook_contexts),
            "behavior_profiles": behavior_profiles,
            "hot_sectors": strategy_context.get("hot_sectors", []),
            "pool": {
                "date": (pool.date if pool else trade_date),
                "source": (pool.source if pool else "runtime_context"),
                "symbols": (pool.symbols if pool else snapshot.get("selected_symbols", [])),
                "names": (pool.names if pool else {}),
                "scores": (pool.scores if pool else {}),
            },
        }
        archive_store.persist_runtime_context(trade_date, runtime_context)
        runtime_state_store.set("latest_runtime_context", runtime_context)
        return runtime_context

    def _persist_runtime_report(payload: dict) -> dict:
        summary = payload.get("summary", {})
        top_picks = payload.get("top_picks", [])
        snapshot = {
            "job_id": payload.get("job_id"),
            "job_type": payload.get("job_type"),
            "generated_at": payload.get("generated_at"),
            "account_id": payload.get("account_id"),
            "auto_trade": payload.get("auto_trade"),
            "universe_scope": payload.get("universe_scope"),
            "selected_symbols": [item["symbol"] for item in top_picks],
            "decision_count": len(top_picks),
            "buy_count": summary.get("buy_count", 0),
            "hold_count": summary.get("hold_count", 0),
            "top_picks": top_picks,
            "report_path": str(latest_report),
        }
        runtime_state_store.set("latest_runtime_report", snapshot)
        history = runtime_state_store.get("runtime_jobs", [])
        history.append(snapshot)
        runtime_state_store.set("runtime_jobs", history[-30:])
        strategy_context = {
            "market_profile": payload.get("market_profile"),
            "sector_profiles": payload.get("sector_profiles", []),
            "playbook_contexts": payload.get("playbook_contexts", []),
            "behavior_profiles": payload.get("behavior_profiles", []),
            "hot_sectors": payload.get("hot_sectors", []),
        }

        if candidate_case_service and top_picks:
            focus_pool_capacity = int(parameter_service.get_param_value("focus_pool_capacity")) if parameter_service else 10
            execution_pool_capacity = int(parameter_service.get_param_value("execution_pool_capacity")) if parameter_service else 3
            synced_cases = candidate_case_service.sync_from_runtime_report(
                payload,
                focus_pool_capacity=focus_pool_capacity,
                execution_pool_capacity=execution_pool_capacity,
            )
            if monitor_state_service and synced_cases:
                trade_date = synced_cases[0].trade_date
                monitor_state_service.save_pool_snapshot(
                    trade_date=trade_date,
                    pool_snapshot=candidate_case_service.build_pool_snapshot(trade_date),
                    source="runtime_pipeline",
                )
                if monitor_change_notifier:
                    monitor_change_notifier.dispatch_latest()
                if dossier_precompute_service:
                    pack = dossier_precompute_service.precompute(
                        trade_date=trade_date,
                        source="candidate_pool",
                    )
                    market_profile_payload = payload.get("market_profile") or {}
                    market_profile = (
                        MarketProfile.model_validate(market_profile_payload)
                        if market_profile_payload
                        else None
                    )
                    if pack and market_profile:
                        selected_symbols = [item["symbol"] for item in top_picks if item.get("symbol")]
                        sector_map = _resolve_symbol_sector_map(pack.get("items", []), selected_symbols)
                        sector_profiles = _infer_sector_profiles(
                            pack.get("items", []),
                            selected_symbols,
                            sector_map,
                            market_profile,
                        )
                        market_profile = market_profile.model_copy(
                            update={
                                "hot_sectors": [item.sector_name for item in sector_profiles[:8]],
                                "sector_profiles": sector_profiles,
                            }
                        )
                        behavior_profiles = _infer_behavior_profiles(
                            pack.get("items", []),
                            selected_symbols,
                            sector_map,
                            sector_profiles,
                            top_picks,
                            market_profile,
                        )
                        leader_ranks = _build_leader_ranks(
                            pack.get("items", []),
                            selected_symbols,
                            sector_map,
                            top_picks,
                            behavior_profiles,
                        )
                        playbook_contexts = strategy_router.route(
                            profile=market_profile,
                            sector_profiles=sector_profiles,
                            candidates=selected_symbols,
                            stock_info={symbol: {"sector": sector_map.get(symbol, "")} for symbol in selected_symbols},
                            behavior_profiles=behavior_profiles,
                            leader_ranks=leader_ranks,
                        )
                        market_profile_payload = {
                            **market_profile.model_dump(),
                            "source": market_profile_payload.get("source", "sentiment_calculator"),
                            "inferred": market_profile_payload.get("inferred", False),
                        }
                        enriched_pack = _enrich_runtime_dossier(
                            pack,
                            market_profile_payload,
                            sector_profiles,
                            playbook_contexts,
                            sector_map,
                            behavior_profiles,
                        )
                        archive_store.persist_market_context(trade_date, enriched_pack["market_context"])
                        archive_store.persist_dossier_pack(enriched_pack)
                        payload["market_profile"] = market_profile_payload
                        payload["sector_profiles"] = [item.model_dump() for item in sector_profiles]
                        payload["playbook_contexts"] = [item.model_dump() for item in playbook_contexts]
                        payload["behavior_profiles"] = [item.model_dump() for item in behavior_profiles.values()]
                        payload["hot_sectors"] = market_profile_payload.get("hot_sectors", [])
                        strategy_context = {
                            "market_profile": payload["market_profile"],
                            "sector_profiles": payload["sector_profiles"],
                            "playbook_contexts": payload["playbook_contexts"],
                            "behavior_profiles": payload["behavior_profiles"],
                            "hot_sectors": payload["hot_sectors"],
                        }
                monitor_state_service.mark_poll_if_due("candidate", trigger="runtime_pipeline", force=True)

        _persist_runtime_context(payload, snapshot, strategy_context)
        return payload

    @router.get("/health")
    async def runtime_health():
        latest_runtime = runtime_state_store.get("latest_runtime_report", {})
        return {
            "status": "ok",
            "service": settings.app_name,
            "run_mode": settings.run_mode,
            "market_mode": getattr(market_adapter, "mode", settings.market_mode),
            "execution_mode": getattr(execution_adapter, "mode", settings.execution_mode),
            "strategy_count": len(strategy_registry.list_active()),
            "latest_report_available": latest_report.exists(),
            "last_runtime_job_at": latest_runtime.get("generated_at"),
        }

    @router.post("/jobs/pipeline")
    async def run_pipeline(request: RuntimeJobRequest):
        runtime_config = _runtime_config()
        candidate_limit = request.max_candidates or runtime_config.screener_pool_size
        universe = _pick_universe(request)
        snapshots = market_adapter.get_snapshots(universe)
        ranked = sorted(
            snapshots,
            key=lambda item: (
                item.volume,
                item.last_price - (item.pre_close or item.last_price),
                item.last_price,
            ),
            reverse=True,
        )

        candidate_symbols = [item.symbol for item in ranked[: max(candidate_limit * 3, candidate_limit)]]
        passed = screener.run(candidate_symbols, runtime_config=runtime_config, top_n=candidate_limit).passed

        decisions = []
        score_map: dict[str, float] = {}
        name_map: dict[str, str] = {}
        snapshot_map = {item.symbol: item for item in ranked}
        final_symbols = passed or candidate_symbols[:candidate_limit]
        for index, symbol in enumerate(final_symbols[:candidate_limit], start=1):
            item = snapshot_map.get(symbol)
            if item is None:
                continue
            decision = _score_snapshot(
                symbol=symbol,
                last_price=item.last_price,
                pre_close=item.pre_close,
                volume=item.volume,
                rank=index,
            )
            decision["name"] = getattr(item, "name", "") or market_adapter.get_symbol_name(symbol)
            score_map[symbol] = float(decision["selection_score"])
            name_map[symbol] = decision["name"]
            decisions.append(decision)

        pool_mgr.update([item["symbol"] for item in decisions], score_map, names=name_map)
        job_id = f"runtime-{uuid4().hex[:10]}"
        report = {
            "job_id": job_id,
            "job_type": "pipeline",
            "generated_at": datetime.now().isoformat(),
            "account_id": request.account_id,
            "auto_trade": request.auto_trade,
            "universe_scope": request.universe_scope,
            "candidates_evaluated": len(universe),
            "top_picks": decisions,
            "summary": {
                "buy_count": sum(1 for item in decisions if item["action"] == "BUY"),
                "hold_count": sum(1 for item in decisions if item["action"] == "HOLD"),
                "reject_count": max(len(candidate_symbols) - len(decisions), 0),
            },
        }
        _, market_profile_payload = _build_market_profile(decisions, ranked)
        report["market_profile"] = market_profile_payload
        _write_report(report)
        report = _persist_runtime_report(report)
        _write_report(report)
        audit_store.append(
            category="runtime",
            message=f"运行时任务完成: {job_id}",
            payload={
                "job_id": job_id,
                "selected_symbols": [item["symbol"] for item in decisions],
                "decision_count": len(decisions),
                "buy_count": report["summary"]["buy_count"],
                "universe_scope": request.universe_scope,
            },
        )
        return report

    @router.post("/jobs/intraday")
    async def run_intraday(request: RuntimeJobRequest):
        intraday_request = request.model_copy(
            update={
                "symbols": request.symbols,
                "universe_scope": "custom",
            }
        )
        return await run_pipeline(intraday_request)

    @router.post("/jobs/autotrade")
    async def run_autotrade(request: RuntimeJobRequest):
        auto_request = request.model_copy(update={"auto_trade": True})
        result = await run_pipeline(auto_request)
        result["execution"] = {
            "status": "blocked",
            "reason": "实测阶段默认关闭自动下单，仅返回候选结果",
        }
        _write_report(result)
        _persist_runtime_report(result)
        audit_store.append(
            category="execution",
            message=f"自动交易请求被阻断: {result['job_id']}",
            payload={"job_id": result["job_id"], "reason": result["execution"]["reason"]},
        )
        return result

    @router.get("/reports/latest")
    async def get_latest_report():
        if latest_report.exists():
            return json.loads(latest_report.read_text(encoding="utf-8"))
        cached = runtime_state_store.get("latest_runtime_report")
        if cached:
            return cached
        pool = pool_mgr.get()
        return {
            "available": False,
            "message": "暂无运行时报告",
            "pool": pool.symbols if pool else [],
        }

    @router.get("/context/latest")
    async def get_latest_runtime_context():
        payload = serving_store.get_latest_runtime_context()
        return payload or {"available": False, "resource": "runtime_context"}

    @router.get("/dossiers/latest")
    async def get_latest_runtime_dossiers():
        dossier_pack = serving_store.get_latest_dossier_pack()
        runtime_context = serving_store.get_latest_runtime_context()
        if not dossier_pack:
            return {"available": False, "resource": "runtime_dossiers"}
        return {
            "available": True,
            "resource": "runtime_dossiers",
            "trade_date": dossier_pack.get("trade_date"),
            "generated_at": dossier_pack.get("generated_at"),
            "pack_id": dossier_pack.get("pack_id"),
            "symbol_count": dossier_pack.get("symbol_count", len(dossier_pack.get("items", []))),
            "behavior_profiles": dossier_pack.get("behavior_profiles", []),
            "runtime_context": (
                {
                    "job_id": runtime_context.get("job_id"),
                    "decision_count": runtime_context.get("decision_count", 0),
                    "selected_symbols": runtime_context.get("selected_symbols", []),
                    "latest_endpoint": "/runtime/context/latest",
                }
                if runtime_context
                else None
            ),
            "items": dossier_pack.get("items", []),
        }

    @router.get("/dossiers/{trade_date}/{symbol}")
    async def get_runtime_dossier(trade_date: str, symbol: str):
        dossier = serving_store.get_dossier(trade_date, symbol)
        if not dossier:
            return {"available": False, "trade_date": trade_date, "symbol": symbol}
        return {"available": True, **dossier}

    return router
