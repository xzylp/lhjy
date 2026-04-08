"""调度器 — APScheduler 真实调度，盘前/盘中/盘后时间表"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .contracts import ExitContext, PlaceOrderRequest, PositionSnapshot, QuoteSnapshot, SectorProfile, StockBehaviorProfile
from .data.serving import ServingStore
from .execution_gateway import (
    EXECUTION_GATEWAY_PENDING_PATH,
    enqueue_execution_gateway_intent,
    resolve_execution_gateway_state_store,
)
from .execution_safety import is_limit_up
from .governance.inspection import collect_parameter_hint_inspection
from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore
from .infra.market_adapter import MarketDataAdapter
from .learning.attribution import TradeAttributionService
from .logging_config import get_logger
from .notify.monitor_changes import MonitorChangeNotifier
from .notify.templates import execution_order_event_template
from .precompute import DossierPrecomputeService
from .reverse_repo import ReverseRepoService
from .settings import AppSettings
from .strategy.sell_decision import PositionState, SellDecisionEngine

logger = get_logger("scheduler")


@dataclass
class ScheduledTask:
    name: str
    cron: str
    handler: str
    enabled: bool = True


# 盘前任务 (07:30 - 09:15, 周一至周五)
PRE_MARKET_TASKS = [
    ScheduledTask(name="新闻扫描",    cron="30 7 * * 1-5",  handler="data.fetcher:fetch_news"),
    ScheduledTask(name="竞价预分析",  cron="0 8 * * 1-5",   handler="sentiment.calculator:pre_market"),
    ScheduledTask(name="环境评分",    cron="0 9 * * 1-5",   handler="sentiment.calculator:compute_daily"),
    ScheduledTask(name="买入清单",    cron="10 9 * * 1-5",  handler="strategy.buy_decision:generate_buy_list"),
    ScheduledTask(name="逆回购开盘回补", cron="35 9 * * 1-5", handler="execution.reverse_repo:auto_repurchase"),
]

# 盘中任务 (09:30 - 15:00)
INTRADAY_TASKS = [
    ScheduledTask(name="开盘执行",    cron="30 9 * * 1-5",  handler="strategy.buy_decision:execute_open"),
    ScheduledTask(name="盯盘巡检",    cron="*/5 10-14 * * 1-5", handler="monitor.market_watcher:check_once"),
    ScheduledTask(name="午间快照",    cron="30 11 * * 1-5", handler="sentiment.calculator:midday_snapshot"),
    ScheduledTask(name="午后刷新",    cron="0 13 * * 1-5",  handler="strategy.screener:refresh"),
    ScheduledTask(name="尾盘决策",    cron="30 14 * * 1-5", handler="strategy.sell_decision:tail_market"),
]

# 盘后任务 (15:00 - 23:00)
POST_MARKET_TASKS = [
    ScheduledTask(name="日终数据拉取", cron="30 15 * * 1-5", handler="data.fetcher:fetch_daily"),
    ScheduledTask(name="因子计算",     cron="45 15 * * 1-5", handler="factors.engine:compute_all"),
    ScheduledTask(name="日终复盘",     cron="0 16 * * 1-5",  handler="report.daily:generate"),
    ScheduledTask(name="龙虎榜分析",   cron="0 17 * * 1-5",  handler="monitor.dragon_tiger:analyze"),
    ScheduledTask(name="股性画像刷新", cron="30 17 * * 1-5", handler="strategy.stock_profile:refresh"),
    ScheduledTask(name="参数治理巡检", cron="0 18 * * 1-5",  handler="governance.parameter_hints:inspection"),
    ScheduledTask(name="次日新闻扫描", cron="0 20 * * 1-5",  handler="data.fetcher:fetch_news"),
    ScheduledTask(name="选股评分",     cron="0 21 * * 1-5",  handler="strategy.screener:run_pipeline"),
    ScheduledTask(name="买入清单预确认", cron="0 22 * * 1-5", handler="strategy.buy_decision:pre_confirm"),
]

ALL_TASKS = PRE_MARKET_TASKS + INTRADAY_TASKS + POST_MARKET_TASKS


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_datetime_pair(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    if (left.tzinfo is None) == (right.tzinfo is None):
        return left, right
    if left.tzinfo is not None:
        return left.replace(tzinfo=None), right
    return left, right.replace(tzinfo=None)


def _resolve_scheduler_account_id(settings: AppSettings, execution_adapter: ExecutionAdapter) -> str:
    candidates: list[str] = []
    configured = str(getattr(settings.xtquant, "account_id", "") or "").strip()
    if configured:
        candidates.append(configured)
    adapter_mode = getattr(execution_adapter, "mode", "") or (
        "mock" if execution_adapter.__class__.__name__.startswith("Mock") else ""
    )
    if adapter_mode in {"mock", "mock-fallback"}:
        candidates.append("sim-001")
    balances = getattr(execution_adapter, "balances", None)
    if isinstance(balances, dict):
        candidates.extend(str(key) for key in balances.keys())
    for account_id in dict.fromkeys(item for item in candidates if item):
        try:
            execution_adapter.get_balance(account_id)
            return account_id
        except Exception:
            continue
    return configured or "sim-001"


def _estimate_position_atr(entry_price: float, current_price: float, exit_params: dict | None = None) -> float:
    atr_pct = float((exit_params or {}).get("atr_pct", 0.0) or 0.0)
    if atr_pct > 0:
        return max(entry_price * atr_pct, 0.01)
    reference_price = max(entry_price, current_price, 1.0)
    return max(reference_price * 0.02, 0.01)


def _normalize_sell_quantity(available: int, sell_ratio: float) -> int:
    if available <= 0 or sell_ratio <= 0:
        return 0
    if sell_ratio >= 0.999:
        return available
    desired = max(int(available * sell_ratio), 100)
    if available < 100:
        return 0
    return min((desired // 100) * 100, (available // 100) * 100)


def _append_scheduler_order_journal(
    meeting_state_store: StateStore | None,
    request: PlaceOrderRequest,
    *,
    order_id: str,
    name: str,
    submitted_at: str,
    source: str = "scheduler_tail_market",
    extra_metadata: dict | None = None,
) -> None:
    if not meeting_state_store:
        return
    journal = meeting_state_store.get("execution_order_journal", [])
    request_payload = request.model_dump()
    for item in reversed(journal):
        if item.get("order_id") != order_id:
            continue
        payload = {
            "trade_date": request.trade_date or item.get("trade_date") or datetime.now().date().isoformat(),
            "account_id": request.account_id,
            "symbol": request.symbol,
            "name": name or item.get("name") or request.symbol,
            "decision_id": request.decision_id,
            "submitted_at": submitted_at,
            "playbook": request.playbook,
            "regime": request.regime,
            "exit_reason": request.exit_reason,
            "request": request_payload,
            "source": source,
        }
        if extra_metadata:
            payload.update(extra_metadata)
        item.update(payload)
        meeting_state_store.set("execution_order_journal", journal[-200:])
        return
    payload = {
        "trade_date": request.trade_date or datetime.now().date().isoformat(),
        "account_id": request.account_id,
        "order_id": order_id,
        "symbol": request.symbol,
        "name": name or request.symbol,
        "decision_id": request.decision_id,
        "submitted_at": submitted_at,
        "playbook": request.playbook,
        "regime": request.regime,
        "exit_reason": request.exit_reason,
        "request": request_payload,
        "source": source,
    }
    if extra_metadata:
        payload.update(extra_metadata)
    journal.append(payload)
    meeting_state_store.set("execution_order_journal", journal[-200:])


def _persist_tail_market_scan(meeting_state_store: StateStore | None, payload: dict) -> None:
    if not meeting_state_store:
        return
    history = meeting_state_store.get("tail_market_history", [])
    history.append(
        {
            "scanned_at": payload.get("scanned_at"),
            "trade_date": payload.get("trade_date"),
            "account_id": payload.get("account_id"),
            "status": payload.get("status"),
            "position_count": payload.get("position_count", 0),
            "signal_count": payload.get("signal_count", 0),
            "submitted_count": payload.get("submitted_count", 0),
            "queued_count": payload.get("queued_count", 0),
            "preview_count": payload.get("preview_count", 0),
            "error_count": payload.get("error_count", 0),
        }
    )
    meeting_state_store.set("latest_tail_market_scan", payload)
    meeting_state_store.set("tail_market_history", history[-30:])


def _normalize_behavior_profile(symbol: str, payload: dict | StockBehaviorProfile | None) -> dict:
    if payload is None:
        return {}
    try:
        if isinstance(payload, StockBehaviorProfile):
            return payload.model_dump()
        if not isinstance(payload, dict):
            return {}
        profile = StockBehaviorProfile.model_validate({"symbol": symbol, **payload})
        return profile.model_dump()
    except Exception:
        return {}


def _build_behavior_profile_map(runtime_context: dict, dossier_by_symbol: dict[str, dict]) -> dict[str, dict]:
    behavior_map: dict[str, dict] = {}
    raw_profiles = runtime_context.get("behavior_profiles")
    if isinstance(raw_profiles, dict):
        for symbol, payload in raw_profiles.items():
            normalized = _normalize_behavior_profile(symbol, payload)
            if normalized:
                behavior_map[symbol] = normalized
    elif isinstance(raw_profiles, list):
        for payload in raw_profiles:
            if not isinstance(payload, dict) or not payload.get("symbol"):
                continue
            normalized = _normalize_behavior_profile(payload["symbol"], payload)
            if normalized:
                behavior_map[payload["symbol"]] = normalized

    for symbol, item in dossier_by_symbol.items():
        normalized = (
            _normalize_behavior_profile(symbol, item.get("behavior_profile"))
            or _normalize_behavior_profile(symbol, (item.get("playbook_context") or {}).get("behavior_profile"))
            or _normalize_behavior_profile(symbol, (item.get("symbol_context") or {}).get("behavior_profile"))
        )
        if normalized:
            behavior_map.setdefault(symbol, normalized)
    return behavior_map


def _load_tail_market_dossier_map(storage_root: Path, trade_date: str) -> dict[str, dict]:
    serving_store = ServingStore(storage_root)
    dossier_pack = serving_store.get_latest_dossier_pack() or {}
    if dossier_pack and dossier_pack.get("trade_date") == trade_date:
        return {
            item.get("symbol"): item
            for item in dossier_pack.get("items", [])
            if isinstance(item, dict) and item.get("symbol")
        }
    return {}


def _load_tail_market_negative_alerts(storage_root: Path, trade_date: str) -> dict[str, int]:
    serving_store = ServingStore(storage_root)
    monitor_context = serving_store.get_latest_monitor_context() or {}
    if monitor_context and monitor_context.get("trade_date") not in {None, trade_date}:
        return {}
    counts: dict[str, int] = {}
    for item in monitor_context.get("recent_events", []):
        symbol = item.get("symbol")
        if not symbol:
            continue
        alert_type = str(item.get("alert_type") or "")
        severity = str(item.get("severity") or "")
        change_pct = float(item.get("change_pct") or 0.0)
        is_negative = (
            change_pct < 0
            or alert_type in {"limit_down"}
            or severity in {"warning", "critical"}
        )
        if is_negative:
            counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def _build_intraday_bar_metrics(bar_items: list) -> dict:
    if not bar_items:
        return {}
    ordered = sorted(bar_items, key=lambda item: item.trade_time)
    recent_window = ordered[-3:]
    recent_returns = [
        float(item.get("return_pct", 0.0) or 0.0)
        for item in _build_bar_return_series(recent_window)
    ]
    first_open = float(ordered[0].open or ordered[0].close or 0.0)
    latest_close = float(ordered[-1].close or 0.0)
    intraday_high = max(float(item.high or item.close or 0.0) for item in ordered)
    intraday_low = min(float(item.low or item.close or 0.0) for item in ordered)
    recent_high = max(float(item.high or item.close or 0.0) for item in recent_window)
    intraday_change_pct = 0.0
    intraday_drawdown_pct = 0.0
    rebound_from_low_pct = 0.0
    recent_drawdown_pct = 0.0
    recent_rebound_from_low_pct = 0.0
    if first_open > 0:
        intraday_change_pct = round((latest_close - first_open) / first_open, 6)
    if intraday_high > 0:
        intraday_drawdown_pct = round((intraday_high - latest_close) / intraday_high, 6)
    if intraday_low > 0:
        rebound_from_low_pct = round((latest_close - intraday_low) / intraday_low, 6)
    if recent_high > 0:
        recent_drawdown_pct = round((recent_high - latest_close) / recent_high, 6)
    recent_low = min(float(item.low or item.close or 0.0) for item in recent_window)
    if recent_low > 0:
        recent_rebound_from_low_pct = round((latest_close - recent_low) / recent_low, 6)
    return {
        "bar_count": len(ordered),
        "first_open": first_open,
        "latest_close": latest_close,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "intraday_change_pct": intraday_change_pct,
        "intraday_drawdown_pct": intraday_drawdown_pct,
        "rebound_from_low_pct": rebound_from_low_pct,
        "latest_bar_return_pct": recent_returns[-1] if recent_returns else 0.0,
        "recent_return_2_sum": round(sum(recent_returns[-2:]), 6) if recent_returns else 0.0,
        "recent_return_3_sum": round(sum(recent_returns[-3:]), 6) if recent_returns else 0.0,
        "recent_negative_bar_count": sum(1 for value in recent_returns if value < 0),
        "recent_drawdown_pct": recent_drawdown_pct,
        "recent_rebound_from_low_pct": recent_rebound_from_low_pct,
    }


def _build_bar_return_series(bar_items: list) -> list[dict]:
    if not bar_items:
        return []
    ordered = sorted(bar_items, key=lambda item: item.trade_time)
    returns: list[dict] = []
    prev_close = 0.0
    for item in ordered:
        close_price = float(item.close or 0.0)
        base_price = float(prev_close or item.open or close_price or 0.0)
        return_pct = round((close_price - base_price) / base_price, 6) if base_price > 0 else 0.0
        returns.append(
            {
                "trade_time": item.trade_time,
                "return_pct": return_pct,
            }
        )
        if close_price > 0:
            prev_close = close_price
    return returns


def _build_sector_relative_sequence_metrics(
    symbol: str,
    peer_symbols: list[str],
    bar_return_map: dict[str, list[dict]],
    *,
    period_label: str = "5m",
    lookback: int = 3,
) -> dict:
    trend_key = f"sector_relative_trend_{period_label}"
    underperform_key = f"sector_underperform_bars_{period_label}"
    symbol_returns = bar_return_map.get(symbol, [])
    if not symbol_returns:
        return {
            trend_key: 0.0,
            underperform_key: 0,
        }
    peer_return_map: dict[str, list[float]] = {}
    for peer_symbol in peer_symbols:
        if peer_symbol == symbol:
            continue
        for item in bar_return_map.get(peer_symbol, []):
            peer_return_map.setdefault(item["trade_time"], []).append(float(item.get("return_pct", 0.0) or 0.0))
    relative_returns: list[float] = []
    for item in symbol_returns[-lookback:]:
        peer_returns = peer_return_map.get(item["trade_time"], [])
        if not peer_returns:
            continue
        peer_avg_return = sum(peer_returns) / len(peer_returns)
        relative_returns.append(round(float(item.get("return_pct", 0.0) or 0.0) - peer_avg_return, 6))
    if not relative_returns:
        return {
            trend_key: 0.0,
            underperform_key: 0,
        }
    return {
        trend_key: round(sum(relative_returns), 6),
        underperform_key: sum(1 for value in relative_returns if value < 0),
    }


def _build_tail_market_review_tags(ctx: ExitContext) -> list[str]:
    tags: list[str] = []
    exit_params = ctx.exit_params or {}
    micro_1m_return_3_sum = _safe_float(exit_params.get("micro_1m_return_3_sum"), 0.0)
    micro_1m_drawdown_pct = _safe_float(exit_params.get("micro_1m_drawdown_pct"), 0.0)
    micro_1m_negative_bars = _safe_int(exit_params.get("micro_1m_negative_bars"), 0)
    micro_1m_latest_return_pct = _safe_float(exit_params.get("micro_1m_latest_return_pct"), 0.0)
    micro_1m_rebound_from_low_pct = _safe_float(exit_params.get("micro_1m_rebound_from_low_pct"), 0.0)
    sector_relative_trend_1m = _safe_float(exit_params.get("sector_relative_trend_1m"), 0.0)
    if ctx.style_tag == "leader":
        tags.append("leader_style")
    if ctx.sector_retreat:
        tags.append("sector_retreat")
    if ctx.negative_alert_count > 0:
        tags.append("negative_alert")
    if ctx.intraday_drawdown_pct >= 0.025 and ctx.rebound_from_low_pct <= 0.01:
        tags.append("intraday_fade")
    if ctx.sector_relative_strength_5m <= -0.015:
        tags.append("sector_relative_weak")
    if ctx.sector_underperform_bars_5m >= 2 and ctx.sector_relative_trend_5m <= -0.01:
        tags.append("sector_relative_trend_weak")
    if (
        ctx.sector_intraday_change_pct <= -0.008
        and ctx.intraday_change_pct <= -0.004
        and (ctx.sector_relative_trend_5m <= -0.004 or sector_relative_trend_1m <= -0.006)
        and micro_1m_return_3_sum <= -0.008
        and micro_1m_drawdown_pct >= 0.012
    ):
        tags.append("sector_sync_weak")
    if (
        micro_1m_return_3_sum <= -0.01
        and micro_1m_drawdown_pct >= 0.012
        and micro_1m_negative_bars >= 2
    ):
        tags.append("microstructure_fast_exit")
    if (
        micro_1m_drawdown_pct >= 0.01
        and micro_1m_rebound_from_low_pct <= 0.004
        and micro_1m_latest_return_pct <= -0.003
        and (sector_relative_trend_1m <= -0.004 or ctx.negative_alert_count > 0)
    ):
        tags.append("micro_rebound_failed")
    return list(dict.fromkeys(tags))


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_tail_market_exit_params(
    base_exit_params: dict | None,
    *,
    intraday_metrics_5m: dict | None,
    intraday_metrics_1m: dict | None,
    sector_relative_sequence_metrics_1m: dict | None,
) -> dict:
    exit_params = dict(base_exit_params or {})
    exit_params.setdefault("post_entry_grace_minutes", 8)
    exit_params.setdefault("rapid_distortion_min_hold_minutes", 8)
    exit_params.setdefault("rapid_distortion_window_minutes", 90)
    exit_params.setdefault("sector_sync_min_hold_minutes", 20)
    exit_params.setdefault("sector_sync_window_minutes", 180)
    metrics_5m = intraday_metrics_5m or {}
    metrics_1m = intraday_metrics_1m or {}
    sector_metrics_1m = sector_relative_sequence_metrics_1m or {}
    exit_params.update(
        {
            "micro_5m_return_2_sum": _safe_float(metrics_5m.get("recent_return_2_sum"), 0.0),
            "micro_5m_drawdown_pct": _safe_float(metrics_5m.get("recent_drawdown_pct"), 0.0),
            "micro_1m_return_3_sum": _safe_float(metrics_1m.get("recent_return_3_sum"), 0.0),
            "micro_1m_drawdown_pct": _safe_float(metrics_1m.get("recent_drawdown_pct"), 0.0),
            "micro_1m_negative_bars": _safe_int(metrics_1m.get("recent_negative_bar_count"), 0),
            "micro_1m_latest_return_pct": _safe_float(metrics_1m.get("latest_bar_return_pct"), 0.0),
            "micro_1m_rebound_from_low_pct": _safe_float(metrics_1m.get("recent_rebound_from_low_pct"), 0.0),
            "sector_relative_trend_1m": _safe_float(
                sector_metrics_1m.get("sector_relative_trend_1m"),
                0.0,
            ),
            "sector_underperform_bars_1m": _safe_int(
                sector_metrics_1m.get("sector_underperform_bars_1m"),
                0,
            ),
        }
    )
    return exit_params


def _build_sector_peer_map(
    positions: list[PositionSnapshot],
    runtime_context: dict,
    dossier_by_symbol: dict[str, dict],
) -> dict[str, list[str]]:
    position_symbols = {item.symbol for item in positions}
    sector_map: dict[str, str] = {}
    all_symbols_by_sector: dict[str, list[str]] = {}

    for item in runtime_context.get("playbook_contexts", []) or []:
        symbol = item.get("symbol")
        sector = item.get("sector")
        if symbol and sector:
            sector_map[symbol] = sector
            all_symbols_by_sector.setdefault(sector, [])
            if symbol not in all_symbols_by_sector[sector]:
                all_symbols_by_sector[sector].append(symbol)

    for symbol, item in dossier_by_symbol.items():
        sector = (
            item.get("resolved_sector")
            or (item.get("playbook_context") or {}).get("sector")
        )
        if symbol and sector:
            sector_map.setdefault(symbol, sector)
            all_symbols_by_sector.setdefault(sector, [])
            if symbol not in all_symbols_by_sector[sector]:
                all_symbols_by_sector[sector].append(symbol)

    peer_map: dict[str, list[str]] = {}
    for symbol in position_symbols:
        sector = sector_map.get(symbol, "")
        if not sector:
            peer_map[symbol] = [symbol]
            continue
        peers = all_symbols_by_sector.get(sector, [])
        peer_symbols = list(dict.fromkeys(peers))
        if symbol not in peer_symbols:
            peer_symbols.append(symbol)
        peer_map[symbol] = peer_symbols
    return peer_map


def _dispatch_tail_market_event(
    dispatcher,
    request: PlaceOrderRequest,
    *,
    name: str,
    order_id: str,
    quantity: int,
    price: float,
) -> None:
    if not dispatcher:
        return
    content = execution_order_event_template(
        action="尾盘卖出",
        symbol=request.symbol,
        name=name,
        account_id=request.account_id,
        side=request.side,
        quantity=quantity,
        price=price,
        order_id=order_id,
        status="PENDING",
        decision_id=request.decision_id,
        reason=request.exit_reason,
    )
    dispatcher.dispatch_trade("尾盘卖出", content, level="warning", force=True)


def _build_tail_market_gateway_intent(
    request: PlaceOrderRequest,
    *,
    name: str,
    decision_id: str | None,
    signal_reason: str,
    playbook: str | None,
    regime: str | None,
    resolved_sector: str | None,
    review_tags: list[str],
    exit_context_snapshot: dict | None,
) -> dict:
    request_payload = request.model_dump()
    estimated_value = round(float(request.price or 0.0) * float(request.quantity), 3)
    return {
        "intent_id": request.request_id,
        "trade_date": request.trade_date or datetime.now().date().isoformat(),
        "case_id": decision_id,
        "decision_id": decision_id,
        "account_id": request.account_id,
        "symbol": request.symbol,
        "name": name,
        "side": request.side,
        "quantity": request.quantity,
        "price": request.price,
        "estimated_value": estimated_value,
        "request": request_payload,
        "headline_reason": signal_reason,
        "playbook": playbook,
        "regime": regime,
        "resolved_sector": resolved_sector,
        "risk_context": {
            "estimated_value": estimated_value,
            "exit_reason": signal_reason,
            "review_tags": list(review_tags),
        },
        "discussion_context": {
            "case_id": decision_id,
            "decision_id": decision_id,
            "name": name,
            "headline_reason": signal_reason,
            "trigger_source": "tail_market_scan",
        },
        "strategy_context": {
            "playbook": playbook,
            "regime": regime,
            "resolved_sector": resolved_sector,
            "exit_reason": signal_reason,
            "review_tags": list(review_tags),
            "exit_context_snapshot": dict(exit_context_snapshot or {}),
        },
    }


def run_tail_market_scan(
    *,
    settings: AppSettings,
    market: MarketDataAdapter,
    execution_adapter: ExecutionAdapter,
    meeting_state_store: StateStore | None,
    runtime_state_store: StateStore | None,
    candidate_case_service=None,
    dispatcher=None,
    runtime_context: dict | None = None,
    discussion_context: dict | None = None,
    account_id: str | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> dict:
    now = (now_factory or datetime.now)()
    account_id = account_id or _resolve_scheduler_account_id(settings, execution_adapter)
    gateway_state_store = resolve_execution_gateway_state_store(meeting_state_store, runtime_state_store)
    if runtime_context is None and runtime_state_store:
        runtime_context = runtime_state_store.get("latest_runtime_context", {}) or {}
    runtime_context = runtime_context or {}
    discussion_context = discussion_context or {}
    trade_date = (
        runtime_context.get("trade_date")
        or discussion_context.get("trade_date")
        or now.date().isoformat()
    )
    market_profile = runtime_context.get("market_profile") or {}
    playbook_map = {
        item.get("symbol"): item
        for item in runtime_context.get("playbook_contexts", [])
        if item.get("symbol")
    }
    dossier_by_symbol = _load_tail_market_dossier_map(settings.storage_root, trade_date)
    behavior_profile_map = _build_behavior_profile_map(runtime_context, dossier_by_symbol)
    negative_alert_map = _load_tail_market_negative_alerts(settings.storage_root, trade_date)
    sector_profile_map = {
        item.get("sector_name"): SectorProfile.model_validate(item)
        for item in (
            runtime_context.get("sector_profiles")
            or market_profile.get("sector_profiles")
            or []
        )
        if item.get("sector_name")
    }
    journal = meeting_state_store.get("execution_order_journal", []) if meeting_state_store else []
    latest_journal_by_symbol: dict[str, dict] = {}
    latest_buy_by_symbol: dict[str, dict] = {}
    open_sell_symbols: set[str] = set()
    for item in journal:
        symbol = item.get("symbol")
        if not symbol:
            continue
        latest_journal_by_symbol[symbol] = item
        request = item.get("request") or {}
        side = str(item.get("side") or request.get("side") or "").upper()
        status = str(item.get("latest_status") or "PENDING").upper()
        if side == "BUY":
            latest_buy_by_symbol[symbol] = item
        if side == "SELL" and status in {"PENDING", "ACCEPTED", "PARTIAL_FILLED", "CANCEL_REQUESTED", "UNKNOWN"}:
            open_sell_symbols.add(symbol)

    case_map_by_symbol: dict[str, object] = {}
    if candidate_case_service:
        if trade_date:
            case_map_by_symbol = {
                item.symbol: item
                for item in candidate_case_service.list_cases(trade_date=trade_date, limit=500)
            }

    try:
        positions = [item for item in execution_adapter.get_positions(account_id) if int(item.available) > 0]
    except Exception as exc:
        payload = {
            "status": "error",
            "account_id": account_id,
            "trade_date": runtime_context.get("trade_date") or now.date().isoformat(),
            "scanned_at": now.isoformat(),
            "execution_plane": str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
            "position_count": 0,
            "signal_count": 0,
            "submitted_count": 0,
            "queued_count": 0,
            "preview_count": 0,
            "error_count": 1,
            "summary_lines": [f"尾盘卖出扫描失败: {exc}"],
            "items": [],
        }
        _persist_tail_market_scan(meeting_state_store, payload)
        return payload

    if not positions:
        payload = {
            "status": "ok",
            "account_id": account_id,
            "trade_date": trade_date,
            "scanned_at": now.isoformat(),
            "execution_plane": str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
            "position_count": 0,
            "signal_count": 0,
            "submitted_count": 0,
            "queued_count": 0,
            "preview_count": 0,
            "error_count": 0,
            "summary_lines": ["尾盘卖出扫描完成: 当前无可卖持仓。"],
            "items": [],
        }
        _persist_tail_market_scan(meeting_state_store, payload)
        return payload

    quote_map = {
        item.symbol: item
        for item in market.get_snapshots([item.symbol for item in positions])
    }
    intraday_bar_map: dict[str, dict] = {}
    intraday_return_map: dict[str, list[dict]] = {}
    micro_bar_map_1m: dict[str, dict] = {}
    micro_return_map_1m: dict[str, list[dict]] = {}
    try:
        sector_peer_map = _build_sector_peer_map(positions, runtime_context, dossier_by_symbol)
        intraday_symbols = list(
            dict.fromkeys(
                symbol
                for peers in sector_peer_map.values()
                for symbol in peers
            )
        )
        intraday_bars = market.get_bars(intraday_symbols or [item.symbol for item in positions], "5m", count=12)
        micro_bars_1m = market.get_bars(intraday_symbols or [item.symbol for item in positions], "1m", count=15)
        grouped_bars: dict[str, list] = {}
        grouped_micro_bars_1m: dict[str, list] = {}
        for item in intraday_bars:
            grouped_bars.setdefault(item.symbol, []).append(item)
        for item in micro_bars_1m:
            grouped_micro_bars_1m.setdefault(item.symbol, []).append(item)
        intraday_bar_map = {
            symbol: _build_intraday_bar_metrics(items)
            for symbol, items in grouped_bars.items()
        }
        intraday_return_map = {
            symbol: _build_bar_return_series(items)
            for symbol, items in grouped_bars.items()
        }
        micro_bar_map_1m = {
            symbol: _build_intraday_bar_metrics(items)
            for symbol, items in grouped_micro_bars_1m.items()
        }
        micro_return_map_1m = {
            symbol: _build_bar_return_series(items)
            for symbol, items in grouped_micro_bars_1m.items()
        }
    except Exception:
        sector_peer_map = {item.symbol: [item.symbol] for item in positions}
        intraday_bar_map = {}
        intraday_return_map = {}
        micro_bar_map_1m = {}
        micro_return_map_1m = {}
    adapter_mode = getattr(execution_adapter, "mode", "") or (
        "mock" if execution_adapter.__class__.__name__.startswith("Mock") else ""
    )
    execution_plane = str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant")
    submit_orders = settings.run_mode == "live" and settings.live_trade_enabled
    queue_for_gateway = execution_plane == "windows_gateway"
    if queue_for_gateway:
        submit_orders = False
    elif settings.run_mode == "paper" and adapter_mode in {"mock", "mock-fallback"}:
        submit_orders = True

    engine = SellDecisionEngine()
    signal_count = 0
    submitted_count = 0
    queued_count = 0
    preview_count = 0
    error_count = 0
    items: list[dict] = []

    for position in positions:
        symbol = position.symbol
        name = market.get_symbol_name(symbol)
        quote = quote_map.get(symbol) or QuoteSnapshot(
            symbol=symbol,
            name=name,
            last_price=position.last_price,
            bid_price=position.last_price,
            ask_price=position.last_price,
            volume=0.0,
        )
        latest_buy = latest_buy_by_symbol.get(symbol, {})
        latest_request = latest_buy.get("request") or {}
        latest_order = latest_journal_by_symbol.get(symbol, {})
        dossier_item = dossier_by_symbol.get(symbol) or {}
        symbol_context = dossier_item.get("symbol_context") or {}
        playbook_context = playbook_map.get(symbol) or dossier_item.get("playbook_context") or {}
        behavior_profile = (
            behavior_profile_map.get(symbol)
            or _normalize_behavior_profile(symbol, playbook_context.get("behavior_profile"))
            or _normalize_behavior_profile(symbol, latest_buy.get("behavior_profile"))
            or _normalize_behavior_profile(symbol, latest_request.get("behavior_profile"))
        )
        sector_name = (
            playbook_context.get("sector")
            or dossier_item.get("resolved_sector")
            or latest_buy.get("sector")
            or latest_request.get("sector")
            or ""
        )
        sector = sector_profile_map.get(sector_name)
        entry_dt = _parse_iso_datetime(latest_buy.get("submitted_at") or latest_order.get("submitted_at"))
        if entry_dt is not None:
            normalized_now, normalized_entry_dt = _normalize_datetime_pair(now, entry_dt)
            holding_minutes = max(int((normalized_now - normalized_entry_dt).total_seconds() // 60), 0)
            holding_days = max((normalized_now.date() - normalized_entry_dt.date()).days, 0)
        else:
            holding_minutes = 0
            holding_days = 0
        playbook = (
            playbook_context.get("playbook")
            or dossier_item.get("assigned_playbook")
            or latest_buy.get("playbook")
            or latest_request.get("playbook")
        )
        regime = (
            latest_buy.get("regime")
            or latest_request.get("regime")
            or market_profile.get("regime")
            or "unknown"
        )
        entry_price = float(position.cost_price or quote.last_price or position.last_price or 0.0)
        current_price = float(quote.last_price or position.last_price or entry_price)
        atr = _estimate_position_atr(entry_price, current_price, playbook_context.get("exit_params") or {})
        relative_strength = (
            round((current_price - entry_price) / max(entry_price, 1e-9), 6)
            if entry_price > 0
            else 0.0
        )
        intraday_metrics = intraday_bar_map.get(symbol, {})
        micro_metrics_1m = micro_bar_map_1m.get(symbol, {})
        sector_peer_symbols = sector_peer_map.get(symbol, [symbol])
        sector_peer_changes = [
            float((intraday_bar_map.get(peer) or {}).get("intraday_change_pct", 0.0) or 0.0)
            for peer in sector_peer_symbols
            if peer != symbol and peer in intraday_bar_map
        ]
        sector_intraday_change_pct = (
            round(sum(sector_peer_changes) / len(sector_peer_changes), 6)
            if sector_peer_changes
            else float(intraday_metrics.get("intraday_change_pct", 0.0) or 0.0)
        )
        sector_relative_strength_5m = round(
            float(intraday_metrics.get("intraday_change_pct", 0.0) or 0.0) - sector_intraday_change_pct,
            6,
        )
        sector_relative_sequence_metrics = _build_sector_relative_sequence_metrics(
            symbol,
            sector_peer_symbols,
            intraday_return_map,
        )
        sector_relative_sequence_metrics_1m = _build_sector_relative_sequence_metrics(
            symbol,
            sector_peer_symbols,
            micro_return_map_1m,
            period_label="1m",
            lookback=5,
        )
        exit_params = _build_tail_market_exit_params(
            playbook_context.get("exit_params") or {},
            intraday_metrics_5m=intraday_metrics,
            intraday_metrics_1m=micro_metrics_1m,
            sector_relative_sequence_metrics_1m=sector_relative_sequence_metrics_1m,
        )
        market_relative = (symbol_context.get("market_relative") or {}).get("relative_strength_pct")
        if market_relative is not None:
            try:
                relative_strength = float(market_relative)
            except (TypeError, ValueError):
                pass
        state = PositionState(
            symbol=symbol,
            entry_price=entry_price,
            atr=atr,
            holding_days=holding_days,
            current_price=current_price,
        )
        ctx = None
        if playbook:
            ctx = ExitContext(
                symbol=symbol,
                playbook=playbook,
                entry_price=entry_price,
                entry_time=(entry_dt.strftime("%H:%M") if entry_dt else "09:30"),
                holding_minutes=holding_minutes,
                holding_days=holding_days,
                sector_name=sector_name,
                is_limit_up=bool(quote.pre_close and is_limit_up(symbol, quote.last_price, quote.pre_close)),
                sector_retreat=bool(sector is not None and sector.life_cycle == "retreat"),
                relative_strength_5m=relative_strength,
                intraday_change_pct=float(intraday_metrics.get("intraday_change_pct", 0.0) or 0.0),
                intraday_drawdown_pct=float(intraday_metrics.get("intraday_drawdown_pct", 0.0) or 0.0),
                rebound_from_low_pct=float(intraday_metrics.get("rebound_from_low_pct", 0.0) or 0.0),
                negative_alert_count=int(negative_alert_map.get(symbol, 0) or 0),
                sector_intraday_change_pct=sector_intraday_change_pct,
                sector_relative_strength_5m=sector_relative_strength_5m,
                sector_relative_trend_5m=float(
                    sector_relative_sequence_metrics.get("sector_relative_trend_5m", 0.0) or 0.0
                ),
                sector_underperform_bars_5m=int(
                    sector_relative_sequence_metrics.get("sector_underperform_bars_5m", 0) or 0
                ),
                optimal_hold_days=int(behavior_profile.get("optimal_hold_days") or 1),
                style_tag=str(behavior_profile.get("style_tag") or playbook_context.get("style_tag") or ""),
                avg_sector_rank_30d=float(behavior_profile.get("avg_sector_rank_30d") or 99.0),
                leader_frequency_30d=float(behavior_profile.get("leader_frequency_30d") or 0.0),
                exit_params=exit_params,
            )
        review_tags = _build_tail_market_review_tags(ctx) if ctx is not None else []
        signal = (
            engine.evaluate_with_context(state, position, ctx=ctx, quote=quote, sector=sector)
            if ctx is not None
            else engine.evaluate(state)
        )
        item = {
            "symbol": symbol,
            "name": name,
            "available": position.available,
            "current_price": current_price,
            "entry_price": entry_price,
            "holding_days": holding_days,
            "holding_minutes": holding_minutes,
            "playbook": playbook,
            "regime": regime,
            "relative_strength_5m": relative_strength,
            "intraday_change_pct": intraday_metrics.get("intraday_change_pct"),
            "intraday_drawdown_pct": intraday_metrics.get("intraday_drawdown_pct"),
            "rebound_from_low_pct": intraday_metrics.get("rebound_from_low_pct"),
            "micro_5m_return_2_sum": exit_params.get("micro_5m_return_2_sum"),
            "micro_5m_drawdown_pct": exit_params.get("micro_5m_drawdown_pct"),
            "micro_1m_return_3_sum": exit_params.get("micro_1m_return_3_sum"),
            "micro_1m_drawdown_pct": exit_params.get("micro_1m_drawdown_pct"),
            "micro_1m_negative_bars": exit_params.get("micro_1m_negative_bars"),
            "micro_1m_latest_return_pct": exit_params.get("micro_1m_latest_return_pct"),
            "micro_1m_rebound_from_low_pct": exit_params.get("micro_1m_rebound_from_low_pct"),
            "negative_alert_count": negative_alert_map.get(symbol, 0),
            "sector_intraday_change_pct": sector_intraday_change_pct,
            "sector_relative_strength_5m": sector_relative_strength_5m,
            "sector_relative_trend_5m": sector_relative_sequence_metrics.get("sector_relative_trend_5m", 0.0),
            "sector_underperform_bars_5m": sector_relative_sequence_metrics.get("sector_underperform_bars_5m", 0),
            "sector_relative_trend_1m": sector_relative_sequence_metrics_1m.get("sector_relative_trend_1m", 0.0),
            "sector_underperform_bars_1m": sector_relative_sequence_metrics_1m.get("sector_underperform_bars_1m", 0),
            "status": "hold",
            "execution_plane": execution_plane,
        }
        if review_tags:
            item["review_tags"] = review_tags
        if behavior_profile:
            item["behavior_profile"] = behavior_profile
        if signal is None:
            items.append(item)
            continue
        signal_count += 1
        sell_quantity = _normalize_sell_quantity(int(position.available), float(signal.sell_ratio))
        item.update(
            {
                "status": "signal",
                "exit_reason": signal.reason.value,
                "sell_ratio": signal.sell_ratio,
                "stop_price": signal.stop_price,
                "planned_quantity": sell_quantity,
            }
        )
        if symbol in open_sell_symbols:
            preview_count += 1
            item["status"] = "skipped_pending_sell"
            item["preview_reason"] = "existing_pending_sell_order"
            items.append(item)
            continue
        if sell_quantity <= 0:
            preview_count += 1
            item["status"] = "skipped_min_lot"
            item["preview_reason"] = "sell_quantity_below_min_lot"
            items.append(item)
            continue
        decision_id = latest_buy.get("decision_id") or latest_order.get("decision_id")
        if not decision_id and symbol in case_map_by_symbol:
            decision_id = case_map_by_symbol[symbol].case_id
        request = PlaceOrderRequest(
            account_id=account_id,
            symbol=symbol,
            side="SELL",
            quantity=sell_quantity,
            price=float(quote.bid_price or quote.last_price or position.last_price or entry_price),
            request_id=f"tail-sell-{symbol.replace('.', '-')}-{uuid4().hex[:8]}",
            decision_id=decision_id,
            trade_date=trade_date,
            playbook=playbook,
            regime=regime,
            exit_reason=signal.reason.value,
        )
        exit_context_snapshot = ctx.model_dump() if ctx is not None else {}
        tail_market_intent = _build_tail_market_gateway_intent(
            request,
            name=name,
            decision_id=decision_id,
            signal_reason=signal.reason.value,
            playbook=playbook,
            regime=regime,
            resolved_sector=sector_name,
            review_tags=review_tags,
            exit_context_snapshot=exit_context_snapshot,
        )
        if queue_for_gateway:
            if gateway_state_store is None:
                preview_count += 1
                item["status"] = "preview"
                item["preview_reason"] = "execution_gateway_state_unavailable"
                item["request"] = request.model_dump()
                items.append(item)
                continue
            queued_packet = enqueue_execution_gateway_intent(
                gateway_state_store,
                tail_market_intent,
                run_mode=str(settings.run_mode),
                approval_source="tail_market_scan",
                summary_lines=["尾盘卖出执行意图已批准，等待 Windows Execution Gateway 拉取。"],
            )
            queued_count += 1
            item.update(
                {
                    "status": "queued_for_gateway",
                    "queued_at": queued_packet["approved_at"],
                    "gateway_pull_path": EXECUTION_GATEWAY_PENDING_PATH,
                    "gateway_intent": queued_packet,
                    "request": request.model_dump(),
                }
            )
            items.append(item)
            continue
        if not submit_orders:
            preview_count += 1
            item["status"] = "preview"
            item["preview_reason"] = (
                "submit_disabled_in_windows_gateway"
                if queue_for_gateway
                else f"submit_disabled_in_{settings.run_mode}"
            )
            item["request"] = request.model_dump()
            items.append(item)
            continue
        try:
            order = execution_adapter.place_order(request)
            submitted_at = now.isoformat()
            extra_metadata = {}
            if ctx is not None:
                extra_metadata = {
                    "exit_context_snapshot": ctx.model_dump(),
                    "review_tags": review_tags,
                }
            _append_scheduler_order_journal(
                meeting_state_store,
                request,
                order_id=order.order_id,
                name=name,
                submitted_at=submitted_at,
                extra_metadata=extra_metadata,
            )
            _dispatch_tail_market_event(
                dispatcher,
                request,
                name=name,
                order_id=order.order_id,
                quantity=order.quantity,
                price=order.price,
            )
            submitted_count += 1
            item.update(
                {
                    "status": "submitted",
                    "order_id": order.order_id,
                    "submitted_at": submitted_at,
                    "request": request.model_dump(),
                }
            )
        except Exception as exc:
            error_count += 1
            item["status"] = "error"
            item["error"] = str(exc)
        items.append(item)

    if error_count and not submitted_count and not queued_count:
        status = "error"
    elif queued_count and not submitted_count:
        status = "queued_for_gateway"
    else:
        status = "ok"
    summary_lines = [
        (
            f"尾盘卖出扫描完成: positions={len(positions)} signals={signal_count} "
            f"submitted={submitted_count} queued={queued_count} preview={preview_count} errors={error_count}."
        )
    ]
    if queue_for_gateway:
        if queued_count > 0:
            summary_lines.append("当前为 windows_gateway 执行平面，尾盘卖出已生成 queued intent，等待 Windows Execution Gateway 拉取。")
        elif preview_count > 0:
            summary_lines.append("当前为 windows_gateway 执行平面，但未能写入 gateway state，尾盘卖出仅保留预演信号。")
    elif not submit_orders:
        summary_lines.append(f"当前为 {settings.run_mode} 模式，自动卖出仅预演不报单。")
    payload = {
        "status": status,
        "account_id": account_id,
        "trade_date": trade_date,
        "scanned_at": now.isoformat(),
        "execution_plane": execution_plane,
        "position_count": len(positions),
        "signal_count": signal_count,
        "submitted_count": submitted_count,
        "queued_count": queued_count,
        "preview_count": preview_count,
        "error_count": error_count,
        "market_regime": market_profile.get("regime") or "unknown",
        "summary_lines": summary_lines,
        "items": items,
    }
    if queue_for_gateway:
        payload["gateway_pull_path"] = EXECUTION_GATEWAY_PENDING_PATH
    _persist_tail_market_scan(meeting_state_store, payload)
    return payload


def build_postclose_review_board_summary(
    *,
    inspection_payload: dict | None = None,
    tail_market_payload: dict | None = None,
    discussion_context: dict | None = None,
) -> dict:
    inspection_payload = inspection_payload or {}
    tail_market_payload = tail_market_payload or {}
    discussion_context = discussion_context or {}
    client_brief = dict(discussion_context.get("client_brief") or {})
    finalize_packet = dict(discussion_context.get("finalize_packet") or {})
    execution_precheck = dict(
        finalize_packet.get("execution_precheck")
        or client_brief.get("execution_precheck")
        or {}
    )
    matched_tail_items = [
        item for item in list(tail_market_payload.get("items") or [])
        if item.get("exit_reason")
    ]
    discussion_status = str(
        client_brief.get("status")
        or discussion_context.get("status")
        or finalize_packet.get("status")
        or "unavailable"
    )
    summary_lines = [
        (
            f"盘后 review board 摘要: 治理高优先级 {int(inspection_payload.get('high_priority_action_item_count', 0) or 0)} 项，"
            f"tail-market 命中 {len(matched_tail_items)} 项，discussion 状态 {discussion_status}。"
        )
    ]
    if inspection_payload.get("summary_lines"):
        summary_lines.append("治理: " + str((inspection_payload.get("summary_lines") or [""])[0]))
    if tail_market_payload.get("summary_lines"):
        summary_lines.append("尾盘: " + str((tail_market_payload.get("summary_lines") or [""])[0]))
    elif matched_tail_items:
        lead = matched_tail_items[0]
        summary_lines.append(
            f"尾盘: {lead.get('symbol')} 命中 {lead.get('exit_reason')}，tags={','.join(list(lead.get('review_tags') or [])[:3])}。"
        )
    if client_brief.get("lines"):
        summary_lines.append("讨论: " + str((client_brief.get("lines") or [""])[0]))
    elif finalize_packet.get("summary_lines"):
        summary_lines.append("讨论: " + str((finalize_packet.get("summary_lines") or [""])[0]))
    return {
        "available": bool(inspection_payload or tail_market_payload or discussion_context),
        "trade_date": (
            discussion_context.get("trade_date")
            or tail_market_payload.get("trade_date")
        ),
        "discussion_status": discussion_status,
        "counts": {
            "governance_action_item_count": int(inspection_payload.get("action_item_count", 0) or 0),
            "governance_high_priority_action_item_count": int(
                inspection_payload.get("high_priority_action_item_count", 0) or 0
            ),
            "tail_market_count": len(matched_tail_items),
            "discussion_blocked_count": int(execution_precheck.get("blocked_count", 0) or 0),
        },
        "summary_lines": summary_lines,
    }


@dataclass
class Scheduler:
    tasks: list[ScheduledTask] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tasks = ALL_TASKS

    def list_tasks(self) -> list[dict]:
        return [{"name": t.name, "cron": t.cron, "enabled": t.enabled} for t in self.tasks]


class APSchedulerRunner:
    """基于 APScheduler 的真实调度器"""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._scheduler = None
        self._task_registry: dict[str, Callable] = {}

    def register(self, handler_path: str, fn: Callable) -> None:
        """注册任务处理函数"""
        self._task_registry[handler_path] = fn

    def _build_scheduler(self):
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        sched = BlockingScheduler(timezone="Asia/Shanghai")
        for task in ALL_TASKS:
            if not task.enabled:
                continue
            fn = self._task_registry.get(task.handler)
            if fn is None:
                fn = self._make_stub(task.name, task.handler)
            parts = task.cron.split()
            trigger = CronTrigger(
                minute=parts[0], hour=parts[1],
                day=parts[2], month=parts[3], day_of_week=parts[4],
                timezone="Asia/Shanghai",
            )
            job_id = f"{task.handler}:{task.cron}"
            sched.add_job(fn, trigger=trigger, id=job_id, name=task.name, misfire_grace_time=60)
            logger.info("注册任务: [%s] %s", task.cron, task.name)
        return sched

    def _make_stub(self, name: str, handler: str) -> Callable:
        def stub():
            if self.dry_run:
                logger.info("[dry-run] 任务触发: %s (%s)", name, handler)
            else:
                logger.warning("任务未注册处理函数: %s (%s)", name, handler)
        return stub

    def start(self) -> None:
        self._scheduler = self._build_scheduler()
        mode = "dry-run" if self.dry_run else "live"
        logger.info("APScheduler 启动 (mode=%s, tasks=%d)", mode, len(ALL_TASKS))
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown()
            logger.info("APScheduler 已停止")


def run_scheduler(dry_run: bool = False) -> None:
    """CLI 入口: 启动调度器，自动注册所有业务处理函数"""
    from .container import (
        get_audit_store,
        get_candidate_case_service,
        get_execution_adapter,
        get_market_adapter,
        get_message_dispatcher,
        get_meeting_state_store,
        get_monitor_state_service,
        get_parameter_service,
        get_research_state_store,
        get_runtime_config_manager,
        get_runtime_state_store,
        get_settings,
    )
    from .data.serving import ServingStore
    from .data.fetcher import DataFetcher, DataPipeline
    from .data.special import SpecialDataFetcher
    from .pending_order_remediation import PendingOrderRemediationService
    from .pending_order_inspection import PendingOrderInspectionService
    from .sentiment.calculator import SentimentCalculator
    from .monitor.alert_engine import AlertEngine
    from .monitor.stock_pool import StockPoolManager
    from .report.daily import DailyReporter

    settings = get_settings()
    market = get_market_adapter()
    fetcher = DataFetcher(market)
    pipeline = DataPipeline(fetcher, settings.storage_root / "cache")
    sentiment = SentimentCalculator()
    special = SpecialDataFetcher()
    alert_engine = AlertEngine()
    pool_mgr = StockPoolManager()
    audit_store = get_audit_store()
    runtime_state = get_runtime_state_store()
    research_state = get_research_state_store()
    meeting_state_store = get_meeting_state_store()
    serving_store = ServingStore(settings.storage_root)
    config_mgr = get_runtime_config_manager()
    parameter_service = get_parameter_service()
    trade_attribution_service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
    candidate_case_service = get_candidate_case_service()
    monitor_state_service = get_monitor_state_service()
    execution_adapter = get_execution_adapter()

    # 飞书通知
    dispatcher = get_message_dispatcher()
    reporter = DailyReporter(dispatcher=dispatcher)
    monitor_change_notifier = MonitorChangeNotifier(monitor_state_service, dispatcher)
    pending_order_inspection_service = PendingOrderInspectionService(execution_adapter, meeting_state_store)
    pending_order_remediation_service = PendingOrderRemediationService(
        execution_adapter,
        meeting_state_store,
        pending_order_inspection_service,
    )
    dossier_precompute_service = DossierPrecomputeService(
        settings=settings,
        market_adapter=market,
        research_state_store=research_state,
        runtime_state_store=runtime_state,
        candidate_case_service=candidate_case_service,
        config_mgr=config_mgr,
    )
    reverse_repo_service = ReverseRepoService(
        settings=settings,
        execution_adapter=execution_adapter,
        market_adapter=market,
        state_store=runtime_state,
        config_mgr=config_mgr,
        parameter_service=parameter_service,
        dispatcher=dispatcher,
    )

    # 共享状态
    _profile = [None]  # 当日情绪画像

    runner = APSchedulerRunner(dry_run=dry_run)

    def record_audit(category: str, message: str, payload: dict | None = None) -> None:
        audit_store.append(category=category, message=message, payload=payload or {})

    def persist_scheduler_runtime_snapshot(runtime_snapshot: dict, source: str) -> dict:
        history = runtime_state.get("runtime_jobs", [])
        history.append(runtime_snapshot)
        runtime_state.set("latest_runtime_report", runtime_snapshot)
        runtime_state.set("runtime_jobs", history[-30:])

        focus_pool_capacity = int(parameter_service.get_param_value("focus_pool_capacity"))
        execution_pool_capacity = int(parameter_service.get_param_value("execution_pool_capacity"))
        synced_cases = candidate_case_service.sync_from_runtime_report(
            runtime_snapshot,
            focus_pool_capacity=focus_pool_capacity,
            execution_pool_capacity=execution_pool_capacity,
        )
        payload = {
            "synced_case_count": len(synced_cases),
            "monitor_snapshot_saved": False,
            "dossier_pack_id": None,
            "dossier_reused": False,
        }
        if synced_cases:
            trade_date = synced_cases[0].trade_date
            monitor_state_service.save_pool_snapshot(
                trade_date=trade_date,
                pool_snapshot=candidate_case_service.build_pool_snapshot(trade_date),
                source=source,
            )
            monitor_change_notifier.dispatch_latest()
            dossier = dossier_precompute_service.precompute(
                trade_date=trade_date,
                source="candidate_pool",
            )
            monitor_state_service.mark_poll_if_due("candidate", trigger=source, force=True)
            payload.update(
                {
                    "trade_date": trade_date,
                    "monitor_snapshot_saved": True,
                    "dossier_pack_id": dossier.get("pack_id"),
                    "dossier_reused": dossier.get("reused", False),
                }
            )
        return payload

    def refresh_idle_dossier(trigger: str) -> dict | None:
        refresh = dossier_precompute_service.refresh_if_due(
            source="candidate_pool",
            trigger=trigger,
        )
        if not refresh.get("refreshed"):
            return None
        audit_payload = {
            "trigger": trigger,
            "reason": refresh.get("reason"),
            "trade_date": refresh.get("trade_date"),
            "symbol_count": refresh.get("symbol_count"),
            "pack_id": refresh.get("pack_id"),
            "reused": refresh.get("reused", False),
            "expires_at": refresh.get("expires_at"),
        }
        record_audit("precompute", "完成候选 dossier 闲时预热", audit_payload)
        return audit_payload

    # ── 注册业务处理函数 ──
    def task_compute_daily():
        _profile[0] = sentiment.calc_from_market_data(market, special)
        if _profile[0]:
            dispatcher.dispatch("sentiment", "情绪评估", f"阶段={_profile[0].sentiment_phase} 得分={_profile[0].sentiment_score:.0f}")
            record_audit(
                "sentiment",
                "更新市场情绪画像",
                {
                    "phase": _profile[0].sentiment_phase,
                    "score": _profile[0].sentiment_score,
                    "position_ceiling": _profile[0].position_ceiling,
                },
            )

    def task_check_once():
        try:
            universe = fetcher.fetch_universe("main-board")[:200]
            snaps = fetcher.fetch_snapshots(universe)
            alerts = alert_engine.check_batch(snaps)
            monitor_state_service.record_alert_events(alerts, snaps)
            heartbeat = monitor_state_service.save_heartbeat_if_due(snaps, alerts, trigger="scheduler_check")
            candidate_poll = monitor_state_service.mark_poll_if_due("candidate", trigger="scheduler_check")
            focus_poll = monitor_state_service.mark_poll_if_due("focus", trigger="scheduler_check")
            execution_poll = monitor_state_service.mark_poll_if_due("execution", trigger="scheduler_check")
            precompute_refresh = (
                dossier_precompute_service.refresh_if_due(
                    source="candidate_pool",
                    trigger="scheduler_check",
                )
                if candidate_poll.get("triggered")
                else {
                    "ok": True,
                    "refreshed": False,
                    "trigger": "scheduler_check",
                    "reason": "candidate_poll_skip",
                }
            )
            pending_order_inspection = (
                pending_order_inspection_service.inspect(
                    settings.xtquant.account_id,
                    warn_after_seconds=int(getattr(config_mgr.get(), "pending_order_warn_seconds", 300) or 300),
                    persist=True,
                )
                if execution_poll.get("triggered")
                else {
                    "status": "skipped",
                    "pending_count": 0,
                    "warning_count": 0,
                    "stale_count": 0,
                }
            )
            pending_order_remediation = (
                pending_order_remediation_service.remediate(
                    settings.xtquant.account_id,
                    auto_action=str(getattr(config_mgr.get(), "pending_order_auto_action", "alert_only") or "alert_only"),
                    cancel_after_seconds=int(getattr(config_mgr.get(), "pending_order_cancel_after_seconds", 900) or 900),
                    persist=True,
                )
                if execution_poll.get("triggered")
                else {
                    "status": "skipped",
                    "stale_count": 0,
                    "actioned_count": 0,
                    "cancelled_count": 0,
                }
            )
            reverse_repo_repurchase = (
                reverse_repo_service.inspect(
                    settings.xtquant.account_id,
                    auto_submit=settings.run_mode == "live",
                    persist=True,
                )
                if execution_poll.get("triggered")
                else {
                    "status": "skipped",
                    "reason": "execution_poll_skip",
                }
            )
            for a in alerts:
                dispatcher.dispatch_alert(a.message)
            if (
                settings.run_mode == "live"
                and settings.notify.alerts_enabled
                and pending_order_inspection.get("status") in {"warning", "error"}
            ):
                dispatcher.dispatch_alert("\n".join(pending_order_inspection.get("summary_lines", [])))
            if (
                settings.run_mode == "live"
                and settings.notify.alerts_enabled
                and pending_order_remediation.get("actioned_count", 0) > 0
            ):
                dispatcher.dispatch_alert("\n".join(pending_order_remediation.get("summary_lines", [])))
            record_audit(
                "monitor",
                "完成盯盘巡检",
                {
                    "alert_count": len(alerts),
                    "checked_symbols": len(universe),
                    "heartbeat_saved": bool(heartbeat),
                    "candidate_poll": candidate_poll,
                    "focus_poll": focus_poll,
                    "execution_poll": execution_poll,
                    "pending_order_inspection": {
                        "status": pending_order_inspection.get("status"),
                        "pending_count": pending_order_inspection.get("pending_count", 0),
                        "warning_count": pending_order_inspection.get("warning_count", 0),
                        "stale_count": pending_order_inspection.get("stale_count", 0),
                    },
                    "pending_order_remediation": {
                        "status": pending_order_remediation.get("status"),
                        "stale_count": pending_order_remediation.get("stale_count", 0),
                        "actioned_count": pending_order_remediation.get("actioned_count", 0),
                        "cancelled_count": pending_order_remediation.get("cancelled_count", 0),
                    },
                    "reverse_repo_repurchase": {
                        "status": reverse_repo_repurchase.get("status"),
                        "reason": reverse_repo_repurchase.get("reason"),
                        "selected_symbol": ((reverse_repo_repurchase.get("selected_candidate") or {}).get("symbol")),
                        "submitted_order_id": ((reverse_repo_repurchase.get("submitted_order") or {}).get("order_id")),
                    },
                    "dossier_refreshed": precompute_refresh.get("refreshed", False),
                    "dossier_refresh_reason": precompute_refresh.get("reason"),
                },
            )
        except Exception as e:
            logger.warning("盯盘巡检失败: %s", e)

    def task_fetch_daily():
        try:
            universe = fetcher.fetch_universe("main-board")
            pipeline.get_daily_bars(universe[:100])
            refresh_idle_dossier("fetch_daily")
            record_audit("data", "完成日终数据拉取", {"symbol_count": min(len(universe), 100)})
        except Exception as e:
            logger.warning("日终数据拉取失败: %s", e)

    def task_fetch_news():
        universe = fetcher.fetch_universe("main-board")[:20]
        sync_item = {
            "symbols": universe,
            "requested_at": datetime.now().isoformat(),
            "news_count": len(research_state.get("news", [])),
            "announcement_count": len(research_state.get("announcements", [])),
        }
        history = research_state.get("sync_history", [])
        history.append(sync_item)
        research_state.set("sync_history", history[-50:])
        refresh_idle_dossier("fetch_news")
        record_audit("research", "完成盘前新闻同步任务", sync_item)

    def task_refresh_behavior_profiles():
        payload = dossier_precompute_service.refresh_behavior_profiles(
            source="candidate_pool",
            trigger="scheduler_refresh_profiles",
        )
        record_audit(
            "precompute",
            "完成股性画像独立刷新",
            {
                "trigger": payload.get("trigger"),
                "trade_date": payload.get("trade_date"),
                "source": payload.get("source"),
                "symbol_count": payload.get("symbol_count", 0),
                "profile_count": payload.get("profile_count", 0),
                "coverage_ratio": payload.get("coverage_ratio", 0.0),
                "source_counts": payload.get("source_counts", {}),
                "missing_symbols": payload.get("missing_symbols", []),
                "refreshed": payload.get("refreshed", False),
                "reason": payload.get("reason"),
            },
        )

    def task_parameter_hint_inspection():
        payload = collect_parameter_hint_inspection(
            parameter_service=parameter_service,
            trade_attribution_service=trade_attribution_service,
            trade_date=None,
            score_date=None,
            statuses="evaluating,approved,effective",
            due_within_days=1,
            limit=50,
        )
        if (
            settings.run_mode == "live"
            and settings.notify.alerts_enabled
            and (
                payload.get("pending_high_risk_rollback_count", 0) > 0
                or payload.get("observation_overdue_count", 0) > 0
            )
        ):
            dispatcher.dispatch_alert("\n".join(payload.get("summary_lines", [])))
        review_board_summary = build_postclose_review_board_summary(
            inspection_payload=payload,
            tail_market_payload=(
                meeting_state_store.get("latest_tail_market_scan", {})
                if meeting_state_store
                else {}
            ),
            discussion_context=(
                meeting_state_store.get("latest_discussion_context", {})
                if meeting_state_store
                else {}
            ),
        )
        if meeting_state_store:
            meeting_state_store.set("latest_review_board_summary", review_board_summary)
        record_audit(
            "governance",
            "完成参数治理巡检",
            {
                "inspected_count": payload.get("inspected_count", 0),
                "statuses": payload.get("statuses", []),
                "due_within_days": payload.get("due_within_days", 1),
                "pending_high_risk_rollback_count": payload.get("pending_high_risk_rollback_count", 0),
                "observation_near_due_count": payload.get("observation_near_due_count", 0),
                "observation_overdue_count": payload.get("observation_overdue_count", 0),
                "action_item_count": payload.get("action_item_count", 0),
                "high_priority_action_item_count": payload.get("high_priority_action_item_count", 0),
                "recommended_action_counts": payload.get("recommended_action_counts", {}),
                "summary_lines": payload.get("summary_lines", []),
                "review_board_summary": review_board_summary,
            },
        )

    def task_daily_report():
        from .report.daily import DailyReportData
        profile = _profile[0] or MarketProfile(sentiment_phase="回暖")
        data = DailyReportData(date=date.today().isoformat(), profile=profile)
        reporter.generate(data)
        record_audit("report", "生成日终复盘报告", {"report_date": data.date})

    def task_run_pipeline():
        try:
            universe = fetcher.fetch_universe("main-board")[:200]
            from .strategy.screener import StockScreener
            screener = StockScreener()
            result = screener.run(universe, _profile[0], runtime_config=config_mgr.get())
            snapshots = fetcher.fetch_snapshots(result.passed[:30])
            snapshot_map = {item.symbol: item for item in snapshots}
            score_map = {symbol: float(max(len(result.passed) - index, 1)) for index, symbol in enumerate(result.passed)}
            name_map = {
                symbol: (snapshot_map[symbol].name if symbol in snapshot_map and snapshot_map[symbol].name else market.get_symbol_name(symbol))
                for symbol in result.passed
            }
            pool_mgr.update(result.passed, score_map, names=name_map)
            runtime_snapshot = {
                "job_id": f"scheduler-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "job_type": "scheduler_pipeline",
                "generated_at": datetime.now().isoformat(),
                "selected_symbols": result.passed,
                "decision_count": len(result.passed),
                "top_picks": [
                    {
                        "symbol": symbol,
                        "name": name_map[symbol],
                        "rank": index + 1,
                        "selection_score": score_map[symbol],
                        "action": "BUY",
                        "summary": f"{name_map[symbol] or symbol} 进入日终候选池，等待多 agent 讨论进一步收敛。",
                        "score_breakdown": {
                            "scheduler_rank_score": score_map[symbol],
                        },
                        "market_snapshot": (
                            {
                                "last_price": snapshot_map[symbol].last_price,
                                "pre_close": snapshot_map[symbol].pre_close,
                                "volume": snapshot_map[symbol].volume,
                            }
                            if symbol in snapshot_map
                            else {}
                        ),
                    }
                    for index, symbol in enumerate(result.passed)
                ],
            }
            sync_payload = persist_scheduler_runtime_snapshot(runtime_snapshot, source="scheduler_pipeline")
            record_audit(
                "runtime",
                "调度器完成选股评分",
                {
                    **runtime_snapshot,
                    **sync_payload,
                },
            )
        except Exception as e:
            logger.warning("选股评分失败: %s", e)

    def task_tail_market():
        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=execution_adapter,
            meeting_state_store=meeting_state_store,
            runtime_state_store=runtime_state,
            candidate_case_service=candidate_case_service,
            dispatcher=dispatcher,
            runtime_context=(
                serving_store.get_latest_runtime_context()
                or runtime_state.get("latest_runtime_context", {})
                or {}
            ),
            discussion_context=(
                serving_store.get_latest_discussion_context()
                or meeting_state_store.get("latest_discussion_context", {})
                or {}
            ),
        )
        record_audit(
            "risk",
            "完成尾盘决策检查",
            payload,
        )

    def task_factor_compute():
        latest = runtime_state.get("latest_runtime_report", {})
        record_audit("factors", "完成因子批处理任务", {"linked_job_id": latest.get("job_id")})

    def task_dragon_tiger():
        record_audit("monitor", "完成龙虎榜分析任务", {"executed_at": datetime.now().isoformat()})

    def task_execute_open():
        latest = runtime_state.get("latest_runtime_report", {})
        record_audit(
            "execution",
            "开盘执行任务进入纸面阻断",
            {"run_mode": settings.run_mode, "latest_job_id": latest.get("job_id"), "execution_mode": settings.execution_mode},
        )

    def task_pre_confirm():
        latest = runtime_state.get("latest_runtime_report", {})
        record_audit("risk", "完成买入清单预确认", {"selected_symbols": latest.get("selected_symbols", [])})

    def task_reverse_repo_repurchase():
        payload = reverse_repo_service.inspect(
            settings.xtquant.account_id,
            auto_submit=settings.run_mode == "live",
            persist=True,
        )
        record_audit(
            "execution",
            "完成逆回购自动回补巡检",
            {
                "status": payload.get("status"),
                "reason": payload.get("reason"),
                "selected_symbol": ((payload.get("selected_candidate") or {}).get("symbol")),
                "submitted_order_id": ((payload.get("submitted_order") or {}).get("order_id")),
                "reverse_repo_gap_value": payload.get("reverse_repo_gap_value"),
            },
        )

    runner.register("sentiment.calculator:compute_daily", task_compute_daily)
    runner.register("sentiment.calculator:pre_market", task_compute_daily)
    runner.register("sentiment.calculator:midday_snapshot", task_compute_daily)
    runner.register("monitor.market_watcher:check_once", task_check_once)
    runner.register("data.fetcher:fetch_news", task_fetch_news)
    runner.register("data.fetcher:fetch_daily", task_fetch_daily)
    runner.register("strategy.stock_profile:refresh", task_refresh_behavior_profiles)
    runner.register("governance.parameter_hints:inspection", task_parameter_hint_inspection)
    runner.register("report.daily:generate", task_daily_report)
    runner.register("strategy.screener:run_pipeline", task_run_pipeline)
    runner.register("strategy.screener:refresh", task_run_pipeline)
    runner.register("strategy.buy_decision:generate_buy_list", task_run_pipeline)
    runner.register("strategy.buy_decision:execute_open", task_execute_open)
    runner.register("strategy.buy_decision:pre_confirm", task_pre_confirm)
    runner.register("execution.reverse_repo:auto_repurchase", task_reverse_repo_repurchase)
    runner.register("strategy.sell_decision:tail_market", task_tail_market)
    runner.register("factors.engine:compute_all", task_factor_compute)
    runner.register("monitor.dragon_tiger:analyze", task_dragon_tiger)

    try:
        runner.start()
    except (KeyboardInterrupt, SystemExit):
        runner.shutdown()
