"""运行时 API - OpenClaw 盘中调用入口"""

from __future__ import annotations

from collections import defaultdict
import json
import math
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
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
from ..runtime_compose_contracts import RuntimeComposeBriefRequest, RuntimeComposeRequest
from ..runtime_config import RuntimeConfig, RuntimeConfigManager
from ..selection_preferences import match_excluded_theme, normalize_excluded_theme_keywords
from ..sentiment.calculator import SentimentCalculator
from ..settings import AppSettings
from ..strategy.registry import StrategyRegistry
from ..strategy.leader_rank import LeaderRanker
from ..strategy.atomic_repository import (
    StrategyAssetType,
    StrategyRuntimeConsumeMode,
    StrategyRepositoryEntry,
    strategy_atomic_repository,
)
from ..strategy.constraint_pack import apply_constraint_pack, resolve_constraint_pack
from ..strategy.evaluation_ledger import EvaluationLedgerService
from ..strategy.factor_registry import bootstrap_factor_registry, factor_registry
from ..strategy.learned_asset_service import LearnedAssetService
from ..strategy.nightly_sandbox import NightlySandbox
from ..strategy.playbook_registry import bootstrap_playbook_registry, playbook_registry
from ..strategy.strategy_composer import StrategyComposer
from ..strategy.router import StrategyRouter
from ..strategy.screener import StockScreener
from ..learning.attribution import TradeAttributionService
from ..learning.registry_updater import RegistryUpdater
from ..learning.score_state import AgentScoreService


class RuntimeJobRequest(BaseModel):
    """运行时选股任务请求"""

    symbols: list[str] = Field(default_factory=list)
    universe_scope: str = "main-board"
    max_candidates: int | None = Field(default=None, ge=1, le=50)
    auto_trade: bool = False
    account_id: str = "8890130545"
    source: str = "market_universe_scan"


def build_router(
    settings: AppSettings,
    market_adapter: MarketDataAdapter,
    execution_adapter: ExecutionAdapter,
    strategy_registry: StrategyRegistry,
    screener: StockScreener,
    pool_mgr: StockPoolManager,
    audit_store: AuditStore,
    runtime_state_store: StateStore,
    meeting_state_store: StateStore | None = None,
    config_mgr: RuntimeConfigManager | None = None,
    parameter_service: ParameterService | None = None,
    candidate_case_service: CandidateCaseService | None = None,
    monitor_state_service: MonitorStateService | None = None,
    message_dispatcher: MessageDispatcher | None = None,
    dossier_precompute_service: DossierPrecomputeService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/runtime", tags=["runtime"])
    bootstrap_factor_registry()
    bootstrap_playbook_registry()
    reports_dir = settings.logs_dir / "reports"
    latest_report = reports_dir / "runtime_latest.json"
    archive_store = DataArchiveStore(settings.storage_root)
    serving_store = ServingStore(settings.storage_root)
    sentiment_calculator = SentimentCalculator()
    strategy_router = StrategyRouter()
    leader_ranker = LeaderRanker()
    trade_attribution_service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
    agent_score_service = AgentScoreService(settings.storage_root / "agent_score_states.json")
    nightly_sandbox = NightlySandbox(settings.storage_root)
    repo_root = Path(__file__).resolve().parents[3]
    registry_updater = RegistryUpdater(repo_root / "openclaw" / "team_registry.final.json")
    
    learned_asset_service = LearnedAssetService(
        strategy_atomic_repository,
        state_store=runtime_state_store,
        audit_store=audit_store,
        candidate_case_service=candidate_case_service,
    )
    
    evaluation_ledger_service = EvaluationLedgerService(
        state_store=runtime_state_store,
        audit_store=audit_store,
        meeting_state_store=meeting_state_store,
        candidate_case_service=candidate_case_service,
        trade_attribution_service=trade_attribution_service,
        agent_score_service=agent_score_service,
        nightly_sandbox=nightly_sandbox,
        learned_asset_service=learned_asset_service,
        registry_updater=registry_updater,
    )

    strategy_composer = StrategyComposer(
        factor_registry, 
        playbook_registry, 
        evaluation_ledger=evaluation_ledger_service
    )

    def _pick_universe(request: RuntimeJobRequest) -> list[str]:
        if request.symbols:
            return request.symbols
        if request.universe_scope == "a-share":
            return market_adapter.get_a_share_universe()
        return market_adapter.get_main_board_universe()

    def _runtime_config() -> RuntimeConfig:
        return config_mgr.get() if config_mgr else RuntimeConfig()

    def _resolve_excluded_theme_keywords(runtime_config: RuntimeConfig | None = None) -> list[str]:
        if parameter_service:
            raw = parameter_service.get_param_value("excluded_theme_keywords")
        else:
            active_config = runtime_config or _runtime_config()
            raw = getattr(active_config, "excluded_theme_keywords", "")
        return normalize_excluded_theme_keywords(raw)

    def _runtime_config_with_overrides(overrides: dict | None = None) -> RuntimeConfig:
        config = _runtime_config()
        if not overrides:
            return config
        payload = config.model_dump()
        for key, value in overrides.items():
            if key in payload:
                payload[key] = value
        return RuntimeConfig(**payload)

    def _normalize_brief_weights(ids: list[str], provided: dict[str, float]) -> dict[str, float]:
        if not ids:
            return {}
        if not provided:
            equal_weight = round(1.0 / len(ids), 4)
            return {item_id: equal_weight for item_id in ids}
        total = sum(max(float(provided.get(item_id, 0.0) or 0.0), 0.0) for item_id in ids)
        if total <= 0:
            equal_weight = round(1.0 / len(ids), 4)
            return {item_id: equal_weight for item_id in ids}
        return {
            item_id: round(max(float(provided.get(item_id, 0.0) or 0.0), 0.0) / total, 4)
            for item_id in ids
        }

    def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in dict(override or {}).items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = _deep_merge_dict(dict(merged.get(key) or {}), value)
            else:
                merged[key] = value
        return merged

    def _resolve_repository_strategy_entry(
        *,
        asset_type: StrategyAssetType,
        asset_id: str,
        version: str,
    ) -> StrategyRepositoryEntry | None:
        entry = strategy_atomic_repository.get(asset_id, version)
        if entry is not None:
            return entry
        if asset_type == "playbook":
            definition = playbook_registry.get(asset_id, version)
            if definition is None:
                return None
            return StrategyRepositoryEntry(
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
        if asset_type == "factor":
            definition = factor_registry.get(asset_id, version)
            if definition is None:
                return None
            return StrategyRepositoryEntry(
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
        return None

    def _assert_runtime_strategy_access(
        entry: StrategyRepositoryEntry,
        *,
        allow_explicit_only: bool,
    ) -> None:
        policy = entry.runtime_policy()
        mode = str(policy.get("mode") or "").strip()
        if mode == "blocked":
            raise ValueError(f"{entry.type} {entry.id}:{entry.version} 已被仓库阻断: {policy.get('reason', '')}")
        if mode == "governance_only":
            raise ValueError(f"{entry.type} {entry.id}:{entry.version} 当前仅可治理，不可进入 compose 主链")
        if mode == "explicit_only" and not allow_explicit_only:
            raise ValueError(
                f"{entry.type} {entry.id}:{entry.version} 为实验/评审资产，需显式声明后方可进入本次 compose"
            )

    def _build_compose_request_from_brief(brief: RuntimeComposeBriefRequest) -> RuntimeComposeRequest:
        selected_playbooks = list(dict.fromkeys([str(item).strip() for item in brief.playbooks if str(item).strip()]))
        selected_factors = list(dict.fromkeys([str(item).strip() for item in brief.factors if str(item).strip()]))
        playbook_spec_map: dict[str, dict[str, Any]] = {}
        factor_spec_map: dict[str, dict[str, Any]] = {}
        for item in list(brief.playbook_specs or []):
            item_id = str(item.id or "").strip()
            if not item_id:
                continue
            if item_id not in selected_playbooks:
                selected_playbooks.append(item_id)
            playbook_spec_map[item_id] = {
                "version": str(item.version or "").strip(),
                "weight": item.weight,
                "params": dict(item.params or {}),
            }
        for item in list(brief.factor_specs or []):
            item_id = str(item.id or "").strip()
            if not item_id:
                continue
            if item_id not in selected_factors:
                selected_factors.append(item_id)
            factor_spec_map[item_id] = {
                "group": str(item.group or "").strip(),
                "version": str(item.version or "").strip(),
                "weight": item.weight,
                "params": dict(item.params or {}),
            }
        if not str(brief.objective or "").strip() and not str(brief.market_hypothesis or "").strip():
            raise ValueError("compose brief 至少需要提供 objective 或 market_hypothesis")
        if not selected_playbooks and not selected_factors:
            raise ValueError("compose brief 至少需要提供一个 playbook 或 factor")
        normalized_playbook_versions = {
            str(item_id).strip(): str(version).strip() or "v1"
            for item_id, version in dict(brief.playbook_versions or {}).items()
            if str(item_id).strip()
        }
        normalized_factor_versions = {
            str(item_id).strip(): str(version).strip() or "v1"
            for item_id, version in dict(brief.factor_versions or {}).items()
            if str(item_id).strip()
        }
        missing_playbooks = [
            item
            for item in selected_playbooks
            if playbook_registry.get(
                item,
                playbook_spec_map.get(item, {}).get("version") or normalized_playbook_versions.get(item, "v1"),
            )
            is None
        ]
        missing_factors = [
            item
            for item in selected_factors
            if factor_registry.get(
                item,
                factor_spec_map.get(item, {}).get("version") or normalized_factor_versions.get(item, "v1"),
            )
            is None
        ]
        if missing_playbooks:
            raise ValueError(f"未注册的战法: {','.join(missing_playbooks)}")
        if missing_factors:
            raise ValueError(f"未注册的因子: {','.join(missing_factors)}")
        for item_id in selected_playbooks:
            resolved_version = playbook_spec_map.get(item_id, {}).get("version") or normalized_playbook_versions.get(item_id, "v1")
            entry = _resolve_repository_strategy_entry(
                asset_type="playbook",
                asset_id=item_id,
                version=resolved_version,
            )
            if entry is None:
                raise ValueError(f"未找到战法仓库条目: {item_id}:{resolved_version}")
            _assert_runtime_strategy_access(entry, allow_explicit_only=True)
        for item_id in selected_factors:
            resolved_version = factor_spec_map.get(item_id, {}).get("version") or normalized_factor_versions.get(item_id, "v1")
            entry = _resolve_repository_strategy_entry(
                asset_type="factor",
                asset_id=item_id,
                version=resolved_version,
            )
            if entry is None:
                raise ValueError(f"未找到因子仓库条目: {item_id}:{resolved_version}")
            _assert_runtime_strategy_access(entry, allow_explicit_only=True)
        playbook_weight_inputs = {
            str(item_id).strip(): float(weight or 0.0)
            for item_id, weight in dict(brief.weights.playbooks or {}).items()
            if str(item_id).strip()
        }
        factor_weight_inputs = {
            str(item_id).strip(): float(weight or 0.0)
            for item_id, weight in dict(brief.weights.factors or {}).items()
            if str(item_id).strip()
        }
        for item_id, item in playbook_spec_map.items():
            if item.get("weight") is not None:
                playbook_weight_inputs[item_id] = float(item.get("weight") or 0.0)
        for item_id, item in factor_spec_map.items():
            if item.get("weight") is not None:
                factor_weight_inputs[item_id] = float(item.get("weight") or 0.0)
        playbook_weights = _normalize_brief_weights(selected_playbooks, playbook_weight_inputs)
        factor_weights = _normalize_brief_weights(selected_factors, factor_weight_inputs)
        factor_groups = {}
        for item_id in selected_factors:
            resolved_version = factor_spec_map.get(item_id, {}).get("version") or normalized_factor_versions.get(item_id, "v1")
            definition = factor_registry.get(item_id, resolved_version)
            factor_groups[item_id] = str(factor_spec_map.get(item_id, {}).get("group") or (definition.group if definition is not None else ""))
        user_preferences = {
            "excluded_theme_keywords": brief.excluded_theme_keywords,
        }
        if brief.max_single_amount is not None:
            user_preferences["max_single_amount"] = brief.max_single_amount
        if brief.equity_position_limit is not None:
            user_preferences["equity_position_limit"] = brief.equity_position_limit
        constraints_payload = _deep_merge_dict(
            {
                "hard_filters": {
                    "excluded_theme_keywords": brief.excluded_theme_keywords,
                },
                "user_preferences": user_preferences,
                "market_rules": {
                    "allowed_regimes": brief.allowed_regimes,
                    "blocked_regimes": brief.blocked_regimes,
                    "min_regime_score": brief.min_regime_score,
                    "allow_inferred_market_profile": brief.allow_inferred_market_profile,
                },
                "position_rules": {
                    "holding_symbols": brief.holding_symbols,
                    "max_single_amount": brief.max_single_amount,
                    "equity_position_limit": brief.equity_position_limit,
                },
                "risk_rules": {
                    "blocked_symbols": brief.blocked_symbols,
                    "max_total_position": brief.max_total_position,
                    "max_single_position": brief.max_single_position,
                    "daily_loss_limit": brief.daily_loss_limit,
                    "emergency_stop": brief.emergency_stop,
                },
                "execution_barriers": {
                    "require_fresh_snapshot": brief.require_fresh_snapshot,
                    "max_snapshot_age_seconds": brief.max_snapshot_age_seconds,
                    "max_price_deviation_pct": brief.max_price_deviation_pct,
                },
            },
            brief.custom_constraints.model_dump(),
        )
        market_context_payload = _deep_merge_dict(
            {
                "focus_topics": brief.focus_sectors,
                "holding_symbols": brief.holding_symbols,
                "notes": brief.notes,
            },
            dict(brief.market_context or {}),
        )
        ranking_secondary_keys = [
            str(item).strip()
            for item in list(brief.ranking_secondary_keys or [])
            if str(item).strip()
        ] or selected_factors[:2]
        return RuntimeComposeRequest.model_validate(
            {
                "request_id": brief.request_id,
                "account_id": brief.account_id,
                "agent": brief.agent.model_dump(),
                "intent": {
                    "mode": str(brief.intent_mode or "opportunity_scan").strip() or "opportunity_scan",
                    "objective": brief.objective,
                    "market_hypothesis": brief.market_hypothesis,
                    "trade_horizon": brief.trade_horizon,
                },
                "universe": {
                    "scope": brief.universe_scope,
                    "symbol_pool": brief.symbol_pool,
                    "sector_whitelist": brief.focus_sectors,
                    "sector_blacklist": brief.avoid_sectors,
                    "source": "runtime_compose_brief",
                },
                "strategy": {
                    "playbooks": [
                        {
                            "id": item_id,
                            "version": playbook_spec_map.get(item_id, {}).get("version") or normalized_playbook_versions.get(item_id, "v1"),
                            "weight": playbook_weights.get(item_id, 0.0),
                            "params": dict(playbook_spec_map.get(item_id, {}).get("params") or {}),
                        }
                        for item_id in selected_playbooks
                    ],
                    "factors": [
                        {
                            "id": item_id,
                            "group": factor_groups.get(item_id, ""),
                            "version": factor_spec_map.get(item_id, {}).get("version") or normalized_factor_versions.get(item_id, "v1"),
                            "weight": factor_weights.get(item_id, 0.0),
                            "params": dict(factor_spec_map.get(item_id, {}).get("params") or {}),
                        }
                        for item_id in selected_factors
                    ],
                    "ranking": {
                        "primary_score": str(brief.ranking_primary_score or "composite_score").strip() or "composite_score",
                        "secondary_keys": ranking_secondary_keys,
                    },
                },
                "constraints": constraints_payload,
                "market_context": market_context_payload,
                "learned_asset_options": {
                    "auto_apply_active": brief.auto_apply_active_learned_assets,
                    "max_auto_apply": brief.max_auto_apply_learned_assets,
                    "preferred_tags": brief.preferred_learned_asset_tags,
                    "blocked_asset_ids": brief.blocked_learned_asset_ids,
                },
                "output": {
                    "max_candidates": brief.max_candidates,
                    "include_filtered_reasons": True,
                    "include_score_breakdown": True,
                    "include_evidence": True,
                    "include_counter_evidence": True,
                    "return_mode": "proposal_ready",
                },
            }
        )

    def _infer_pipeline_source_from_compose_request(request: RuntimeComposeRequest) -> str:
        intent_mode = str(request.intent.mode or "").strip().lower()
        objective_text = " ".join(
            [
                str(request.intent.objective or ""),
                str(request.intent.market_hypothesis or ""),
                " ".join(str(item) for item in list(request.market_context.get("focus_topics") or [])),
            ]
        ).lower()
        playbook_ids = {str(item.id or "").strip().lower() for item in request.strategy.playbooks}
        holding_symbols = list(request.market_context.get("holding_symbols") or [])
        if intent_mode in {"day_trading", "intraday_t_scan"}:
            return "intraday_t_scan"
        if intent_mode in {"replacement_review", "position_monitor_scan"} or holding_symbols:
            return "position_monitor_scan"
        if intent_mode in {"tail_ambush_scan", "tail_close_ambush"} or "tail_close_ambush" in playbook_ids or "尾盘" in objective_text:
            return "tail_ambush_scan"
        if intent_mode in {"news_catalyst_scan"} or any(keyword in objective_text for keyword in ("新闻", "公告", "催化", "消息")):
            return "news_catalyst_scan"
        if intent_mode in {"sector_heat_scan"} or (
            request.universe.sector_whitelist and not request.universe.symbol_pool
        ):
            return "sector_heat_scan"
        return "market_universe_scan"

    def _tokenize_text(value: str) -> set[str]:
        return {
            token
            for token in [item.strip().lower() for item in str(value or "").replace("/", " ").replace(",", " ").split()]
            if len(token) >= 2
        }

    def _score_auto_learned_asset(request: RuntimeComposeRequest, entry: StrategyRepositoryEntry) -> float:
        score = 0.0
        content = dict(entry.content or {})
        preferred_tags = {str(item).strip().lower() for item in request.learned_asset_options.preferred_tags if str(item).strip()}
        entry_tags = {str(item).strip().lower() for item in entry.tags if str(item).strip()}
        if preferred_tags:
            score += float(len(preferred_tags & entry_tags)) * 3.0
        requested_playbooks = {item.id for item in request.strategy.playbooks}
        requested_factors = {item.id for item in request.strategy.factors}
        content_playbooks = {str(item).strip() for item in list(content.get("playbooks") or []) if str(item).strip()}
        content_factors = {
            str(item).strip()
            for item in list(content.get("factors") or []) + list(dict(content.get("factor_weights") or {}).keys()) + list(dict(content.get("weights") or {}).keys())
            if str(item).strip()
        }
        score += float(len(requested_playbooks & content_playbooks)) * 2.0
        score += float(len(requested_factors & content_factors)) * 2.0

        focus_topics = {
            str(item).strip().lower()
            for item in list(request.market_context.get("focus_topics") or []) + list(request.universe.sector_whitelist or [])
            if str(item).strip()
        }
        content_topics = {
            str(item).strip().lower()
            for item in list(content.get("focus_topics") or []) + list(content.get("sectors") or []) + list(content.get("themes") or [])
            if str(item).strip()
        }
        score += float(len(focus_topics & content_topics)) * 2.5

        text_tokens = set()
        text_tokens |= _tokenize_text(request.intent.objective)
        text_tokens |= _tokenize_text(request.intent.market_hypothesis)
        text_tokens |= _tokenize_text(request.intent.trade_horizon)
        text_tokens |= _tokenize_text(entry.name)
        text_tokens |= _tokenize_text(entry.source)
        content_tokens = set()
        for value in content.get("match_keywords") or []:
            content_tokens |= _tokenize_text(str(value))
        content_tokens |= _tokenize_text(" ".join(entry.tags))
        for key in ["summary", "hypothesis", "notes", "style"]:
            content_tokens |= _tokenize_text(str(content.get(key) or ""))
        overlap = len(text_tokens & content_tokens)
        score += float(overlap)

        if str(entry.author or "").strip() == str(request.agent.agent_id or "").strip():
            score += 0.5
        return score

    def _select_auto_learned_assets(request: RuntimeComposeRequest) -> list[StrategyRepositoryEntry]:
        options = request.learned_asset_options
        if not options.auto_apply_active or options.max_auto_apply <= 0:
            return []
        explicit_ids = {item.id for item in request.learned_assets}
        blocked_ids = {str(item).strip() for item in options.blocked_asset_ids if str(item).strip()}
        candidates = []
        for entry in strategy_atomic_repository.list_entries(status="active"):
            if entry.type not in {"learned_combo", "template", "playbook"}:
                continue
            if entry.id in explicit_ids or entry.id in blocked_ids:
                continue
            score = _score_auto_learned_asset(request, entry)
            if score <= 0:
                continue
            candidates.append((score, entry))
        candidates.sort(key=lambda item: (item[0], item[1].updated_at, item[1].id), reverse=True)
        return [entry for _, entry in candidates[: options.max_auto_apply]]

    def _serialize_repository_entry(entry: StrategyRepositoryEntry) -> dict:
        payload = entry.model_dump()
        payload["runtime_policy"] = entry.runtime_policy()
        return payload

    def _build_version_race_summary(
        version_item: dict[str, Any],
        *,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        asset_type = str(version_item.get("type") or "").strip()
        asset_id = str(version_item.get("id") or "").strip()
        per_version: dict[str, dict[str, Any]] = {}
        key_field = "used_playbook_keys" if asset_type == "playbook" else "used_factor_keys"

        for version_meta in list(version_item.get("versions") or []):
            version = str(version_meta.get("version") or "").strip()
            if not version:
                continue
            per_version[version] = {
                "version": version,
                "runtime_mode": str(((version_meta.get("runtime_policy") or {}).get("mode")) or "").strip(),
                "status": str(version_meta.get("status") or "").strip(),
                "usage_count": 0,
                "adopted_count": 0,
                "settled_count": 0,
                "hit_rate": None,
                "latest_used_at": "",
                "latest_outcome_status": "",
            }

        hit_samples: dict[str, list[float]] = defaultdict(list)
        for record in records:
            used_keys = {
                str(item).strip()
                for item in list(record.get(key_field) or record.get("used_asset_keys") or [])
                if str(item).strip()
            }
            if not used_keys:
                continue
            generated_at = str(record.get("generated_at") or record.get("recorded_at") or "").strip()
            adoption = dict(record.get("adoption") or {})
            outcome = dict(record.get("outcome") or {})
            posterior_metrics = dict(outcome.get("posterior_metrics") or {})
            adoption_status = str(adoption.get("status") or "").strip()
            outcome_status = str(outcome.get("status") or "").strip()

            for version, bucket in per_version.items():
                key = f"{asset_id}:{version}"
                if key not in used_keys:
                    continue
                bucket["usage_count"] += 1
                if adoption_status == "adopted":
                    bucket["adopted_count"] += 1
                if outcome_status == "settled":
                    bucket["settled_count"] += 1
                if generated_at and generated_at > str(bucket.get("latest_used_at") or ""):
                    bucket["latest_used_at"] = generated_at
                    bucket["latest_outcome_status"] = outcome_status
                hit_rate = posterior_metrics.get("hit_rate")
                if isinstance(hit_rate, (int, float)) and not math.isnan(float(hit_rate)):
                    hit_samples[version].append(float(hit_rate))

        for version, bucket in per_version.items():
            samples = hit_samples.get(version) or []
            if samples:
                bucket["hit_rate"] = round(sum(samples) / len(samples), 4)

        candidates = list(per_version.values())
        blocked_candidates = [item for item in candidates if item["runtime_mode"] == "blocked"]
        scored_candidates = [item for item in candidates if item["runtime_mode"] != "blocked"]
        real_candidates = [
            item
            for item in scored_candidates
            if item["adopted_count"] > 0 or item["settled_count"] > 0 or item["hit_rate"] is not None
        ]

        def _candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            hit_rate = float(item["hit_rate"]) if item["hit_rate"] is not None else -1.0
            return (
                int(item["settled_count"]),
                int(item["adopted_count"]),
                hit_rate,
                int(item["usage_count"]),
                str(item["latest_used_at"] or ""),
                str(item["version"] or ""),
            )

        recommended_version = str(version_item.get("recommended_version") or "").strip()
        recommended_reason = "缺少真实结果，沿用仓库默认推荐"
        has_real_outcome = bool(real_candidates)
        if real_candidates:
            real_candidates.sort(key=_candidate_sort_key, reverse=True)
            winner = real_candidates[0]
            recommended_version = str(winner.get("version") or recommended_version).strip()
            recommended_reason = (
                f"基于真实结果推荐: usage={winner['usage_count']} adopted={winner['adopted_count']} "
                f"settled={winner['settled_count']} hit_rate={winner['hit_rate']}"
            )

        scored_candidates.sort(key=_candidate_sort_key, reverse=True)
        return {
            "recommended_version": recommended_version,
            "recommended_reason": recommended_reason,
            "has_real_outcome": has_real_outcome,
            "candidates": scored_candidates,
            "blocked_candidates": blocked_candidates,
        }

    def _build_version_governance_suggestion(
        version_item: dict[str, Any],
        *,
        race_summary: dict[str, Any],
    ) -> dict[str, Any]:
        default_recommended_version = str(version_item.get("recommended_version") or "").strip()
        race_recommended_version = str(race_summary.get("recommended_version") or "").strip()
        has_real_outcome = bool(race_summary.get("has_real_outcome"))

        if not has_real_outcome:
            return {
                "recommended_action": "observe_only",
                "governance_route": "race_observe",
                "attention_level": "low",
                "reason": "暂无真实 adoption/outcome 结果，继续观察即可",
            }
        if race_recommended_version and race_recommended_version != default_recommended_version:
            return {
                "recommended_action": "review_cutover",
                "governance_route": "race_review",
                "attention_level": "high",
                "reason": (
                    f"真实结果当前更支持 {race_recommended_version}，"
                    f"已偏离默认推荐 {default_recommended_version or 'unknown'}，建议进入切流评审"
                ),
            }
        return {
            "recommended_action": "maintain_default",
            "governance_route": "race_maintain",
            "attention_level": "normal",
            "reason": "真实结果与默认推荐一致，维持当前默认版本即可",
        }

    def _build_repository_governance_summary(version_items: list[dict[str, Any]]) -> dict[str, Any]:
        review_cutover_count = sum(
            1
            for item in version_items
            if str((item.get("governance_suggestion") or {}).get("recommended_action") or "").strip() == "review_cutover"
        )
        maintain_default_count = sum(
            1
            for item in version_items
            if str((item.get("governance_suggestion") or {}).get("recommended_action") or "").strip() == "maintain_default"
        )
        observe_only_count = sum(
            1
            for item in version_items
            if str((item.get("governance_suggestion") or {}).get("recommended_action") or "").strip() == "observe_only"
        )
        return {
            "review_cutover_count": review_cutover_count,
            "maintain_default_count": maintain_default_count,
            "observe_only_count": observe_only_count,
        }

    def _build_repository_governance_panel(
        *,
        asset_type: StrategyAssetType | None = None,
        asset_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        version_view = _build_repository_version_view(asset_type=asset_type, asset_id=asset_id)
        governance_summary = _build_repository_governance_summary(version_view)
        attention_rank = {"high": 0, "normal": 1, "low": 2}
        sorted_items = sorted(
            version_view,
            key=lambda item: (
                attention_rank.get(
                    str((item.get("governance_suggestion") or {}).get("attention_level") or "").strip(),
                    9,
                ),
                0 if str((item.get("governance_suggestion") or {}).get("recommended_action") or "").strip() == "review_cutover" else 1,
                str(item.get("id") or ""),
            ),
        )
        high_attention_items = [
            item
            for item in sorted_items
            if str((item.get("governance_suggestion") or {}).get("attention_level") or "").strip() == "high"
        ]
        return {
            "summary": {
                "total_assets": len(version_view),
                "high_attention_count": len(high_attention_items),
                **governance_summary,
            },
            "items": sorted_items[:limit],
            "high_attention_items": high_attention_items[:limit],
        }

    def _build_repository_version_view(
        *,
        asset_type: StrategyAssetType | None = None,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        base_items = strategy_atomic_repository.version_view(type=asset_type, asset_id=asset_id)
        records = evaluation_ledger_service.list_records(limit=300)
        enriched_items: list[dict[str, Any]] = []
        for item in base_items:
            race_summary = _build_version_race_summary(item, records=records)
            enriched_items.append(
                {
                    **item,
                    "race_summary": race_summary,
                    "governance_suggestion": _build_version_governance_suggestion(item, race_summary=race_summary),
                }
            )
        return enriched_items

    def _ensure_repository_asset(
        *,
        asset_id: str,
        asset_type: StrategyAssetType,
        version: str,
        author: str,
        source: str,
        params_schema: dict | None = None,
        content: dict | None = None,
        tags: list[str] | None = None,
    ) -> StrategyRepositoryEntry:
        existing = strategy_atomic_repository.get(asset_id, version)
        if existing is not None:
            return existing
        entry = StrategyRepositoryEntry(
            id=asset_id,
            name=asset_id,
            type=asset_type,
            status="experimental",
            version=version,
            author=author,
            source=source,
            params_schema=params_schema or {},
            content=content or {},
            tags=tags or [],
        )
        return strategy_atomic_repository.register(entry)

    def _register_compose_assets(request: RuntimeComposeRequest) -> dict:
        used_assets: list[dict] = []
        learned_assets: list[dict] = []
        active_learned_assets: list[dict] = []
        auto_selected_learned_assets: list[dict] = []
        author = request.agent.agent_id or "ashare"
        for playbook in request.strategy.playbooks:
            registered_playbook = playbook_registry.get(playbook.id, playbook.version)
            entry = (
                strategy_atomic_repository.get(playbook.id, playbook.version)
                if registered_playbook is not None
                else _ensure_repository_asset(
                    asset_id=playbook.id,
                    asset_type="playbook",
                    version=playbook.version,
                    author=author,
                    source="runtime_compose",
                    params_schema=playbook.params,
                    content={"weight": playbook.weight, "params": playbook.params},
                    tags=["compose-playbook", "auto-registered"],
                )
            )
            if entry is None:
                continue
            used_assets.append(_serialize_repository_entry(entry))
        for factor in request.strategy.factors:
            registered_factor = factor_registry.get(factor.id, factor.version)
            entry = (
                strategy_atomic_repository.get(factor.id, factor.version)
                if registered_factor is not None
                else _ensure_repository_asset(
                    asset_id=factor.id,
                    asset_type="factor",
                    version=factor.version,
                    author=author,
                    source="runtime_compose",
                    params_schema=factor.params,
                    content={"group": factor.group, "weight": factor.weight, "params": factor.params},
                    tags=["compose-factor", "auto-registered"],
                )
            )
            if entry is None:
                continue
            used_assets.append(_serialize_repository_entry(entry))
        for learned in request.learned_assets:
            existing = strategy_atomic_repository.get(learned.id, learned.version)
            if existing is None:
                existing = strategy_atomic_repository.submit_learned_entry(
                    asset_id=learned.id,
                    name=learned.name,
                    asset_type=learned.type,
                    author=author,
                    source=learned.source,
                    content=learned.content,
                    version=learned.version,
                    params_schema=learned.params_schema,
                    evidence_schema=learned.evidence_schema,
                    risk_notes=learned.risk_notes,
                    tags=learned.tags,
                )
            learned_payload = _serialize_repository_entry(existing)
            learned_assets.append(learned_payload)
            if str(existing.status).strip() == "active":
                active_learned_assets.append(learned_payload)
        for entry in _select_auto_learned_assets(request):
            payload = _serialize_repository_entry(entry)
            learned_assets.append(payload)
            active_learned_assets.append(payload)
            auto_selected_learned_assets.append(payload)
        return {
            "used_assets": used_assets,
            "learned_assets": learned_assets,
            "active_learned_assets": active_learned_assets,
            "auto_selected_learned_assets": auto_selected_learned_assets,
            "repository_summary": strategy_atomic_repository.summary(),
            "registered_playbooks": [
                item.model_dump() for item in playbook_registry.list_all()
            ],
            "registered_factors": [
                item.model_dump() for item in factor_registry.list_all()
            ],
        }

    def _build_composition_manifest(
        request: RuntimeComposeRequest,
        *,
        constraint_summary: dict[str, Any],
        compose_output: dict[str, Any],
    ) -> dict[str, Any]:
        top_candidates = list(compose_output.get("candidates") or [])
        explanations = dict(compose_output.get("explanations") or {})
        return {
            "intent": {
                "mode": request.intent.mode,
                "objective": request.intent.objective,
                "market_hypothesis": request.intent.market_hypothesis,
                "trade_horizon": request.intent.trade_horizon,
            },
            "universe": {
                "scope": request.universe.scope,
                "symbol_pool": list(request.universe.symbol_pool or []),
                "sector_whitelist": list(request.universe.sector_whitelist or []),
                "sector_blacklist": list(request.universe.sector_blacklist or []),
                "source": request.universe.source,
            },
            "playbooks": [
                {
                    "id": item.id,
                    "version": item.version,
                    "weight": round(float(item.weight or 0.0), 4),
                    "params": dict(item.params or {}),
                }
                for item in request.strategy.playbooks
            ],
            "factors": [
                {
                    "id": item.id,
                    "group": item.group,
                    "version": item.version,
                    "weight": round(float(item.weight or 0.0), 4),
                    "params": dict(item.params or {}),
                }
                for item in request.strategy.factors
            ],
            "ranking": request.strategy.ranking.model_dump(),
            "filters": constraint_summary,
            "evidence": {
                "strategy_summary": str(explanations.get("strategy_summary") or ""),
                "weight_summary": [str(item) for item in list(explanations.get("weight_summary") or []) if str(item).strip()],
                "constraint_summary": [str(item) for item in list(explanations.get("constraint_summary") or []) if str(item).strip()],
                "market_driver_summary": [str(item) for item in list(explanations.get("market_driver_summary") or []) if str(item).strip()],
                "candidate_evidence_preview": [
                    {
                        "symbol": str(item.get("symbol") or ""),
                        "evidence": [str(line) for line in list(item.get("evidence") or [])[:4] if str(line).strip()],
                        "counter_evidence": [str(line) for line in list(item.get("counter_evidence") or [])[:3] if str(line).strip()],
                    }
                    for item in top_candidates[:3]
                ],
            },
            "proposal_packet": dict(compose_output.get("proposal_packet") or {}),
        }

    def _build_brief_execution_summary(
        brief: RuntimeComposeBriefRequest,
        compose_request: RuntimeComposeRequest,
    ) -> dict[str, Any]:
        return {
            "custom_profile_independent": True,
            "used_reference_profile": False,
            "profile_dependency": "none",
            "validated_compose_request": True,
            "selected_playbooks": [item.id for item in compose_request.strategy.playbooks],
            "selected_factors": [item.id for item in compose_request.strategy.factors],
            "ranking": compose_request.strategy.ranking.model_dump(),
            "constraint_sections": [
                key
                for key, value in compose_request.constraints.model_dump().items()
                if value not in ({}, [], None, "")
            ],
            "market_context_keys": sorted(
                [str(key) for key, value in dict(brief.market_context or {}).items() if value not in (None, "", [], {})]
            ),
        }

    def _filter_top_picks_by_preferences(
        decisions: list[dict],
        *,
        runtime_config: RuntimeConfig | None = None,
        sector_map: dict[str, str] | None = None,
        pack_items: list[dict] | None = None,
    ) -> tuple[list[dict], list[dict], list[str]]:
        keywords = _resolve_excluded_theme_keywords(runtime_config)
        if not decisions or not keywords:
            return decisions, [], keywords

        sector_map = sector_map or {}
        pack_map = {str(item.get("symbol")): item for item in (pack_items or []) if item.get("symbol")}
        filtered: list[dict] = []
        excluded: list[dict] = []
        for item in decisions:
            symbol = str(item.get("symbol") or "").strip()
            if not symbol:
                continue
            pack_item = pack_map.get(symbol) or {}
            symbol_context = pack_item.get("symbol_context") or {}
            sector_relative = symbol_context.get("sector_relative") or {}
            resolved_sector = str(
                sector_map.get(symbol)
                or item.get("resolved_sector")
                or pack_item.get("resolved_sector")
                or ""
            ).strip()
            name = str(item.get("name") or market_adapter.get_symbol_name(symbol) or symbol).strip()
            matched = match_excluded_theme(
                keywords,
                name=name,
                resolved_sector=resolved_sector,
                extra_texts=sector_relative.get("sector_tags") or [],
            )
            if matched:
                excluded.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        "resolved_sector": resolved_sector,
                        "matched_keyword": matched["keyword"],
                        "matched_field": matched["field"],
                        "matched_text": matched["matched_text"],
                    }
                )
                continue
            updated = dict(item)
            updated["name"] = name
            if resolved_sector:
                updated["resolved_sector"] = resolved_sector
            filtered.append(updated)
        return filtered, excluded, keywords

    monitor_change_notifier = (
        MonitorChangeNotifier(monitor_state_service, message_dispatcher)
        if monitor_state_service
        else None
    )

    def _score_snapshot(symbol: str, last_price: float, pre_close: float, volume: float, rank: int) -> dict:
        base_price = pre_close or last_price or 1.0
        momentum = ((last_price - base_price) / base_price) * 100.0
        liquidity_score = min(volume / 1_000_000.0, 1.0) * 14.0
        price_bias = _clamp(momentum * 0.9, -4.0, 6.0)
        stability_score = max(0.0, 4.0 - abs(momentum) * 0.35)
        rank_penalty = min(max(rank - 1, 0), 9) * 1.2
        selection_score = round(
            max(
                46.0,
                min(
                    88.0,
                    58.0 + liquidity_score + price_bias + stability_score - rank_penalty,
                ),
            ),
            2,
        )
        action = "BUY" if selection_score >= 68.0 else "HOLD"
        return {
            "symbol": symbol,
            "rank": rank,
            "selection_score": selection_score,
            "action": action,
            "score_breakdown": {
                "coarse_score": selection_score,
                "momentum_pct": round(momentum, 2),
                "liquidity_score": round(liquidity_score, 2),
                "price_bias_score": round(price_bias, 2),
                "stability_score": round(stability_score, 2),
                "rank_penalty_score": round(rank_penalty, 2),
            },
            "summary": (
                f"{symbol} 粗筛分 {selection_score}，"
                f"量能{'较强' if liquidity_score >= 9.0 else '一般'}，"
                f"动能{'可跟踪' if action == 'BUY' else '待观察'}"
            ),
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

    def _playbook_ctx_payload(item) -> dict:
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            return item.model_dump()
        return dict(item or {})

    def _resolve_playbook_match_payload(ctx: dict | None) -> dict | None:
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

    def _top_pick_sort_key(item: dict, playbook_map: dict[str, dict]) -> tuple[int, float, float, float, int, float]:
        symbol = str(item.get("symbol") or "")
        ctx = playbook_map.get(symbol, {})
        match_payload = _resolve_playbook_match_payload(ctx)
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

    def _apply_playbook_order(decisions: list[dict], playbook_contexts: list) -> list[dict]:
        if not decisions or not playbook_contexts:
            return decisions
        playbook_payloads = [_playbook_ctx_payload(item) for item in playbook_contexts]
        playbook_map = {
            str(item.get("symbol") or ""): item
            for item in playbook_payloads
            if item.get("symbol")
        }
        enriched: list[dict] = []
        for item in decisions:
            updated = dict(item)
            symbol = str(updated.get("symbol") or "")
            ctx = playbook_map.get(symbol)
            if ctx:
                updated["assigned_playbook"] = ctx.get("playbook", updated.get("assigned_playbook"))
                updated["playbook_context"] = ctx
                match_payload = _resolve_playbook_match_payload(ctx)
                if match_payload is not None:
                    updated["playbook_match_score"] = match_payload
            enriched.append(updated)
        ordered = sorted(enriched, key=lambda item: _top_pick_sort_key(item, playbook_map), reverse=True)
        for index, item in enumerate(ordered, start=1):
            item["rank"] = index
        return ordered

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
            "selection_preferences": snapshot.get("selection_preferences", {}),
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

    def _resolve_trade_date(payload: dict) -> str:
        generated_at = payload.get("generated_at") or datetime.now().isoformat()
        return datetime.fromisoformat(generated_at).date().isoformat()

    def _filter_runtime_cases(synced_cases: list, trade_date: str, top_picks: list[dict]) -> list:
        selected_symbols = {str(item.get("symbol") or "") for item in top_picks if item.get("symbol")}
        if not synced_cases or not selected_symbols:
            return []
        filtered = [
            case
            for case in synced_cases
            if getattr(case, "trade_date", None) == trade_date and getattr(case, "symbol", None) in selected_symbols
        ]
        return sorted(filtered, key=lambda case: (case.runtime_snapshot.rank, case.symbol))

    def _persist_runtime_report(payload: dict) -> dict:
        summary = payload.get("summary", {})
        top_picks = list(payload.get("top_picks", []))
        trade_date = _resolve_trade_date(payload)
        synced_cases = []
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
            synced_cases = _filter_runtime_cases(synced_cases, trade_date, top_picks)
            if monitor_state_service and synced_cases:
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
                        top_picks, excluded_candidates, excluded_theme_keywords = _filter_top_picks_by_preferences(
                            top_picks,
                            runtime_config=_runtime_config(),
                            sector_map=sector_map,
                            pack_items=pack.get("items", []),
                        )
                        if excluded_candidates:
                            payload["top_picks"] = top_picks
                            payload["selection_preferences"] = {
                                "excluded_theme_keywords": excluded_theme_keywords,
                                "excluded_candidates": excluded_candidates,
                            }
                            summary = payload.get("summary", {})
                            summary["buy_count"] = sum(1 for item in top_picks if item.get("action") == "BUY")
                            summary["hold_count"] = sum(1 for item in top_picks if item.get("action") == "HOLD")
                            payload["summary"] = summary
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
                        payload["top_picks"] = _apply_playbook_order(payload.get("top_picks", top_picks), payload["playbook_contexts"])
                        top_picks = payload["top_picks"]
                        strategy_context = {
                            "market_profile": payload["market_profile"],
                            "sector_profiles": payload["sector_profiles"],
                            "playbook_contexts": payload["playbook_contexts"],
                            "behavior_profiles": payload["behavior_profiles"],
                            "hot_sectors": payload["hot_sectors"],
                        }
                        if candidate_case_service and top_picks:
                            synced_cases = candidate_case_service.sync_from_runtime_report(
                                payload,
                                focus_pool_capacity=focus_pool_capacity,
                                execution_pool_capacity=execution_pool_capacity,
                            )
                            synced_cases = _filter_runtime_cases(synced_cases, trade_date, top_picks)
                            if synced_cases:
                                monitor_state_service.save_pool_snapshot(
                                    trade_date=trade_date,
                                    pool_snapshot=candidate_case_service.build_pool_snapshot(trade_date),
                                    source="runtime_pipeline",
                                )
                                if monitor_change_notifier:
                                    monitor_change_notifier.dispatch_latest()
                monitor_state_service.mark_poll_if_due("candidate", trigger="runtime_pipeline", force=True)

        top_picks = list(payload.get("top_picks", top_picks))
        pool_mgr.update(
            [item["symbol"] for item in top_picks if item.get("symbol")],
            {str(item.get("symbol")): float(item.get("selection_score", 0.0) or 0.0) for item in top_picks if item.get("symbol")},
            names={str(item.get("symbol")): str(item.get("name") or item.get("symbol")) for item in top_picks if item.get("symbol")},
        )
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
            "selection_preferences": payload.get("selection_preferences", {}),
            "report_path": str(latest_report),
        }
        runtime_state_store.set("latest_runtime_report", snapshot)
        history = runtime_state_store.get("runtime_jobs", [])
        history.append(snapshot)
        runtime_state_store.set("runtime_jobs", history[-30:])
        _persist_runtime_context(payload, snapshot, strategy_context)
        payload["case_ids"] = [case.case_id for case in synced_cases]
        payload["case_count"] = len(synced_cases)
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

    @router.get("/strategy-repository")
    async def get_strategy_repository(
        asset_type: StrategyAssetType | None = None,
        status: str | None = None,
        runtime_mode: StrategyRuntimeConsumeMode | None = None,
        asset_id: str | None = None,
    ):
        version_view = _build_repository_version_view(asset_type=asset_type, asset_id=asset_id)
        entries = strategy_atomic_repository.list_entries(
            type=asset_type,
            status=status,
        )
        if asset_id is not None:
            entries = [entry for entry in entries if entry.id == str(asset_id).strip()]
        if runtime_mode is not None:
            entries = [entry for entry in entries if str(entry.runtime_policy().get("mode") or "").strip() == runtime_mode]
        return {
            "ok": True,
            "items": [_serialize_repository_entry(entry) for entry in entries],
            "version_view": version_view,
            "governance_summary": _build_repository_governance_summary(version_view),
            "summary": strategy_atomic_repository.summary(),
        }

    @router.get("/strategy-repository/panel")
    async def get_strategy_repository_panel(
        limit: int = 20,
        asset_type: StrategyAssetType | None = None,
        asset_id: str | None = None,
    ):
        panel = _build_repository_governance_panel(asset_type=asset_type, asset_id=asset_id, limit=limit)
        return {
            "ok": True,
            **panel,
        }

    @router.get("/factors")
    async def get_runtime_factors():
        items = factor_registry.list_all()
        return {
            "ok": True,
            "items": [item.model_dump() for item in items],
            "count": len(items),
        }

    @router.get("/playbooks")
    async def get_runtime_playbooks():
        items = playbook_registry.list_all()
        return {
            "ok": True,
            "items": [item.model_dump() for item in items],
            "count": len(items),
        }

    @router.get("/capabilities")
    async def get_runtime_capabilities():
        factors = factor_registry.list_all()
        playbooks = playbook_registry.list_all()
        repository_summary = strategy_atomic_repository.summary()
        return {
            "ok": True,
            "compose_available": True,
            "repository_summary": repository_summary,
            "system_tools": [
                {
                    "category": "market_data",
                    "endpoints": [
                        "/data/catalog",
                        "/data/market-context/latest",
                        "/data/symbol-contexts/latest",
                        "/data/dossiers/latest",
                        "/data/event-context/latest"
                    ],
                    "description": "获取全市场情绪画像、板块轮动、个股基本面与事件催化档案",
                },
                {
                    "category": "discussion_and_collaboration",
                    "endpoints": [
                        "/data/discussion-context/latest",
                        "/system/discussions/summary",
                        "/system/discussions/cycles/{trade_date}",
                        "/system/discussions/agent-packets"
                    ],
                    "description": "获取当前交易日的多 Agent 讨论进度、争议焦点与结构化上下文包",
                },
                {
                    "category": "execution_and_account",
                    "endpoints": [
                        "/system/account-state",
                        "/system/discussions/execution-precheck",
                        "/system/discussions/execution-intents",
                        "/system/discussions/execution-intents/dispatch",
                        "/system/discussions/execution-dispatch/latest"
                    ],
                    "description": "获取真实账户资金、持仓、并做订单执行预演或真实提交",
                },
                {
                    "category": "strategy_and_runtime",
                    "endpoints": [
                        "/data/runtime-context/latest",
                        "/runtime/factors",
                        "/runtime/playbooks",
                        "/runtime/capabilities",
                        "/runtime/jobs/compose",
                        "/runtime/jobs/compose-from-brief",
                        "/runtime/jobs/news-catalyst",
                        "/runtime/jobs/tail-ambush"
                    ],
                    "description": "发现并调用程序底座的选股因子、战法组件与编排引擎",
                },
                {
                    "category": "learning_and_governance",
                    "endpoints": [
                        "/runtime/strategy-repository",
                        "/runtime/strategy-repository/panel",
                        "/runtime/learned-assets/transition",
                        "/runtime/learned-assets/panel",
                        "/runtime/evaluations/panel",
                        "/system/reports/postclose-master",
                        "/system/audits"
                    ],
                    "description": "管理学习产物、回看 compose 账本评估与追踪盘后复盘审计结果",
                }
            ],
            "factors": [
                {
                    **item.model_dump(),
                    "executable": True,
                    "params_schema": item.params_schema,
                    "evidence_schema": item.evidence_schema,
                    "runtime_role": "因子评分器",
                    "agent_usage": "Agent 可组合多个因子，作为 runtime compose 的加权输入",
                }
                for item in factors
            ],
            "playbooks": [
                {
                    **item.model_dump(),
                    "executable": True,
                    "params_schema": item.params_schema,
                    "evidence_schema": item.evidence_schema,
                    "runtime_role": "战法执行器",
                    "agent_usage": "Agent 可按市场假设与交易周期组合战法，作为候选重排依据",
                }
                for item in playbooks
            ],
            "compose_contract": {
                "positioning": "runtime 是 Agent 可参数化调用的策略工具与证据工具，不是固定选股器",
                "agent_responsibilities": [
                    "先提出市场假设，再组织战法/因子/约束参数",
                    "根据盘中变化决定是否再次调用 compose，而不是机械重复运行",
                    "拿结构化候选、证据、反证进入讨论与质询",
                    "只有当已转正 learned asset 与当前市场假设、主题方向、战法/因子结构明显匹配时，才开启自动吸附",
                ],
                "runtime_responsibilities": [
                    "执行 Agent 组织好的参数化扫描",
                    "返回候选、过滤原因、证据、反证、仓位提示",
                    "把学习产物沉淀为可审批、可升级、可归档的仓库资产",
                ],
            },
            "compose_brief_contract": {
                "positioning": "给 Agent 的轻量编排入口，适合先提交市场假设、方向偏好、战法/因子与仓位偏好，再由程序组装正式 compose 请求",
                "endpoint": "/runtime/jobs/compose-from-brief",
                "selection_rule": "playbooks/factors 不应固定写死，应随当前任务变化而变化；先定义任务，再选组合。任务画像只提供建议，不构成强绑定；Agent 也可以完全跳过默认画像，自定义组合。",
                "custom_first": True,
                "fields": [
                    "intent_mode",
                    "objective",
                    "market_hypothesis",
                    "trade_horizon",
                    "focus_sectors",
                    "avoid_sectors",
                    "excluded_theme_keywords",
                    "allowed_regimes",
                    "blocked_regimes",
                    "min_regime_score",
                    "allow_inferred_market_profile",
                    "playbooks",
                    "factors",
                    "playbook_specs",
                    "factor_specs",
                    "playbook_versions",
                    "factor_versions",
                    "weights",
                    "ranking_primary_score",
                    "ranking_secondary_keys",
                    "auto_apply_active_learned_assets",
                    "max_auto_apply_learned_assets",
                    "preferred_learned_asset_tags",
                    "blocked_learned_asset_ids",
                    "max_candidates",
                    "max_single_amount",
                    "equity_position_limit",
                    "holding_symbols",
                    "blocked_symbols",
                    "max_total_position",
                    "max_single_position",
                    "daily_loss_limit",
                    "emergency_stop",
                    "require_fresh_snapshot",
                    "max_snapshot_age_seconds",
                    "max_price_deviation_pct",
                    "custom_constraints",
                    "market_context",
                ],
                "orchestration_trace_fields": [
                    "market_hypothesis",
                    "tool_selection_reason",
                    "rejected_options",
                    "should_run_compose",
                    "next_action",
                ],
            },
            "strategy_selection_method": {
                "positioning": "先识别任务、市场阶段、交易周期、当前持仓状态和证据缺口，再组织组合；不要把任务名直接等同于固定模板。",
                "steps": [
                    "先判断当前是机会发现、龙头博弈、做T、低位潜伏、超跌修复、持仓替换、盘后复盘，还是几种任务叠加。",
                    "再判断市场阶段：主升、回暖、震荡、普跌、防守、轮动初期。",
                    "再判断交易周期：intraday / intraday_to_overnight / overnight_to_swing。",
                    "最后才选 playbooks、factors、weights、约束和 learned assets。",
                ],
            },
            "task_dimensions": {
                "objective_family": [
                    "opportunity_scan",
                    "leader_battle",
                    "intraday_discovery",
                    "day_trading",
                    "low_level_digging",
                    "oversold_rebound",
                    "replacement_review",
                    "postclose_learning",
                ],
                "market_phase": ["trend", "rotation", "recovery", "range", "selloff", "defensive"],
                "trade_horizon": ["intraday", "intraday_to_overnight", "overnight_to_swing"],
                "portfolio_state": ["underweight", "fully_allocated", "holding_review_needed", "t_window_open"],
            },
            "task_profiles": {
                "basic_scan": {
                    "label": "基础筛选",
                    "when_to_use": "先做一轮宽口径扫描，建立基础候选池，再决定是否升级到更强战法。",
                    "objective_template": "先做一轮基础机会扫描，找出可继续讨论的候选。",
                    "trade_horizon": "intraday_to_overnight",
                    "recommended_playbooks": ["trend_acceleration", "sector_resonance"],
                    "recommended_factors": ["momentum_slope", "sector_heat_score", "breakout_quality"],
                    "why": "适合作为默认底盘，但不应永远只停留在这套组合。",
                    "binding_level": "advisory_only",
                },
                "leader_playbook": {
                    "label": "龙头战法",
                    "when_to_use": "市场处于主升、热点集中、前排强度明显时。",
                    "objective_template": "围绕主线前排寻找龙头博弈机会。",
                    "trade_horizon": "intraday_to_overnight",
                    "recommended_playbooks": ["leader_chase", "sector_resonance", "trend_acceleration"],
                    "recommended_factors": ["momentum_slope", "sector_heat_score", "breakout_quality"],
                    "why": "强调前排、热点共振和强动能，不适合退潮或低弹性行情。",
                    "binding_level": "advisory_only",
                },
                "intraday_discovery": {
                    "label": "盘中发现机会",
                    "when_to_use": "盘中主线轮动、分时异动增多，需要抓新冒头机会时。",
                    "objective_template": "围绕盘中异动和热点扩散寻找新机会票。",
                    "trade_horizon": "intraday",
                    "recommended_playbooks": ["weak_to_strong_intraday", "sector_resonance", "trend_acceleration"],
                    "recommended_factors": ["momentum_slope", "sector_heat_score", "main_fund_inflow"],
                    "why": "更适合实时发现，不适合只靠日线静态分数。",
                    "binding_level": "advisory_only",
                },
                "day_trading": {
                    "label": "日内做T",
                    "when_to_use": "已有持仓，需要判断是否做顺势增强或高抛低吸时。",
                    "objective_template": "围绕现有持仓寻找日内做T和节奏优化机会。",
                    "trade_horizon": "intraday",
                    "recommended_playbooks": ["weak_to_strong_intraday", "position_replacement"],
                    "recommended_factors": ["momentum_slope", "liquidity_score", "main_fund_inflow"],
                    "why": "重点服务持仓优化，不应把做T变成脱离主仓逻辑的噪声交易。",
                    "binding_level": "advisory_only",
                },
                "low_level_digging": {
                    "label": "低位挖掘",
                    "when_to_use": "市场处于轮动初期或需要寻找尚未充分发酵的方向时。",
                    "objective_template": "围绕低位启动与预期差寻找潜伏机会。",
                    "trade_horizon": "overnight_to_swing",
                    "recommended_playbooks": ["tail_close_ambush", "sector_resonance"],
                    "recommended_factors": ["sector_heat_score", "breakout_quality", "stability_score"],
                    "why": "强调潜伏和预期差，不适合已经一致高潮的龙头票。",
                    "binding_level": "advisory_only",
                },
                "oversold_rebound": {
                    "label": "超跌反弹",
                    "when_to_use": "普跌后修复、超跌品种止跌、需要做均值回归博弈时。",
                    "objective_template": "围绕超跌修复寻找反弹性价比机会。",
                    "trade_horizon": "intraday_to_overnight",
                    "recommended_playbooks": ["oversold_rebound", "tail_close_ambush"],
                    "recommended_factors": ["stability_score", "price_bias_score", "liquidity_score"],
                    "why": "强调偏离修复，不适合强趋势追高。",
                    "binding_level": "advisory_only",
                },
                "replacement_review": {
                    "label": "持仓替换",
                    "when_to_use": "仓位已满或持仓质量下降，需要比较新旧机会成本时。",
                    "objective_template": "围绕现有持仓与新候选做替换效率复核。",
                    "trade_horizon": "intraday_to_overnight",
                    "recommended_playbooks": ["position_replacement", "sector_resonance"],
                    "recommended_factors": ["momentum_slope", "sector_heat_score", "breakout_quality"],
                    "why": "重点解决替谁、为什么更优，而不是重新从零选股。",
                    "binding_level": "advisory_only",
                },
            },
            "composition_rules": [
                "允许多任务画像混编，不要求一次 compose 只能选一个画像。",
                "允许只借用某个画像里的 playbook，不必整套照搬。",
                "允许 agent 完全跳出画像，自定义组合，但必须说明为什么当前任务不适合已有模板。",
                "若任务是持仓替换或做T，应把 holding_symbols 和持仓约束一起传入，而不是沿用纯机会发现模板。",
                "若任务是盘中发现，应优先提高实时性因子和分时型 playbook 权重，而不是继续沿用夜间筛选口味。",
            ],
            "anti_misuse_rules": [
                "不要把基础筛选模板误当成通用最终模板。",
                "不要因为命中某个任务名，就忽略市场阶段与交易周期是否匹配。",
                "不要把龙头、超跌、低位潜伏、做T这几类本质不同的任务压成一套同质组合。",
                "若组合长期不变，应先怀疑模板使用不当或任务识别失败。",
            ],
            "profile_mix_examples": [
                {
                    "name": "盘中主线走强 + 未满仓",
                    "profiles": ["intraday_discovery", "leader_playbook"],
                    "reason": "既要抓新冒头机会，也要兼顾前排龙头博弈。",
                },
                {
                    "name": "满仓后做结构优化",
                    "profiles": ["replacement_review", "day_trading"],
                    "reason": "一边评估替换，一边看现有持仓是否有日内节奏优化空间。",
                },
                {
                    "name": "普跌后的修复博弈",
                    "profiles": ["oversold_rebound", "low_level_digging"],
                    "reason": "一边做超跌修复，一边筛预期差和低位启动。",
                },
            ],
            "task_profile_usage": [
                "先选任务画像，再填 objective/market_hypothesis/playbooks/factors/weights。",
                "不要把基础筛选的组合硬套到龙头博弈、做T、超跌反弹等完全不同任务上。",
                "若当前输出长期不变，先怀疑任务画像没切换，而不是默认市场没有新机会。",
                "任务画像是建议模板，不是硬编码菜单；必要时可以多画像混编或完全自定义。",
            ],
            "supplemental_skill_pack": {
                "positioning": "外挂技能包，只作为辅助研究、监控、回测和交叉验证能力，不替代 agent 自组组合与程序主链事实。",
                "usage_rules": [
                    "先用本项目能力形成主判断，再用 skill pack 做补充、对照和交叉验证。",
                    "skill pack 不直接决定最终候选，不越权替代 runtime compose、discussion、risk_gate。",
                    "若 skill 输出与 agent 自组结论冲突，应显式回报差异和采纳理由。",
                ],
                "skills": [
                    {
                        "id": "policy_monitor",
                        "name": "Policy-Monitor",
                        "purpose": "抓政策、监管、行业新闻并提炼影响",
                        "best_for": ["政策催化", "监管变化", "行业主题扩散"],
                    },
                    {
                        "id": "stock_analyst",
                        "name": "Stock-Analyst",
                        "purpose": "做个股多维快速体检",
                        "best_for": ["陌生股票快检", "候选池外票体检", "和主链分析做对照"],
                    },
                    {
                        "id": "daily_trade_review",
                        "name": "Daily-Trade-Review",
                        "purpose": "盘后复盘、归因和操作回看",
                        "best_for": ["盘后学习", "错失机会复盘", "行为纠偏"],
                    },
                    {
                        "id": "quant_kb",
                        "name": "Quant-KB",
                        "purpose": "量化知识库与战法参考",
                        "best_for": ["策略思路补充", "战法对照", "参数灵感来源"],
                    },
                    {
                        "id": "stock_watcher",
                        "name": "Stock-Watcher",
                        "purpose": "监控自选股异动与突破",
                        "best_for": ["盘中盯盘", "持仓异动", "机会票二次提醒"],
                    },
                    {
                        "id": "ashares_data",
                        "name": "A-Shares-Data",
                        "purpose": "补充 A 股基础与历史数据视角",
                        "best_for": ["历史对照", "财务排雷", "非主链数据补充"],
                    },
                    {
                        "id": "report_extractor",
                        "name": "Report-Extractor",
                        "purpose": "提炼研报、财报和公告",
                        "best_for": ["研报速读", "公告摘要", "研究证据补丁"],
                    },
                    {
                        "id": "risk_alert_system",
                        "name": "Risk-Alert-System",
                        "purpose": "回撤、利空和市场异动预警",
                        "best_for": ["持仓风险监测", "突发利空", "盘中安全提醒"],
                    },
                    {
                        "id": "backtest_engine",
                        "name": "Backtest-Engine",
                        "purpose": "对自组组合做最小回测和对照验证",
                        "best_for": ["参数比较", "战法对照", "盘后学习反馈"],
                    },
                    {
                        "id": "skill_vetter",
                        "name": "Skill-Vetter",
                        "purpose": "检查 skill 权限与安全边界",
                        "best_for": ["外挂技能审计", "权限边界核查", "数据外发风险控制"],
                    },
                ],
                "comparison_method": [
                    "主方案: agent 基于 runtime/factors/playbooks/market_context 自组组合。",
                    "参考方案: 调用 skill pack 产出补充观点或旁路评分。",
                    "输出时同时给出一致点、冲突点、最终采纳理由。",
                ],
            },
            "learned_asset_flow": {
                "entry_status": "draft",
                "review_status": "review_required",
                "promotion_targets": ["experimental", "active", "rejected", "archived"],
                "advice_endpoints": {
                    "ingest": "/runtime/learned-assets/advice/ingest",
                    "queue": "/runtime/learned-assets/advice",
                    "resolve": "/runtime/learned-assets/advice/resolve",
                },
                "advice_governance_rules": [
                    "advice ingest 只负责把盘后建议入队，不会自动改学习产物状态",
                    "advice resolve 默认只更新队列处理结果，只有显式 apply_transition=true 才会尝试正式流转",
                    "若 advice resolve 触发 target_status=active，仍必须满足 discussion/risk/audit 审批上下文与正式引用校验",
                ],
                "active_requirements": {
                    "discussion_passed": True,
                    "risk_passed": True,
                    "audit_passed": True,
                    "discussion_case_id": "required",
                    "risk_gate": "allow|clear|pass",
                    "audit_gate": "clear|pass",
                },
                "agent_usage_rules": [
                    "draft/review_required 的 learned asset 只能注册、评审、回看，不得进入主链加权",
                    "active learned asset 可由 Agent 显式引用，也可在明确开启 auto_apply_active 时由 runtime 自动吸附",
                    "自动吸附不应长期常开，只有当市场假设、战法组合和主题偏好与历史学习产物明显贴合时才建议开启",
                    "若启用自动吸附，Agent 必须解释吸附原因、吸附到哪些资产、它们改变了哪些排序或偏置",
                ],
                "repository_runtime_modes": {
                    "default": "默认可消费，适合作为日常主链",
                    "explicit_only": "实验/灰度资产，只应在 Agent 明确知道原因时显式点名使用",
                    "auto_or_explicit": "active 学习产物，可显式引用，也可通过 auto_apply_active 吸附",
                    "governance_only": "仍在治理链，仅可评审或回看，不可进入主链",
                    "blocked": "已下线/拒绝，不可使用",
                },
                "version_selection_rules": [
                    "同一资产若存在多个版本，优先选择 default_enabled=true 的版本作为推荐主链版本",
                    "若不存在 default 版本，则退化为 explicit_enabled=true 的最新版本作为灰度候选",
                    "blocked 版本只保留在版本视图中用于回看，不应进入 compose 主链",
                    "若某版本已有真实 adoption/outcome 记录，则优先用真实结果覆盖纯状态推荐",
                    "第一阶段只提供推荐切流信息，不自动修改仓库状态或 compose 默认版本",
                ],
                "race_governance_actions": {
                    "observe_only": "暂无真实结果，继续观察",
                    "maintain_default": "真实结果与默认推荐一致，维持当前默认版本",
                    "review_cutover": "真实结果与默认推荐冲突，建议进入切流评审",
                },
                "race_panel_usage": [
                    "可通过 /runtime/strategy-repository/panel 查看高关注切流评审项",
                    "panel 只提供治理建议，不会自动修改默认版本或仓库状态",
                ],
                "discussion_binding_fields": [
                    "case_id",
                    "trade_date",
                    "symbol",
                    "final_status",
                    "risk_gate",
                    "audit_gate",
                    "trade_date_summary",
                    "reason_board_summary",
                    "vote_detail",
                    "reply_pack_summary",
                    "final_brief_summary",
                    "finalize_packet_summary",
                ],
            },
            "compose_request_schema": {
                "intent": ["mode", "objective", "market_hypothesis", "trade_horizon"],
                "universe": ["scope", "symbol_pool", "sector_whitelist", "sector_blacklist", "source"],
                "strategy": ["playbooks", "factors", "ranking"],
                "constraints": ["hard_filters", "user_preferences", "market_rules", "position_rules", "risk_rules", "execution_barriers"],
                "learned_asset_options": ["auto_apply_active", "max_auto_apply", "preferred_tags", "blocked_asset_ids"],
                "repository_query": ["asset_type", "asset_id", "status", "runtime_mode"],
                "repository_panels": ["/runtime/strategy-repository/panel"],
                "output": [
                    "max_candidates",
                    "include_filtered_reasons",
                    "include_score_breakdown",
                    "include_evidence",
                    "include_counter_evidence",
                    "return_mode",
                ],
            },
            "compose_response_contract": {
                "market_summary": ["market_regime", "hot_sectors", "risk_flags"],
                "candidates": [
                    "symbol",
                    "name",
                    "rank",
                    "action_hint",
                    "composite_score",
                    "playbook_fit",
                    "factor_scores",
                    "evidence",
                    "counter_evidence",
                    "risk_flags",
                    "positioning_hint",
                    "learned_asset_adjustments",
                    "raw_runtime_decision",
                ],
                "proposal_packet": ["selected_symbols", "watchlist_symbols", "discussion_focus"],
                "composition_manifest": [
                    "intent",
                    "universe",
                    "playbooks",
                    "factors",
                    "ranking",
                    "filters",
                    "evidence",
                    "proposal_packet",
                ],
                "applied_constraints": ["hard_filters", "market_rules", "position_rules", "risk_rules", "execution_barriers", "summary_lines"],
                "evaluation_trace": ["stored", "trace_id"],
                "repository": [
                    "used_assets",
                    "learned_assets",
                    "active_learned_assets",
                    "auto_selected_learned_assets",
                    "repository_summary",
                    "version_view",
                    "version_view[*].race_summary",
                    "version_view[*].governance_suggestion",
                    "governance_summary",
                ],
            },
            "compose_from_brief_response_contract": {
                "brief": [
                    "intent_mode",
                    "objective",
                    "market_hypothesis",
                    "playbooks",
                    "factors",
                    "playbook_specs",
                    "factor_specs",
                    "custom_constraints",
                    "market_context",
                ],
                "compose_request": ["intent", "universe", "strategy", "constraints", "output"],
                "brief_execution": [
                    "custom_profile_independent",
                    "used_reference_profile",
                    "profile_dependency",
                    "validated_compose_request",
                    "selected_playbooks",
                    "selected_factors",
                    "ranking",
                    "constraint_sections",
                    "market_context_keys",
                ],
            },
            "playbook_notes": {
                "trend_acceleration": "更适合主升阶段，已接动能/量能/动作联合评估",
                "weak_to_strong_intraday": "更适合盘中前排转强，已接前排/动能/量能联合评估",
                "sector_resonance": "更适合热点板块共振，已接热点命中/前排/量能联合评估",
                "position_replacement": "更适合持仓替换场景，已接持仓存在/前排/基础分联合评估",
                "tail_close_ambush": "更适合尾盘潜伏，已接交易周期/动作/量能联合评估",
            },
            "constraint_pack_notes": {
                "hard_filters": "硬过滤，适合停牌、ST、黑名单、涨跌停不可交易等围栏",
                "user_preferences": "用户偏好，适合行业排除、单票金额、仓位上限等柔性约束",
                "market_rules": "市场规则，适合限制 regime、最低 regime_score、是否接受 inferred 市场画像",
                "position_rules": "持仓规则，适合替换仓位、最低持仓、换手偏好等",
                "risk_rules": "风险规则，适合波动阈值、回撤上限、审计前置条件等",
                "execution_barriers": "执行前围栏，适合声明快照新鲜度、价格偏离等执行前条件；缺少快照字段时先保留为声明约束",
            },
            "evaluation_ledger_notes": {
                "record_endpoint": "/runtime/evaluations",
                "feedback_endpoint": "/runtime/evaluations/feedback",
                "reconcile_endpoint": "/runtime/evaluations/reconcile",
                "reconcile_outcome_endpoint": "/runtime/evaluations/reconcile-outcome",
                "panel_endpoint": "/runtime/evaluations/panel",
                "tracked_fields": [
                    "request/intention/strategy/constraints",
                    "market_summary",
                    "candidates/filtered_out",
                    "proposal_packet",
                    "adoption",
                    "outcome",
                    "outcome.learning_bridge",
                    "outcome.learning_bridge.learned_assets",
                    "outcome.learning_bridge.registry_weights",
                ],
            },
            "compose_request_example": {
                "request_id": "compose-example-001",
                "account_id": "8890130545",
                "intent": {
                    "mode": "opportunity_scan",
                    "objective": "寻找主升阶段的趋势加速机会",
                    "market_hypothesis": "热点板块扩散，优先趋势加速与板块共振",
                    "trade_horizon": "intraday_to_overnight",
                },
                "universe": {
                    "scope": "main-board",
                    "symbol_pool": [],
                    "sector_whitelist": ["机器人"],
                    "sector_blacklist": ["银行"],
                    "source": "runtime_compose",
                },
                "strategy": {
                    "playbooks": [{"id": "trend_acceleration", "version": "v1", "weight": 0.7, "params": {}}],
                    "factors": [{"id": "momentum_slope", "group": "momentum", "version": "v1", "weight": 0.3, "params": {}}],
                    "ranking": {"primary_score": "composite_score", "secondary_keys": ["momentum_slope"]},
                },
                "constraints": {
                    "market_rules": {
                        "allowed_regimes": ["trend", "rotation"],
                        "min_regime_score": 0.5,
                        "allow_inferred_market_profile": True,
                    },
                    "risk_rules": {
                        "max_total_position": 0.5,
                        "max_single_position": 0.2,
                        "daily_loss_limit": 0.03,
                        "emergency_stop": False,
                    },
                    "execution_barriers": {
                        "require_fresh_snapshot": True,
                        "max_snapshot_age_seconds": 180,
                        "max_price_deviation_pct": 0.02,
                    },
                    "user_preferences": {
                        "excluded_theme_keywords": ["银行"],
                        "max_single_amount": 20000,
                        "equity_position_limit": 0.3,
                    }
                },
                "learned_asset_options": {
                    "auto_apply_active": True,
                    "max_auto_apply": 2,
                    "preferred_tags": ["trend", "sector-rotation"],
                    "blocked_asset_ids": ["deprecated_combo_x"],
                },
                "output": {"max_candidates": 12, "return_mode": "proposal_ready"},
            },
            "compose_brief_example": {
                "request_id": "compose-brief-example-001",
                "account_id": "8890130545",
                "objective": "围绕热点扩散寻找可讨论的新机会票",
                "market_hypothesis": "机器人与泛科技活跃，回避低弹性方向",
                "trade_horizon": "intraday_to_overnight",
                "auto_apply_active_learned_assets": True,
                "max_auto_apply_learned_assets": 2,
                "preferred_learned_asset_tags": ["trend", "rotation"],
                "blocked_learned_asset_ids": ["deprecated_combo_x"],
                "focus_sectors": ["机器人", "AI算力"],
                "avoid_sectors": ["银行"],
                "excluded_theme_keywords": ["银行"],
                "allowed_regimes": ["trend", "rotation"],
                "blocked_regimes": ["defensive"],
                "min_regime_score": 0.5,
                "allow_inferred_market_profile": True,
                "holding_symbols": ["600166.SH"],
                "playbooks": ["trend_acceleration", "sector_resonance"],
                "factors": ["momentum_slope", "sector_heat_score", "breakout_quality"],
                "playbook_versions": {"trend_acceleration": "v1", "sector_resonance": "v1"},
                "factor_versions": {"momentum_slope": "v1", "sector_heat_score": "v1", "breakout_quality": "v1"},
                "weights": {
                    "playbooks": {"trend_acceleration": 0.6, "sector_resonance": 0.4},
                    "factors": {"momentum_slope": 0.4, "sector_heat_score": 0.35, "breakout_quality": 0.25},
                },
                "max_candidates": 8,
                "max_single_amount": 20000,
                "equity_position_limit": 0.3,
                "max_total_position": 0.5,
                "max_single_position": 0.2,
                "daily_loss_limit": 0.03,
                "emergency_stop": False,
                "blocked_symbols": ["600000.SH"],
                "require_fresh_snapshot": True,
                "max_snapshot_age_seconds": 180,
                "max_price_deviation_pct": 0.02,
            },
        }

    @router.post("/learned-assets/transition")
    async def transition_learned_asset(payload: dict):
        asset_id = str(payload.get("asset_id") or "").strip()
        version = str(payload.get("version") or "v1").strip()
        target_status = str(payload.get("target_status") or "").strip()
        operator = str(payload.get("operator") or "system").strip()
        note = str(payload.get("note") or "").strip()
        evaluation_summary = payload.get("evaluation_summary") or {}
        approval_context = payload.get("approval_context") or {}
        if not asset_id or not target_status:
            return {
                "ok": False,
                "error": "asset_id 与 target_status 必填",
            }
        try:
            entry = learned_asset_service.transition(
                asset_id=asset_id,
                version=version,
                target_status=target_status,  # type: ignore[arg-type]
                operator=operator,
                note=note,
                evaluation_summary=evaluation_summary,
                approval_context=approval_context,
            )
            return {
                "ok": True,
                "item": entry.model_dump(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    @router.get("/learned-assets/approvals")
    async def get_learned_asset_approvals(limit: int = 20):
        return {
            "ok": True,
            "items": learned_asset_service.recent_approvals(limit=limit),
        }

    @router.get("/learned-assets/panel")
    async def get_learned_asset_panel(limit: int = 20):
        panel = learned_asset_service.build_panel(limit=limit)
        return {
            "ok": True,
            **panel,
        }

    @router.get("/learned-assets/advice")
    async def get_learned_asset_advice_queue(limit: int = 20, status: str | None = None):
        return {
            "ok": True,
            "items": learned_asset_service.list_advice_queue(limit=limit, status=status),
        }

    @router.post("/learned-assets/advice/ingest")
    async def ingest_learned_asset_advice(payload: dict):
        trace_id = str(payload.get("trace_id") or "").strip()
        operator = str(payload.get("operator") or "system").strip()
        asset_id = str(payload.get("asset_id") or "").strip()
        if not trace_id:
            return {"ok": False, "error": "trace_id 必填"}
        try:
            record = evaluation_ledger_service.get_record(trace_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        outcome = dict(record.get("outcome") or {})
        learning_bridge = dict(outcome.get("learning_bridge") or {})
        learned_assets = dict(learning_bridge.get("learned_assets") or {})
        items = list(learned_assets.get("items") or [])
        if asset_id:
            items = [item for item in items if str(item.get("id") or "").strip() == asset_id]
        queued: list[dict] = []
        for item in items:
            advice = dict(item.get("advice") or {})
            if not advice:
                continue
            try:
                queued.append(
                    learned_asset_service.stage_advice(
                        asset_id=str(item.get("id") or "").strip(),
                        version=str(item.get("version") or "v1").strip(),
                        trace_id=trace_id,
                        operator=operator,
                        advice=advice,
                        note=str(payload.get("note") or "").strip(),
                    )
                )
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "trace_id": trace_id,
            "queued_count": len(queued),
            "items": queued,
        }

    @router.post("/learned-assets/advice/resolve")
    async def resolve_learned_asset_advice(payload: dict):
        queue_id = str(payload.get("queue_id") or "").strip()
        resolution_status = str(payload.get("resolution_status") or "").strip()
        operator = str(payload.get("operator") or "system").strip()
        note = str(payload.get("note") or "").strip()
        apply_transition = bool(payload.get("apply_transition"))
        raw_target_status = payload.get("target_status")
        target_status = str(raw_target_status).strip() if raw_target_status is not None else None
        evaluation_summary = payload.get("evaluation_summary") or {}
        approval_context = payload.get("approval_context") or {}
        if not queue_id or not resolution_status:
            return {"ok": False, "error": "queue_id 与 resolution_status 必填"}
        try:
            resolved_item = learned_asset_service.resolve_advice(
                queue_id=queue_id,
                resolution_status=resolution_status,
                operator=operator,
                note=note,
                apply_transition=apply_transition,
                target_status=target_status,  # type: ignore[arg-type]
                evaluation_summary=evaluation_summary,
                approval_context=approval_context,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "item": resolved_item,
        }

    @router.get("/evaluations")
    async def get_compose_evaluations(limit: int = 20, agent_id: str | None = None):
        return {
            "ok": True,
            "items": evaluation_ledger_service.list_records(limit=limit, agent_id=agent_id),
        }

    @router.get("/evaluations/panel")
    async def get_compose_evaluations_panel(limit: int = 20):
        panel = evaluation_ledger_service.build_panel(limit=limit)
        return {
            "ok": True,
            **panel,
        }

    @router.post("/evaluations/feedback")
    async def update_compose_evaluation_feedback(payload: dict):
        trace_id = str(payload.get("trace_id") or "").strip()
        if not trace_id:
            return {
                "ok": False,
                "error": "trace_id 必填",
            }
        try:
            item = evaluation_ledger_service.update_feedback(
                trace_id=trace_id,
                adoption=(payload.get("adoption") or None),
                outcome=(payload.get("outcome") or None),
            )
            return {
                "ok": True,
                "item": item,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    @router.post("/evaluations/reconcile")
    async def reconcile_compose_evaluation(payload: dict):
        trace_id = str(payload.get("trace_id") or "").strip()
        if not trace_id:
            return {
                "ok": False,
                "error": "trace_id 必填",
            }
        try:
            item = evaluation_ledger_service.reconcile_adoption(
                trace_id=trace_id,
                updated_by=str(payload.get("updated_by") or "runtime:reconcile"),
            )
            return {
                "ok": True,
                "item": item,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    @router.post("/evaluations/reconcile-outcome")
    async def reconcile_compose_evaluation_outcome(payload: dict):
        trace_id = str(payload.get("trace_id") or "").strip()
        if not trace_id:
            return {
                "ok": False,
                "error": "trace_id 必填",
            }
        try:
            item = evaluation_ledger_service.reconcile_outcome(
                trace_id=trace_id,
                account_id=str(payload.get("account_id") or "").strip() or None,
                sync_registry_weights=bool(payload.get("sync_registry_weights", False)),
                updated_by=str(payload.get("updated_by") or "runtime:reconcile_outcome"),
            )
            return {
                "ok": True,
                "item": item,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    @router.post("/jobs/compose-from-brief")
    async def run_compose_from_brief(brief: RuntimeComposeBriefRequest):
        try:
            compose_request = _build_compose_request_from_brief(brief)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }
        payload = await run_compose(compose_request)
        payload["ok"] = True
        payload["brief"] = {
            "intent_mode": brief.intent_mode,
            "objective": brief.objective,
            "market_hypothesis": brief.market_hypothesis,
            "focus_sectors": brief.focus_sectors,
            "avoid_sectors": brief.avoid_sectors,
            "excluded_theme_keywords": brief.excluded_theme_keywords,
            "blocked_symbols": brief.blocked_symbols,
            "allowed_regimes": brief.allowed_regimes,
            "blocked_regimes": brief.blocked_regimes,
            "playbooks": brief.playbooks,
            "factors": brief.factors,
            "playbook_specs": [item.model_dump() for item in brief.playbook_specs],
            "factor_specs": [item.model_dump() for item in brief.factor_specs],
            "playbook_versions": brief.playbook_versions,
            "factor_versions": brief.factor_versions,
            "notes": brief.notes,
            "custom_constraints": brief.custom_constraints.model_dump(),
            "market_context": brief.market_context,
        }
        payload["compose_request"] = compose_request.model_dump()
        payload["brief_execution"] = _build_brief_execution_summary(brief, compose_request)
        return payload

    @router.post("/jobs/compose")
    async def run_compose(request: RuntimeComposeRequest):
        try:
            for playbook in request.strategy.playbooks:
                entry = _resolve_repository_strategy_entry(
                    asset_type="playbook",
                    asset_id=playbook.id,
                    version=playbook.version,
                )
                if entry is not None:
                    _assert_runtime_strategy_access(entry, allow_explicit_only=True)
            for factor in request.strategy.factors:
                entry = _resolve_repository_strategy_entry(
                    asset_type="factor",
                    asset_id=factor.id,
                    version=factor.version,
                )
                if entry is not None:
                    _assert_runtime_strategy_access(entry, allow_explicit_only=True)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }
        repository_payload = _register_compose_assets(request)
        base_runtime_config = _runtime_config()
        resolved_constraints = resolve_constraint_pack(request.constraints.model_dump(), base_runtime_config)
        runtime_config = _runtime_config_with_overrides(resolved_constraints.runtime_overrides(base_runtime_config))
        constraint_summary = resolved_constraints.summary(runtime_config)
        pipeline_request = RuntimeJobRequest(
            symbols=request.universe.symbol_pool,
            universe_scope=request.universe.scope,
            max_candidates=request.output.max_candidates,
            auto_trade=False,
            account_id=request.account_id,
            source=_infer_pipeline_source_from_compose_request(request),
        )
        pipeline_payload = await run_pipeline(pipeline_request)
        constrained_top_picks, extra_filtered = apply_constraint_pack(
            list(pipeline_payload.get("top_picks", [])),
            resolved_constraints,
            market_profile=pipeline_payload.get("market_profile"),
        )
        pipeline_payload["top_picks"] = constrained_top_picks
        selection_preferences = dict(pipeline_payload.get("selection_preferences") or {})
        selection_preferences["excluded_candidates"] = list(selection_preferences.get("excluded_candidates") or []) + extra_filtered
        selection_preferences["constraint_pack"] = constraint_summary
        pipeline_payload["selection_preferences"] = selection_preferences
        
        # 获取市场驱动上下文 (R0.4)
        event_context = serving_store.get_latest_event_context()
        monitor_context = serving_store.get_latest_monitor_context()
        
        compose_output = strategy_composer.build_output(
            request,
            pipeline_payload,
            runtime_config,
            constraint_trace=constraint_summary,
            learned_asset_entries=list(repository_payload.get("learned_assets") or []),
            event_context=event_context,
            monitor_context=monitor_context,
            market_adapter=market_adapter,
        )
        trace_id = f"compose-{pipeline_payload.get('job_id', uuid4().hex[:10])}"
        evaluation_ledger_service.record_compose_evaluation(
            trace_id=trace_id,
            request_payload=request.model_dump(),
            runtime_job={
                "pipeline_job_id": pipeline_payload.get("job_id"),
                "case_ids": pipeline_payload.get("case_ids", []),
                "case_count": pipeline_payload.get("case_count", 0),
            },
            market_summary=compose_output["market_summary"],
            candidates=compose_output["candidates"],
            filtered_out=compose_output["filtered_out"],
            proposal_packet=compose_output["proposal_packet"],
            applied_constraints=constraint_summary,
            repository=repository_payload,
            generated_at=str(pipeline_payload.get("generated_at") or datetime.now().isoformat()),
        )
        composition_manifest = _build_composition_manifest(
            request,
            constraint_summary=constraint_summary,
            compose_output=compose_output,
        )
        return {
            "job_id": pipeline_payload.get("job_id"),
            "status": "completed",
            "request_id": request.request_id or f"compose-{uuid4().hex[:10]}",
            "generated_at": pipeline_payload.get("generated_at"),
            "agent": request.agent.model_dump(),
            "intent": request.intent.model_dump(),
            "market_summary": compose_output["market_summary"],
            "candidates": compose_output["candidates"],
            "filtered_out": compose_output["filtered_out"],
            "explanations": compose_output["explanations"],
            "proposal_packet": compose_output["proposal_packet"],
            "composition_manifest": composition_manifest,
            "applied_constraints": constraint_summary,
            "evaluation_trace": {
                "stored": True,
                "trace_id": trace_id,
            },
            "repository": repository_payload,
            "runtime_job": {
                "pipeline_job_id": pipeline_payload.get("job_id"),
                "case_ids": pipeline_payload.get("case_ids", []),
                "case_count": pipeline_payload.get("case_count", 0),
            },
        }

    def _run_pipeline_sync(request: RuntimeJobRequest) -> dict:
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

        early_sector_map = _resolve_symbol_sector_map([], [item["symbol"] for item in decisions if item.get("symbol")])
        decisions, excluded_candidates, excluded_theme_keywords = _filter_top_picks_by_preferences(
            decisions,
            runtime_config=runtime_config,
            sector_map=early_sector_map,
        )

        job_id = f"runtime-{uuid4().hex[:10]}"
        report = {
            "job_id": job_id,
            "job_type": "pipeline",
            "source": str(request.source or "market_universe_scan").strip() or "market_universe_scan",
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
            "selection_preferences": {
                "excluded_theme_keywords": excluded_theme_keywords,
                "excluded_candidates": excluded_candidates,
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
                "selected_symbols": [item["symbol"] for item in report["top_picks"]],
                "decision_count": len(report["top_picks"]),
                "buy_count": report["summary"]["buy_count"],
                "universe_scope": request.universe_scope,
                "source": report["source"],
            },
        )
        return report

    def _unique_symbols(symbols: list[str]) -> list[str]:
        return [str(symbol) for symbol in dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip())]

    def _resolve_news_catalyst_symbols(request: RuntimeJobRequest) -> tuple[list[str], dict[str, Any]]:
        explicit_symbols = _unique_symbols(request.symbols)
        if explicit_symbols:
            return explicit_symbols, {"mode": "explicit_symbols", "source_symbol_count": len(explicit_symbols)}

        event_context = serving_store.get_latest_event_context() or {}
        candidates: list[str] = []
        catalyst_items: list[dict[str, Any]] = []
        for key in ("highlights", "items", "events"):
            for raw in list(event_context.get(key) or []):
                if not isinstance(raw, dict):
                    continue
                symbol = str(raw.get("symbol") or "").strip()
                if not symbol:
                    continue
                sentiment = str(raw.get("sentiment") or raw.get("impact") or "").strip().lower()
                severity = str(raw.get("severity") or "").strip().lower()
                tags = " ".join(str(item).lower() for item in list(raw.get("tags") or []))
                title = str(raw.get("title") or raw.get("headline") or raw.get("summary") or "")
                if sentiment in {"positive", "bullish", "support"} or "positive" in tags or severity in {"high", "critical"}:
                    candidates.append(symbol)
                    catalyst_items.append({"symbol": symbol, "title": title, "sentiment": sentiment or severity})
        resolved = _unique_symbols(candidates)
        return resolved, {
            "mode": "event_context",
            "source_symbol_count": len(resolved),
            "catalyst_items": catalyst_items[:5],
            "trade_date": str(event_context.get("trade_date") or ""),
        }

    def _resolve_tail_ambush_symbols(request: RuntimeJobRequest) -> tuple[list[str], dict[str, Any]]:
        explicit_symbols = _unique_symbols(request.symbols)
        if explicit_symbols:
            return explicit_symbols, {"mode": "explicit_symbols", "source_symbol_count": len(explicit_symbols)}

        runtime_context = serving_store.get_latest_runtime_context() or {}
        monitor_context = serving_store.get_latest_monitor_context() or {}
        candidates: list[str] = []
        for item in list(runtime_context.get("top_picks") or []):
            if isinstance(item, dict):
                candidates.append(str(item.get("symbol") or ""))
        candidates.extend(str(item) for item in list(runtime_context.get("selected_symbols") or []))
        for raw in list(monitor_context.get("recent_events") or []):
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol") or "").strip()
            if not symbol:
                continue
            change_pct = float(raw.get("change_pct", 0.0) or 0.0)
            severity = str(raw.get("severity") or "").strip().lower()
            if change_pct > 0 or severity in {"info", "warning"}:
                candidates.append(symbol)
        resolved = _unique_symbols(candidates)
        return resolved, {
            "mode": "runtime_context",
            "source_symbol_count": len(resolved),
            "runtime_trade_date": str(runtime_context.get("trade_date") or ""),
            "runtime_job_id": str(runtime_context.get("job_id") or ""),
        }

    @router.post("/jobs/pipeline")
    async def run_pipeline(request: RuntimeJobRequest):
        return await run_in_threadpool(lambda: _run_pipeline_sync(request))

    @router.post("/jobs/intraday")
    async def run_intraday(request: RuntimeJobRequest):
        intraday_request = request.model_copy(
            update={
                "symbols": request.symbols,
                "universe_scope": "custom",
                "source": "intraday_t_scan",
            }
        )
        return await run_in_threadpool(lambda: _run_pipeline_sync(intraday_request))

    @router.post("/jobs/news-catalyst")
    async def run_news_catalyst(request: RuntimeJobRequest):
        resolved_symbols, source_context = _resolve_news_catalyst_symbols(request)
        catalyst_request = request.model_copy(
            update={
                "symbols": resolved_symbols,
                "universe_scope": "custom" if resolved_symbols else request.universe_scope,
                "source": "news_catalyst_scan",
            }
        )
        result = await run_in_threadpool(lambda: _run_pipeline_sync(catalyst_request))
        result["source_context"] = source_context
        return result

    @router.post("/jobs/tail-ambush")
    async def run_tail_ambush(request: RuntimeJobRequest):
        resolved_symbols, source_context = _resolve_tail_ambush_symbols(request)
        ambush_request = request.model_copy(
            update={
                "symbols": resolved_symbols,
                "universe_scope": "custom" if resolved_symbols else request.universe_scope,
                "source": "tail_ambush_scan",
            }
        )
        result = await run_in_threadpool(lambda: _run_pipeline_sync(ambush_request))
        result["source_context"] = source_context
        return result

    @router.post("/jobs/autotrade")
    async def run_autotrade(request: RuntimeJobRequest):
        auto_request = request.model_copy(update={"auto_trade": True})
        result = await run_in_threadpool(lambda: _run_pipeline_sync(auto_request))
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
