"""Runtime compose 请求契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RuntimeComposeAgentRef(BaseModel):
    agent_id: str = "ashare"
    role: str = "controller"
    session_id: str = ""
    proposal_id: str = ""


class RuntimeComposeIntent(BaseModel):
    mode: str = "opportunity_scan"
    objective: str = ""
    market_hypothesis: str = ""
    trade_horizon: str = ""


class RuntimeComposeUniverse(BaseModel):
    scope: str = "main-board"
    symbol_pool: list[str] = Field(default_factory=list)
    sector_whitelist: list[str] = Field(default_factory=list)
    sector_blacklist: list[str] = Field(default_factory=list)
    source: str = "runtime_compose"


class RuntimeComposePlaybookSpec(BaseModel):
    id: str
    version: str = "v1"
    weight: float = 0.0
    params: dict = Field(default_factory=dict)


class RuntimeComposeFactorSpec(BaseModel):
    id: str
    group: str = ""
    version: str = "v1"
    weight: float = 0.0
    params: dict = Field(default_factory=dict)


class RuntimeComposeRanking(BaseModel):
    primary_score: str = "composite_score"
    secondary_keys: list[str] = Field(default_factory=list)


class RuntimeComposeStrategy(BaseModel):
    playbooks: list[RuntimeComposePlaybookSpec] = Field(default_factory=list)
    factors: list[RuntimeComposeFactorSpec] = Field(default_factory=list)
    ranking: RuntimeComposeRanking = Field(default_factory=RuntimeComposeRanking)


class RuntimeComposeConstraints(BaseModel):
    hard_filters: dict = Field(default_factory=dict)
    user_preferences: dict = Field(default_factory=dict)
    market_rules: dict = Field(default_factory=dict)
    position_rules: dict = Field(default_factory=dict)
    risk_rules: dict = Field(default_factory=dict)
    execution_barriers: dict = Field(default_factory=dict)


class RuntimeComposeOutput(BaseModel):
    max_candidates: int = Field(default=12, ge=1, le=50)
    include_filtered_reasons: bool = True
    include_score_breakdown: bool = True
    include_evidence: bool = True
    include_counter_evidence: bool = True
    return_mode: str = "proposal_ready"


class RuntimeComposeLearnedAsset(BaseModel):
    id: str
    name: str
    type: Literal["playbook", "template", "learned_combo"] = "learned_combo"
    version: str = "v1"
    source: str = "agent_learning"
    params_schema: dict = Field(default_factory=dict)
    evidence_schema: dict = Field(default_factory=dict)
    risk_notes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    content: dict = Field(default_factory=dict)


class RuntimeComposeLearnedAssetOptions(BaseModel):
    auto_apply_active: bool = False
    max_auto_apply: int = Field(default=2, ge=0, le=10)
    preferred_tags: list[str] = Field(default_factory=list)
    blocked_asset_ids: list[str] = Field(default_factory=list)


class RuntimeComposeRequest(BaseModel):
    request_id: str = ""
    account_id: str = "8890130545"
    agent: RuntimeComposeAgentRef = Field(default_factory=RuntimeComposeAgentRef)
    intent: RuntimeComposeIntent = Field(default_factory=RuntimeComposeIntent)
    universe: RuntimeComposeUniverse = Field(default_factory=RuntimeComposeUniverse)
    strategy: RuntimeComposeStrategy = Field(default_factory=RuntimeComposeStrategy)
    constraints: RuntimeComposeConstraints = Field(default_factory=RuntimeComposeConstraints)
    market_context: dict = Field(default_factory=dict)
    output: RuntimeComposeOutput = Field(default_factory=RuntimeComposeOutput)
    learned_assets: list[RuntimeComposeLearnedAsset] = Field(default_factory=list)
    learned_asset_options: RuntimeComposeLearnedAssetOptions = Field(default_factory=RuntimeComposeLearnedAssetOptions)


class RuntimeComposeBriefWeights(BaseModel):
    playbooks: dict[str, float] = Field(default_factory=dict)
    factors: dict[str, float] = Field(default_factory=dict)


class RuntimeComposeBriefPlaybookSpec(BaseModel):
    id: str
    version: str = ""
    weight: float | None = None
    params: dict = Field(default_factory=dict)


class RuntimeComposeBriefFactorSpec(BaseModel):
    id: str
    group: str = ""
    version: str = ""
    weight: float | None = None
    params: dict = Field(default_factory=dict)


class RuntimeComposeBriefRequest(BaseModel):
    request_id: str = ""
    account_id: str = "8890130545"
    agent: RuntimeComposeAgentRef = Field(default_factory=RuntimeComposeAgentRef)
    intent_mode: str = "opportunity_scan"
    objective: str = ""
    market_hypothesis: str = ""
    trade_horizon: str = "intraday_to_overnight"
    universe_scope: str = "main-board"
    symbol_pool: list[str] = Field(default_factory=list)
    focus_sectors: list[str] = Field(default_factory=list)
    avoid_sectors: list[str] = Field(default_factory=list)
    excluded_theme_keywords: list[str] = Field(default_factory=list)
    holding_symbols: list[str] = Field(default_factory=list)
    playbooks: list[str] = Field(default_factory=list)
    factors: list[str] = Field(default_factory=list)
    playbook_specs: list[RuntimeComposeBriefPlaybookSpec] = Field(default_factory=list)
    factor_specs: list[RuntimeComposeBriefFactorSpec] = Field(default_factory=list)
    playbook_versions: dict[str, str] = Field(default_factory=dict)
    factor_versions: dict[str, str] = Field(default_factory=dict)
    weights: RuntimeComposeBriefWeights = Field(default_factory=RuntimeComposeBriefWeights)
    ranking_primary_score: str = "composite_score"
    ranking_secondary_keys: list[str] = Field(default_factory=list)
    auto_apply_active_learned_assets: bool = False
    max_auto_apply_learned_assets: int = Field(default=2, ge=0, le=10)
    preferred_learned_asset_tags: list[str] = Field(default_factory=list)
    blocked_learned_asset_ids: list[str] = Field(default_factory=list)
    max_candidates: int = Field(default=12, ge=1, le=50)
    max_single_amount: float | None = None
    equity_position_limit: float | None = None
    max_total_position: float | None = None
    max_single_position: float | None = None
    daily_loss_limit: float | None = None
    emergency_stop: bool = False
    blocked_symbols: list[str] = Field(default_factory=list)
    allowed_regimes: list[str] = Field(default_factory=list)
    blocked_regimes: list[str] = Field(default_factory=list)
    min_regime_score: float | None = None
    allow_inferred_market_profile: bool = True
    require_fresh_snapshot: bool = False
    max_snapshot_age_seconds: int | None = None
    max_price_deviation_pct: float | None = None
    custom_constraints: RuntimeComposeConstraints = Field(default_factory=RuntimeComposeConstraints)
    market_context: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
