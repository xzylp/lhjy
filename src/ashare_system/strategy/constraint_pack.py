"""Constraint Pack - 为 runtime compose 提供独立的约束解析与过滤能力。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..runtime_config import RuntimeConfig
from ..selection_preferences import normalize_excluded_theme_keywords
from ..execution_safety import is_limit_up, is_limit_down


def _normalize_symbol_list(items: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for item in items or []:
        value = str(item or "").strip().upper()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_prefix_list(items: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for item in items or []:
        value = str(item or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


class ResolvedConstraintPack(BaseModel):
    excluded_theme_keywords: list[str] = Field(default_factory=list)
    symbol_blacklist: list[str] = Field(default_factory=list)
    allowed_regimes: list[str] = Field(default_factory=list)
    blocked_regimes: list[str] = Field(default_factory=list)
    min_regime_score: float | None = None
    allow_inferred_market_profile: bool = True
    blocked_symbols: list[str] = Field(default_factory=list)
    max_total_position: float | None = None
    max_single_position: float | None = None
    daily_loss_limit: float | None = None
    emergency_stop: bool = False
    require_fresh_snapshot: bool = False
    max_snapshot_age_seconds: int | None = None
    max_price_deviation_pct: float | None = None
    excluded_prefixes: list[str] = Field(default_factory=list)
    holding_symbols: list[str] = Field(default_factory=list)
    avoid_existing_holdings: bool = False
    max_single_amount: float | None = None
    equity_position_limit: float | None = None
    # R0.8 新增: 板块黑白名单
    allowed_sectors: list[str] = Field(default_factory=list)
    blocked_sectors: list[str] = Field(default_factory=list)

    def runtime_overrides(self, base_config: RuntimeConfig) -> dict[str, Any]:
        return {
            "excluded_theme_keywords": ",".join(self.excluded_theme_keywords),
            "max_single_amount": (
                self.max_single_amount if self.max_single_amount is not None else base_config.max_single_amount
            ),
            "equity_position_limit": (
                self.equity_position_limit
                if self.equity_position_limit is not None
                else base_config.equity_position_limit
            ),
            "max_total_position": (
                self.max_total_position if self.max_total_position is not None else base_config.max_total_position
            ),
            "max_single_position": (
                self.max_single_position if self.max_single_position is not None else base_config.max_single_position
            ),
            "daily_loss_limit": (
                self.daily_loss_limit if self.daily_loss_limit is not None else base_config.daily_loss_limit
            ),
            "emergency_stop": self.emergency_stop if self.emergency_stop else base_config.emergency_stop,
        }

    def summary(self, runtime_config: RuntimeConfig) -> dict[str, Any]:
        return {
            "hard_filters": {
                "excluded_theme_keywords": self.excluded_theme_keywords,
                "symbol_blacklist": self.symbol_blacklist,
                "excluded_prefixes": self.excluded_prefixes,
                "allowed_sectors": self.allowed_sectors,
                "blocked_sectors": self.blocked_sectors,
            },
            "market_rules": {
                "allowed_regimes": self.allowed_regimes,
                "blocked_regimes": self.blocked_regimes,
                "min_regime_score": self.min_regime_score,
                "allow_inferred_market_profile": self.allow_inferred_market_profile,
            },
            "position_rules": {
                "holding_symbols": self.holding_symbols,
                "avoid_existing_holdings": self.avoid_existing_holdings,
                "max_single_amount": runtime_config.max_single_amount,
                "equity_position_limit": runtime_config.equity_position_limit,
            },
            "risk_rules": {
                "blocked_symbols": self.blocked_symbols,
                "max_total_position": runtime_config.max_total_position,
                "max_single_position": runtime_config.max_single_position,
                "daily_loss_limit": runtime_config.daily_loss_limit,
                "emergency_stop": runtime_config.emergency_stop,
            },
            "execution_barriers": {
                "require_fresh_snapshot": self.require_fresh_snapshot,
                "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                "max_price_deviation_pct": self.max_price_deviation_pct,
                "declarative_only_when_snapshot_missing": True,
            },
            "summary_lines": [
                "硬过滤: "
                + (
                    "无"
                    if not (self.excluded_theme_keywords or self.symbol_blacklist or self.excluded_prefixes or self.allowed_sectors or self.blocked_sectors)
                    else " / ".join(
                        [
                            *(["排除方向=" + ",".join(self.excluded_theme_keywords)] if self.excluded_theme_keywords else []),
                            *(["黑名单=" + ",".join(self.symbol_blacklist)] if self.symbol_blacklist else []),
                            *(["前缀封锁=" + ",".join(self.excluded_prefixes)] if self.excluded_prefixes else []),
                            *(["允许板块=" + ",".join(self.allowed_sectors)] if self.allowed_sectors else []),
                            *(["阻断板块=" + ",".join(self.blocked_sectors)] if self.blocked_sectors else []),
                        ]
                    )
                ),
                "市场规则: "
                + (
                    "无"
                    if not (
                        self.allowed_regimes
                        or self.blocked_regimes
                        or self.min_regime_score is not None
                        or not self.allow_inferred_market_profile
                    )
                    else " / ".join(
                        [
                            *(["允许 regime=" + ",".join(self.allowed_regimes)] if self.allowed_regimes else []),
                            *(["阻断 regime=" + ",".join(self.blocked_regimes)] if self.blocked_regimes else []),
                            *(
                                [f"最低 regime_score={self.min_regime_score}"]
                                if self.min_regime_score is not None
                                else []
                            ),
                            *(["禁止 inferred market_profile"] if not self.allow_inferred_market_profile else []),
                        ]
                    )
                ),
                "持仓规则: "
                + (
                    "无"
                    if not (self.holding_symbols or self.avoid_existing_holdings)
                    else " / ".join(
                        [
                            *(["持仓符号=" + ",".join(self.holding_symbols)] if self.holding_symbols else []),
                            *(["回避已有持仓"] if self.avoid_existing_holdings else []),
                            f"单票金额上限={runtime_config.max_single_amount}",
                            f"股票仓位上限={runtime_config.equity_position_limit}",
                        ]
                    )
                ),
                "执行前围栏: "
                + (
                    "无"
                    if not (
                        self.require_fresh_snapshot
                        or self.max_snapshot_age_seconds is not None
                        or self.max_price_deviation_pct is not None
                    )
                    else " / ".join(
                        [
                            *(["要求快照新鲜"] if self.require_fresh_snapshot else []),
                            *(
                                [f"快照最大年龄={self.max_snapshot_age_seconds}s"]
                                if self.max_snapshot_age_seconds is not None
                                else []
                            ),
                            *(
                                [f"最大价格偏离={self.max_price_deviation_pct:.2%}"]
                                if self.max_price_deviation_pct is not None
                                else []
                            ),
                            "缺少快照字段时仅声明不硬拦截",
                        ]
                    )
                ),
                "风险规则: "
                + (
                    "无"
                    if not (
                        self.blocked_symbols
                        or runtime_config.emergency_stop
                        or runtime_config.max_total_position
                        or runtime_config.max_single_position
                        or runtime_config.daily_loss_limit
                    )
                    else " / ".join(
                        [
                            *(["封锁符号=" + ",".join(self.blocked_symbols)] if self.blocked_symbols else []),
                            f"总仓位上限={runtime_config.max_total_position:.0%}",
                            f"单票仓位上限={runtime_config.max_single_position:.0%}",
                            f"日亏损上限={runtime_config.daily_loss_limit:.0%}",
                            *(["紧急停机=开启"] if runtime_config.emergency_stop else []),
                        ]
                    )
                ),
            ],
        }


def resolve_constraint_pack(constraints: dict[str, Any] | None, base_config: RuntimeConfig) -> ResolvedConstraintPack:
    payload = dict(constraints or {})
    hard_filters = dict(payload.get("hard_filters") or {})
    user_preferences = dict(payload.get("user_preferences") or {})
    market_rules = dict(payload.get("market_rules") or {})
    position_rules = dict(payload.get("position_rules") or {})
    risk_rules = dict(payload.get("risk_rules") or {})
    execution_barriers = dict(payload.get("execution_barriers") or {})
    excluded_theme_keywords = normalize_excluded_theme_keywords(
        list(user_preferences.get("excluded_theme_keywords") or [])
        + list(hard_filters.get("excluded_theme_keywords") or [])
    )
    max_single_amount = position_rules.get("max_single_amount", user_preferences.get("max_single_amount"))
    equity_position_limit = position_rules.get(
        "equity_position_limit",
        user_preferences.get("equity_position_limit"),
    )
    max_total_position = risk_rules.get("max_total_position")
    max_single_position = risk_rules.get("max_single_position")
    daily_loss_limit = risk_rules.get("daily_loss_limit")
    return ResolvedConstraintPack(
        excluded_theme_keywords=excluded_theme_keywords,
        symbol_blacklist=_normalize_symbol_list(hard_filters.get("symbol_blacklist")),
        allowed_regimes=[str(item).strip() for item in (market_rules.get("allowed_regimes") or []) if str(item).strip()],
        blocked_regimes=[str(item).strip() for item in (market_rules.get("blocked_regimes") or []) if str(item).strip()],
        min_regime_score=(
            float(market_rules.get("min_regime_score"))
            if market_rules.get("min_regime_score") is not None
            else None
        ),
        allow_inferred_market_profile=bool(market_rules.get("allow_inferred_market_profile", True)),
        blocked_symbols=_normalize_symbol_list(risk_rules.get("blocked_symbols")),
        max_total_position=float(max_total_position) if max_total_position is not None else None,
        max_single_position=float(max_single_position) if max_single_position is not None else None,
        daily_loss_limit=float(daily_loss_limit) if daily_loss_limit is not None else None,
        emergency_stop=bool(risk_rules.get("emergency_stop")),
        require_fresh_snapshot=bool(execution_barriers.get("require_fresh_snapshot")),
        max_snapshot_age_seconds=(
            int(execution_barriers.get("max_snapshot_age_seconds"))
            if execution_barriers.get("max_snapshot_age_seconds") is not None
            else None
        ),
        max_price_deviation_pct=(
            float(execution_barriers.get("max_price_deviation_pct"))
            if execution_barriers.get("max_price_deviation_pct") is not None
            else None
        ),
        excluded_prefixes=_normalize_prefix_list(hard_filters.get("excluded_prefixes")),
        holding_symbols=_normalize_symbol_list(position_rules.get("holding_symbols")),
        avoid_existing_holdings=bool(position_rules.get("avoid_existing_holdings")),
        max_single_amount=float(max_single_amount) if max_single_amount is not None else None,
        equity_position_limit=float(equity_position_limit) if equity_position_limit is not None else None,
        allowed_sectors=[str(item).strip() for item in (hard_filters.get("allowed_sectors") or []) if str(item).strip()],
        blocked_sectors=[str(item).strip() for item in (hard_filters.get("blocked_sectors") or []) if str(item).strip()],
    )


def _resolve_market_gate_reason(resolved: ResolvedConstraintPack, market_profile: dict[str, Any] | None) -> str | None:
    profile = dict(market_profile or {})
    regime = str(profile.get("regime") or "").strip()
    regime_score = float(profile.get("regime_score", 0.0) or 0.0)
    inferred = bool(profile.get("inferred", False))
    if resolved.allowed_regimes and regime not in set(resolved.allowed_regimes):
        return f"market_regime_not_allowed:{regime or 'unknown'}"
    if resolved.blocked_regimes and regime in set(resolved.blocked_regimes):
        return f"market_regime_blocked:{regime}"
    if resolved.min_regime_score is not None and regime_score < resolved.min_regime_score:
        return f"market_regime_score_too_low:{regime_score}"
    if not resolved.allow_inferred_market_profile and inferred:
        return "market_profile_inferred_not_allowed"
    return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _execution_barrier_reason(item: dict[str, Any], resolved: ResolvedConstraintPack) -> str | None:
    snapshot = dict(item.get("market_snapshot") or {})
    symbol = str(item.get("symbol") or "").strip().upper()
    action = str(item.get("action") or "BUY").upper()
    pre_close = float(snapshot.get("pre_close", 0.0) or 0.0)
    last_price = float(snapshot.get("last_price", 0.0) or 0.0)

    # R0.8: 硬阻断 - 涨跌停判定 (买入阻断涨停，卖出阻断跌停)
    if pre_close > 0 and last_price > 0:
        if action == "BUY" and is_limit_up(symbol, last_price, pre_close):
            return "limit_up_block_buy"
        if action == "SELL" and is_limit_down(symbol, last_price, pre_close):
            return "limit_down_block_sell"

    if not (
        resolved.require_fresh_snapshot
        or resolved.max_snapshot_age_seconds is not None
        or resolved.max_price_deviation_pct is not None
    ):
        return None

    timestamp = (
        str(
            snapshot.get("quote_timestamp")
            or snapshot.get("snapshot_at")
            or snapshot.get("captured_at")
            or ""
        ).strip()
    )
    if (resolved.require_fresh_snapshot or resolved.max_snapshot_age_seconds is not None) and timestamp:
        parsed = _parse_iso_datetime(timestamp)
        if parsed is not None and resolved.max_snapshot_age_seconds is not None:
            age_seconds = max((datetime.now(parsed.tzinfo) - parsed).total_seconds(), 0.0)
            if age_seconds > resolved.max_snapshot_age_seconds:
                return f"market_snapshot_stale:{int(age_seconds)}s"
    elif resolved.require_fresh_snapshot:
        # 如果要求新鲜快照但完全没有时间戳，视作不新鲜
        return "market_snapshot_missing_timestamp"

    if resolved.max_price_deviation_pct is not None:
        latest = last_price
        bid = float(snapshot.get("bid_price", 0.0) or 0.0)
        ask = float(snapshot.get("ask_price", 0.0) or 0.0)
        # 偏离度通常看买一卖一价的中点或对手价
        reference = ask if action == "BUY" and ask > 0 else (bid if action == "SELL" and bid > 0 else latest)
        
        if latest > 0 and reference > 0:
            deviation = abs(latest - reference) / reference
            if deviation > resolved.max_price_deviation_pct:
                return f"price_deviation_exceeded:{round(deviation, 4)}"
    return None


def apply_constraint_pack(
    decisions: list[dict[str, Any]],
    resolved: ResolvedConstraintPack,
    *,
    market_profile: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    market_gate_reason = _resolve_market_gate_reason(resolved, market_profile)
    if market_gate_reason:
        for item in decisions:
            filtered.append(
                {
                    "symbol": str(item.get("symbol") or "").strip().upper(),
                    "name": str(item.get("name") or "").strip(),
                    "stage": "market_rules",
                    "reason": market_gate_reason,
                }
            )
        return [], filtered
    for item in decisions:
        symbol = str(item.get("symbol") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        code = symbol.split(".", 1)[0] if "." in symbol else symbol
        resolved_sector = str(item.get("resolved_sector") or "").strip()
        if resolved.emergency_stop:
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "risk_rules",
                    "reason": "命中 emergency_stop",
                }
            )
            continue
        execution_barrier_reason = _execution_barrier_reason(item, resolved)
        if execution_barrier_reason:
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "execution_barriers",
                    "reason": execution_barrier_reason,
                }
            )
            continue
        if symbol in set(resolved.symbol_blacklist):
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "hard_filters",
                    "reason": "命中 symbol_blacklist",
                }
            )
            continue
        if resolved.excluded_prefixes and any(code.startswith(prefix) for prefix in resolved.excluded_prefixes):
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "hard_filters",
                    "reason": "命中 excluded_prefixes",
                }
            )
            continue
        # R0.8: 板块过滤
        if resolved.allowed_sectors and resolved_sector not in set(resolved.allowed_sectors):
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "hard_filters",
                    "reason": f"未命中 allowed_sectors: {resolved_sector}",
                }
            )
            continue
        if resolved.blocked_sectors and resolved_sector in set(resolved.blocked_sectors):
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "hard_filters",
                    "reason": f"命中 blocked_sectors: {resolved_sector}",
                }
            )
            continue
        if symbol in set(resolved.blocked_symbols):
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "risk_rules",
                    "reason": "命中 blocked_symbols",
                }
            )
            continue
        if resolved.avoid_existing_holdings and symbol in set(resolved.holding_symbols):
            filtered.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "stage": "position_rules",
                    "reason": "命中 avoid_existing_holdings",
                }
            )
            continue
        kept.append(item)
    return kept, filtered
