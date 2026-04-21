"""调度器 — APScheduler 真实调度，盘前/盘中/盘后时间表"""

from __future__ import annotations

import importlib
import json
import os
import time
import httpx
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pandas as pd

from .contracts import (
    EventFetchResult,
    ExitContext,
    MarketProfile,
    MarketEvent,
    PlaceOrderRequest,
    PlaybookOverrideSnapshot,
    PositionSnapshot,
    QuoteSnapshot,
    SectorProfile,
    StockBehaviorProfile,
)
from .data.archive import DataArchiveStore
from .data.catalog_service import CatalogService
from .data.control_db import ControlPlaneDB
from .data.contracts import EventRecord
from .data.freshness import DataFreshnessMonitor, build_freshness_meta
from .data.history_ingest import HistoryIngestService
from .data.history_store import HistoryStore
from .data.serving import ServingStore
from .execution.order_strategy import OrderExecutionPlan, OrderStrategyResolver
from .execution.quality_tracker import ExecutionQualityTracker
from .execution_gateway import (
    EXECUTION_GATEWAY_PENDING_PATH,
    enqueue_execution_gateway_intent,
    resolve_execution_gateway_state_store,
)
from .execution_safety import is_limit_up, is_trading_session
from .governance.inspection import collect_parameter_hint_inspection
from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore, _atomic_write_json, _file_lock, _load_json_payload
from .infra.circuit_breaker import CircuitBreakerRegistry
from .infra.market_adapter import MarketDataAdapter
from .infra.state_migration import migrate_legacy_state_files
from .infra.trace import TradeTraceService
from .backtest.live_drift import LiveBacktestDriftTracker
from .learning.attribution import TradeAttributionService
from .logging_config import get_logger
from .market.regime_detector import detect_market_regime, detect_regime_transition
from .monitor.latency_tracker import get_latency_tracker_snapshot, record_latency_sample
from .notify.monitor_changes import MonitorChangeNotifier
from .notify.templates import (
    agent_supervision_template,
    execution_order_event_template,
    latency_alert_notification_template,
    position_watch_notification_template,
)
from .precompute import DossierPrecomputeService
from .reverse_repo import ReverseRepoService
from .settings import AppSettings
from .strategy.day_trading import DayTradingEngine
from .strategy.default_runtime_strategy import (
    apply_market_alignment_order as apply_default_market_alignment_order,
    apply_playbook_order as apply_default_playbook_order,
    build_leader_ranks as build_default_leader_ranks,
    build_routing_market_profile as build_default_routing_market_profile,
    infer_behavior_profiles as infer_default_behavior_profiles,
    infer_sector_profiles as infer_default_sector_profiles,
    resolve_symbol_sector_map as resolve_default_symbol_sector_map,
    score_runtime_snapshot,
)
from .strategy.leader_rank import LeaderRanker
from .strategy.router import StrategyRouter
from .strategy.sell_decision import PositionState, SellDecisionEngine, SellReason, SellSignal
from .supervision_state import annotate_supervision_payload, record_supervision_notification
from .supervision_tasks import build_agent_task_plan, record_agent_task_dispatch

logger = get_logger("scheduler")
PLAYBOOK_OVERRIDE_STORAGE_FILE = Path("learning") / "playbook_overrides.json"
AGENT_AUTONOMY_CONTROL_KEY_PREFIX = "agent_autonomy_control:"
REGIME_CONFIRMATION_DAYS = 3
OFFENSIVE_PLAYBOOKS = {
    "leader_chase",
    "divergence_reseal",
    "sector_reflow_first_board",
    "limit_up_relay",
    "dragon_relay",
}


@dataclass
class ScheduledTask:
    name: str
    handler: str
    cron: str = ""
    enabled: bool = True
    interval_seconds: int | None = None


# 盘前任务 (07:30 - 09:15, 周一至周五)
PRE_MARKET_TASKS = [
    ScheduledTask(name="数据新鲜度巡检", cron="20 7 * * 1-5", handler="data.freshness:check"),
    ScheduledTask(name="新闻扫描",    cron="30 7 * * 1-5",  handler="data.fetcher:fetch_news"),
    ScheduledTask(name="竞价预分析",  cron="0 8 * * 1-5",   handler="sentiment.calculator:pre_market"),
    ScheduledTask(name="环境评分",    cron="0 9 * * 1-5",   handler="sentiment.calculator:compute_daily"),
    ScheduledTask(name="买入清单",    cron="10 9 * * 1-5",  handler="strategy.buy_decision:generate_buy_list"),
    ScheduledTask(name="竞价快照09:20", cron="20 9 * * 1-5", handler="strategy.auction:scan_0920"),
    ScheduledTask(name="竞价快照09:24", cron="24 9 * * 1-5", handler="strategy.auction:scan_0924"),
    ScheduledTask(name="逆回购开盘回补", cron="35 9 * * 1-5", handler="execution.reverse_repo:auto_repurchase"),
]

# 盘中任务 (09:30 - 15:00)
INTRADAY_TASKS = [
    ScheduledTask(name="开盘执行",    cron="30 9 * * 1-5",  handler="strategy.buy_decision:execute_open"),
    ScheduledTask(name="执行窗口推进", cron="*/1 9-14 * * 1-5", handler="execution.window:advance"),
    ScheduledTask(name="持仓快巡视", interval_seconds=3, handler="position.watch:fast_realtime"),
    ScheduledTask(name="持仓深巡视", interval_seconds=30, handler="position.watch:check_realtime"),
    ScheduledTask(name="桥健康守护", interval_seconds=30, handler="execution.bridge_guardian:check"),
    ScheduledTask(name="盯盘巡检",    cron="*/5 10-14 * * 1-5", handler="monitor.market_watcher:check_once"),
    ScheduledTask(name="微观巡检",    cron="*/1 9-14 * * 1-5", handler="monitor.market_watcher:check_micro"),
    ScheduledTask(name="Agent监督巡检", cron="*/3 9-15 * * 1-5", handler="supervision.agent:check"),
    ScheduledTask(name="Agent自主起手", cron="*/4 9-15 * * 1-5", handler="autonomy.agent:runtime_bootstrap"),
    ScheduledTask(name="午间快照",    cron="30 11 * * 1-5", handler="sentiment.calculator:midday_snapshot"),
    ScheduledTask(name="午后刷新",    cron="0 13 * * 1-5",  handler="strategy.screener:refresh"),
    ScheduledTask(name="尾盘决策",    cron="30 14 * * 1-5", handler="strategy.sell_decision:tail_market"),
]

# 盘后任务 (15:00 - 23:00)
# 市场动作继续限制在交易日；研究、治理、学习与回放任务允许每日运行。
POST_MARKET_TASKS = [
    ScheduledTask(name="日终数据拉取", cron="30 15 * * 1-5", handler="data.fetcher:fetch_daily"),
    ScheduledTask(name="日线入湖",     cron="40 15 * * 1-5", handler="history.ingest:daily"),
    ScheduledTask(name="因子计算",     cron="45 15 * * 1-5", handler="factors.engine:compute_all"),
    ScheduledTask(name="日终复盘",     cron="0 16 * * 1-5",  handler="report.daily:generate"),
    ScheduledTask(name="挂单治理",     cron="0 16 * * 1-5",  handler="execution.stale_order:cleanup"),
    ScheduledTask(name="执行对账",     cron="10 16 * * 1-5", handler="execution.reconciliation:run"),
    ScheduledTask(name="学分结算",     cron="30 16 * * *",   handler="learning.score_state:daily_settlement"),
    ScheduledTask(name="Prompt进化",   cron="45 16 * * *",   handler="learning.prompt_patcher:daily_patch"),
    ScheduledTask(name="龙虎榜分析",   cron="0 17 * * *",    handler="monitor.dragon_tiger:analyze"),
    ScheduledTask(name="注册表权重覆写", cron="0 17 * * *",  handler="learning.registry_updater:update_weights"),
    ScheduledTask(name="策略自进化",   cron="15 17 * * *",   handler="learning.self_evolve:suggest"),
    ScheduledTask(name="股性画像刷新", cron="30 17 * * *",   handler="strategy.stock_profile:refresh"),
    ScheduledTask(name="股性画像入库", cron="40 17 * * *",   handler="history.ingest:behavior_profiles"),
    ScheduledTask(name="增量学习回放", cron="30 17 * * *",   handler="learning.continuous:validate"),
    ScheduledTask(name="参数治理巡检", cron="0 18 * * *",    handler="governance.parameter_hints:inspection"),
    ScheduledTask(name="分钟线入湖",   cron="10 18 * * 1-5", handler="history.ingest:minute"),
    ScheduledTask(name="持仓压力测试", cron="0 19 * * *",    handler="risk.stress_test:run"),
    ScheduledTask(name="次日新闻扫描", cron="0 20 * * *",    handler="data.fetcher:fetch_news"),
    ScheduledTask(name="账本回测验证", cron="30 20 * * 1-5", handler="strategy.evaluation_ledger:reconcile_backtest"),
    ScheduledTask(name="因子有效性巡检", cron="10 20 * * 1-5", handler="strategy.factor_monitor:refresh"),
    ScheduledTask(name="选股评分",     cron="0 21 * * 1-5",  handler="strategy.screener:run_pipeline"),
    ScheduledTask(name="买入清单预确认", cron="0 22 * * 1-5", handler="strategy.buy_decision:pre_confirm"),
    ScheduledTask(name="夜间沙盘推演", cron="0 23 * * *",    handler="strategy.nightly_sandbox:run"),
]

ALL_TASKS = PRE_MARKET_TASKS + INTRADAY_TASKS + POST_MARKET_TASKS


def _normalize_crontab_day_of_week(expr: str) -> str:
    """把传统 crontab 星期编号转换为 APScheduler/CronTrigger 语义。

    crontab: 0/7=Sunday, 1=Monday, ..., 6=Saturday
    APScheduler: 0=Monday, ..., 6=Sunday
    """

    normalized = str(expr or "").strip()
    if not normalized or any(ch.isalpha() for ch in normalized):
        return normalized

    def map_token(token: str) -> str:
        if token == "*":
            return token
        if "/" in token:
            base, step = token.split("/", 1)
            return f"{map_token(base)}/{step}"
        if "," in token:
            return ",".join(map_token(part) for part in token.split(","))
        if "-" in token:
            left, right = token.split("-", 1)
            return f"{map_token(left)}-{map_token(right)}"

        value = int(token)
        if value in {0, 7}:
            return "6"
        if 1 <= value <= 6:
            return str(value - 1)
        return token

    return map_token(normalized)


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


def _resolve_agent_autonomy_timeout_sec() -> float:
    raw_value = str(os.getenv("ASHARE_AGENT_AUTONOMY_TIMEOUT_SEC", "") or "").strip()
    if not raw_value:
        return 180.0
    try:
        return min(max(float(raw_value), 20.0), 180.0)
    except ValueError:
        return 180.0


def _compose_record_matches_trade_date(record: dict[str, Any], trade_date: str) -> bool:
    normalized_trade_date = str(trade_date or "").strip()
    if not normalized_trade_date:
        return False
    if str(record.get("trade_date") or "").strip() == normalized_trade_date:
        return True
    generated_at = str(record.get("generated_at") or "").strip()
    if generated_at.startswith(normalized_trade_date):
        return True
    adoption = dict(record.get("adoption") or {})
    if str(adoption.get("trade_date") or "").strip() == normalized_trade_date:
        return True
    runtime_job = dict(record.get("runtime_job") or {})
    compact_trade_date = normalized_trade_date.replace("-", "")
    for case_id in list(runtime_job.get("case_ids") or []):
        if compact_trade_date and compact_trade_date in str(case_id or ""):
            return True
    return False


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


def _seconds_since(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
    return max((now - dt).total_seconds(), 0.0)


def _resolve_symbol_limit_pct(symbol: str) -> float:
    normalized = str(symbol or "").strip()
    if normalized.startswith(("300", "301", "688", "689")):
        return 0.2
    if normalized.startswith(("43", "83", "87", "88", "92")):
        return 0.3
    return 0.1


def _snapshot_change_pct(snapshot: Any) -> float:
    pre_close = float(getattr(snapshot, "pre_close", 0.0) or 0.0)
    last_price = float(getattr(snapshot, "last_price", 0.0) or 0.0)
    if pre_close <= 0 or last_price <= 0:
        return 0.0
    return (last_price - pre_close) / max(pre_close, 1e-9)


def _snapshot_turnover_amount(snapshot: Any) -> float:
    last_price = float(getattr(snapshot, "last_price", 0.0) or 0.0)
    volume = float(getattr(snapshot, "volume", 0.0) or 0.0)
    return max(last_price, 0.0) * max(volume, 0.0)


def _resolve_runtime_symbol_context_map(runtime_context: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = dict(runtime_context or {})
    context_map: dict[str, dict[str, Any]] = {}
    for item in list(payload.get("playbook_contexts") or []):
        candidate = dict(item or {})
        symbol = str(candidate.get("symbol") or "").strip()
        if symbol:
            context_map[symbol] = candidate
    for item in list(payload.get("top_picks") or []):
        candidate = dict(item or {})
        symbol = str(candidate.get("symbol") or "").strip()
        if not symbol:
            continue
        existing = dict(context_map.get(symbol) or {})
        if candidate.get("resolved_sector") and not existing.get("sector"):
            existing["sector"] = candidate.get("resolved_sector")
        score_breakdown = dict(candidate.get("score_breakdown") or {})
        if score_breakdown and not existing.get("score_breakdown"):
            existing["score_breakdown"] = score_breakdown
        context_map[symbol] = existing
    return context_map


def _build_regime_driven_candidate_payloads(
    *,
    fetcher,
    runtime_context: dict[str, Any] | None,
    workspace_context: dict[str, Any] | None,
    max_sectors: int = 3,
    max_candidates: int = 12,
) -> dict[str, Any]:
    runtime_payload = dict(runtime_context or {})
    workspace_payload = dict(workspace_context or {})
    market_context = dict(workspace_payload.get("market_context") or {})
    monitor_context = dict(workspace_payload.get("monitor_context") or {})
    detected_regime = detect_market_regime(
        market_context=dict((market_context.get("market_profile") or market_context) or {}),
        runtime_context=runtime_payload,
        monitor_context=monitor_context,
    )
    hot_sector_chain = [
        str(item).strip()
        for item in list(detected_regime.get("hot_sector_chain") or [])
        if str(item).strip()
    ]
    if not hot_sector_chain:
        hot_sector_chain = [
            str(item).strip()
            for item in list((runtime_payload.get("market_profile") or {}).get("hot_sectors") or [])
            if str(item).strip()
        ]
    if not hot_sector_chain:
        return {
            "available": False,
            "market_regime": detected_regime,
            "items": [],
            "summary_lines": ["未识别到可用热点链，跳过 regime_driven 候选注入。"],
        }

    symbol_context_map = _resolve_runtime_symbol_context_map(runtime_payload)
    all_sector_entries: list[dict[str, Any]] = []
    selected_items: list[dict[str, Any]] = []
    selected_symbols: set[str] = set()
    followed_sectors: list[str] = []

    def append_candidate(entry: dict[str, Any], source_role: str) -> None:
        symbol = str(entry.get("symbol") or "").strip()
        if not symbol or symbol in selected_symbols or len(selected_items) >= max_candidates:
            return
        selected_symbols.add(symbol)
        score = float(entry.get("selection_score") or 0.0)
        selected_items.append(
            {
                "symbol": symbol,
                "name": str(entry.get("name") or symbol),
                "selection_score": round(score, 4),
                "action": "BUY",
                "source": "regime_driven",
                "source_tags": ["regime_driven", str(entry.get("source_tag") or "hot_chain_probe")],
                "source_role": source_role,
                "resolved_sector": str(entry.get("resolved_sector") or "").strip(),
                "summary": str(entry.get("summary") or "").strip(),
                "headline_reason": str(entry.get("summary") or "").strip(),
                "score_breakdown": dict(entry.get("score_breakdown") or {}),
                "market_snapshot": dict(entry.get("market_snapshot") or {}),
                "market_profile": {
                    "regime": detected_regime.get("runtime_regime"),
                    "hot_sectors": hot_sector_chain,
                    "detected_regime": detected_regime,
                },
            }
        )

    for sector in hot_sector_chain[:max_sectors]:
        sector_symbols = list(dict.fromkeys(fetcher.fetch_sector_symbols(sector) or []))[:20]
        if not sector_symbols:
            continue
        followed_sectors.append(sector)
        snapshots = fetcher.fetch_snapshots(sector_symbols)
        ranked_entries: list[dict[str, Any]] = []
        for snapshot in snapshots:
            symbol = str(snapshot.symbol or "").strip()
            if not symbol:
                continue
            change_pct = _snapshot_change_pct(snapshot)
            turnover_amount = _snapshot_turnover_amount(snapshot)
            sector_context = dict(symbol_context_map.get(symbol) or {})
            resolved_sector = str(
                sector_context.get("sector")
                or ((sector_context.get("symbol_context") or {}).get("sector_name"))
                or sector
            ).strip()
            ranked_entries.append(
                {
                    "symbol": symbol,
                    "name": str(getattr(snapshot, "name", "") or symbol),
                    "resolved_sector": resolved_sector or sector,
                    "change_pct": change_pct,
                    "turnover_amount": turnover_amount,
                    "selection_score": change_pct * 100 + min(turnover_amount / 1_000_000_000.0, 20.0),
                    "market_snapshot": {
                        "last_price": float(getattr(snapshot, "last_price", 0.0) or 0.0),
                        "pre_close": float(getattr(snapshot, "pre_close", 0.0) or 0.0),
                        "volume": float(getattr(snapshot, "volume", 0.0) or 0.0),
                        "turnover_amount": turnover_amount,
                        "change_pct": round(change_pct, 6),
                    },
                }
            )
        ranked_entries.sort(
            key=lambda item: (
                float(item.get("change_pct", 0.0) or 0.0),
                float(item.get("turnover_amount", 0.0) or 0.0),
            ),
            reverse=True,
        )
        all_sector_entries.extend(ranked_entries)
        if not ranked_entries:
            continue
        leader = dict(ranked_entries[0])
        leader["source_tag"] = "sector_leader"
        leader["summary"] = (
            f"{sector} 热点链龙头候选，涨幅 {float(leader.get('change_pct') or 0.0):.2%}，"
            f"成交额代理 {float(leader.get('turnover_amount') or 0.0) / 100000000:.2f} 亿。"
        )
        leader["score_breakdown"] = {
            "sector_leader_bonus": 8.0,
            "change_pct": round(float(leader.get("change_pct") or 0.0), 6),
            "turnover_amount": round(float(leader.get("turnover_amount") or 0.0), 2),
        }
        append_candidate(leader, "scheduler.regime.leader")

        continuation_candidates = [
            item for item in ranked_entries[1:]
            if 0.02 <= float(item.get("change_pct", 0.0) or 0.0) < max(_resolve_symbol_limit_pct(str(item.get("symbol") or "")) - 0.01, 0.06)
        ]
        if continuation_candidates:
            continuation = dict(continuation_candidates[0])
            continuation["source_tag"] = "concept_follow"
            continuation["selection_score"] = float(continuation.get("selection_score") or 0.0) + 4.0
            continuation["summary"] = (
                f"{sector} 主线扩散跟踪票，当前涨幅 {float(continuation.get('change_pct') or 0.0):.2%}，"
                "尚未封板，适合盘前跟踪补涨。"
            )
            continuation["score_breakdown"] = {
                "concept_follow_bonus": 4.0,
                "change_pct": round(float(continuation.get("change_pct") or 0.0), 6),
                "turnover_amount": round(float(continuation.get("turnover_amount") or 0.0), 2),
            }
            append_candidate(continuation, "scheduler.regime.follow")

    money_flow_candidates = [
        item for item in all_sector_entries
        if str(item.get("symbol") or "").strip() not in selected_symbols and float(item.get("change_pct", 0.0) or 0.0) >= 0.01
    ]
    money_flow_candidates.sort(
        key=lambda item: (
            float(item.get("turnover_amount", 0.0) or 0.0) * max(float(item.get("change_pct", 0.0) or 0.0), 0.001),
            float(item.get("turnover_amount", 0.0) or 0.0),
        ),
        reverse=True,
    )
    for item in money_flow_candidates:
        enriched = dict(item)
        enriched["source_tag"] = "money_flow_anomaly"
        enriched["selection_score"] = float(enriched.get("selection_score") or 0.0) + 2.5
        enriched["summary"] = (
            f"{enriched.get('resolved_sector') or '热点方向'} 资金异动票，"
            f"涨幅 {float(enriched.get('change_pct') or 0.0):.2%}，成交额代理 {float(enriched.get('turnover_amount') or 0.0) / 100000000:.2f} 亿。"
        )
        enriched["score_breakdown"] = {
            "money_flow_bonus": 2.5,
            "change_pct": round(float(enriched.get("change_pct") or 0.0), 6),
            "turnover_amount": round(float(enriched.get("turnover_amount") or 0.0), 2),
        }
        append_candidate(enriched, "scheduler.regime.flow")
        if len(selected_items) >= max_candidates:
            break

    return {
        "available": bool(selected_items),
        "market_regime": detected_regime,
        "items": selected_items,
        "summary_lines": [
            (
                f"regime_driven 候选构建完成: regime={detected_regime.get('regime_label') or 'unknown'} "
                f"hot_sectors={len(hot_sector_chain)} injected={len(selected_items)}."
            ),
            (
                "热点链跟踪: " + " / ".join(followed_sectors[:4])
                if followed_sectors
                else "热点链跟踪: 无可用板块成分股数据。"
            ),
        ],
    }


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


def _persist_position_watch_scan(meeting_state_store: StateStore | None, payload: dict) -> None:
    if not meeting_state_store:
        return
    history = meeting_state_store.get("position_watch_history", [])
    history.append(
        {
            "scanned_at": payload.get("scanned_at"),
            "trade_date": payload.get("trade_date"),
            "account_id": payload.get("account_id"),
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "position_count": payload.get("position_count", 0),
            "sell_signal_count": payload.get("sell_signal_count", 0),
            "day_trading_signal_count": payload.get("day_trading_signal_count", 0),
            "submitted_count": payload.get("submitted_count", 0),
            "queued_count": payload.get("queued_count", 0),
            "preview_count": payload.get("preview_count", 0),
            "error_count": payload.get("error_count", 0),
        }
    )
    meeting_state_store.set("latest_position_watch_scan", payload)
    meeting_state_store.set("position_watch_history", history[-720:])


def _persist_intraday_rank_result(
    meeting_state_store: StateStore | None,
    runtime_state_store: StateStore | None,
    payload: dict,
) -> None:
    history_item = {
        "generated_at": payload.get("generated_at"),
        "trade_date": payload.get("trade_date"),
        "market_regime": payload.get("market_regime"),
        "candidate_count": payload.get("candidate_count", 0),
        "action_count": payload.get("action_count", 0),
        "freeze_count": payload.get("freeze_count", 0),
        "upgrade_count": payload.get("upgrade_count", 0),
        "downgrade_count": payload.get("downgrade_count", 0),
        "freeze_all": payload.get("freeze_all", False),
    }
    if meeting_state_store:
        history = meeting_state_store.get("intraday_rank_history", [])
        history.append(history_item)
        meeting_state_store.set("latest_intraday_rank_result", payload)
        meeting_state_store.set("intraday_rank_history", history[-60:])
    if runtime_state_store:
        history = runtime_state_store.get("intraday_rank_history", [])
        history.append(history_item)
        runtime_state_store.set("latest_intraday_rank_result", payload)
        runtime_state_store.set("intraday_rank_history", history[-60:])


def _load_intraday_ranker() -> object | None:
    try:
        module = importlib.import_module("ashare_system.monitor.intraday_ranker")
    except ModuleNotFoundError:
        return None
    ranker_cls = getattr(module, "IntradayRanker", None)
    if ranker_cls is None:
        return None
    try:
        return ranker_cls()
    except Exception:
        logger.exception("初始化 IntradayRanker 失败，将回退 scheduler 本地盘中重排规则。")
        return None


def _load_auto_governance() -> object | None:
    try:
        module = importlib.import_module("ashare_system.learning.auto_governance")
    except ModuleNotFoundError:
        return None
    governance_cls = getattr(module, "AutoGovernance", None)
    if governance_cls is None:
        return None
    try:
        return governance_cls()
    except Exception:
        logger.exception("初始化 AutoGovernance 失败，将跳过 playbook override 生成。")
        return None


def _load_event_fetcher() -> object | None:
    try:
        module = importlib.import_module("ashare_system.data.event_fetcher")
    except ModuleNotFoundError:
        return None
    fetcher_cls = getattr(module, "EventFetcher", None)
    if fetcher_cls is None:
        return None
    try:
        return fetcher_cls()
    except Exception:
        logger.exception("初始化 EventFetcher 失败，将跳过结构化事件抓取。")
        return None


def _as_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, dict) else {}
    return {}


def _playbook_override_storage_path(settings: AppSettings) -> Path:
    return settings.storage_root / PLAYBOOK_OVERRIDE_STORAGE_FILE


def _load_playbook_override_snapshot(settings: AppSettings) -> dict[str, Any]:
    storage_path = _playbook_override_storage_path(settings)
    if not storage_path.exists():
        return {}
    try:
        payload = json.loads(storage_path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("读取 playbook_overrides.json 失败，将忽略旧 override 快照。")
        return {}
    try:
        return PlaybookOverrideSnapshot.model_validate(payload).model_dump()
    except Exception:
        return _as_payload(payload)


def _persist_playbook_override_snapshot(
    settings: AppSettings,
    meeting_state_store: StateStore | None,
    runtime_state_store: StateStore | None,
    snapshot: Any,
) -> dict[str, Any]:
    payload = _as_payload(snapshot)
    if not payload:
        return {}
    storage_path = _playbook_override_storage_path(settings)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if meeting_state_store:
        meeting_state_store.set("latest_playbook_override_snapshot", payload)
    if runtime_state_store:
        runtime_state_store.set("latest_playbook_override_snapshot", payload)
    return payload


def _build_playbook_override_snapshot(
    *,
    settings: AppSettings,
    report: Any,
) -> dict[str, Any]:
    auto_governance = _load_auto_governance()
    if auto_governance is None:
        return {}
    builder = getattr(auto_governance, "build_override_snapshot", None)
    if not callable(builder):
        return {}
    previous_snapshot = _load_playbook_override_snapshot(settings) or None
    try:
        snapshot = builder(report=report, previous_snapshot=previous_snapshot)
    except Exception:
        logger.exception("生成 playbook override 快照失败，将跳过本次自动治理写回。")
        return {}
    return _as_payload(snapshot)


def _coerce_intraday_rank_actions(value: Any, generated_at: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        if isinstance(value.get("actions"), list):
            raw_items = value.get("actions", [])
        elif all(key in value for key in ("symbol", "action")):
            raw_items = [value]
        else:
            raw_items = []
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    actions: list[dict[str, Any]] = []
    for item in raw_items:
        payload = _as_payload(item)
        symbol = str(payload.get("symbol") or "").strip()
        action = str(payload.get("action") or "").strip().upper()
        if not symbol or not action:
            continue
        actions.append(
            {
                "symbol": symbol,
                "action": action,
                "reason": str(payload.get("reason") or "").strip(),
                "trigger": str(payload.get("trigger") or "").strip(),
                "priority_delta": int(payload.get("priority_delta", 0) or 0),
                "generated_at": str(payload.get("generated_at") or generated_at),
            }
        )
    return actions


def _event_is_negative(event: dict[str, Any]) -> bool:
    impact = str(event.get("impact") or "").strip().lower()
    sentiment = str(event.get("sentiment") or "").strip().lower()
    severity = str(event.get("severity") or "").strip().lower()
    tags = [str(tag).strip().lower() for tag in list(event.get("tags") or [])]
    title = str(event.get("title") or "").strip().lower()
    negative_tags = ("negative", "risk", "warning", "penalty", "fraud", "default", "downgrade", "bear")
    if impact in {"negative", "block"}:
        return True
    if sentiment in {"negative", "bearish", "risk_off"}:
        return True
    if severity in {"high", "critical", "block"} and any(tag for tag in tags if any(word in tag for word in negative_tags)):
        return True
    return any(word in title for word in negative_tags)


def _build_intraday_event_context(
    settings: AppSettings,
    event_context: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(event_context or {})
    if payload:
        return payload
    return ServingStore(settings.storage_root).get_latest_event_context() or {}


def _has_negative_event(
    *,
    symbol: str,
    event_context: dict[str, Any],
) -> bool:
    highlights = list(event_context.get("highlights") or [])
    by_scope = event_context.get("by_scope") or {}
    symbol_events = list(by_scope.get("symbol") or [])
    market_events = list(by_scope.get("market") or [])
    for event in [*symbol_events, *market_events, *highlights]:
        payload = _as_payload(event)
        event_symbol = str(payload.get("symbol") or "").strip()
        impact_scope = str(payload.get("impact_scope") or "").strip().lower()
        if impact_scope == "symbol" and event_symbol and event_symbol != symbol:
            continue
        if _event_is_negative(payload):
            return True
    return False


def _structured_event_dedupe_key(item: dict[str, Any]) -> str:
    return "|".join(
        [
            str(item.get("category") or "announcements"),
            str(item.get("symbol") or ""),
            str(item.get("source") or ""),
            str(item.get("title") or ""),
            str(item.get("event_at") or ""),
        ]
    )


def _structured_event_to_record(item: dict[str, Any], *, recorded_at: str) -> EventRecord:
    published_at = str(item.get("published_at") or recorded_at)
    category = str(item.get("category") or "announcements").strip().lower()
    if category not in {"news", "announcements", "policy"}:
        category = "announcements"
    freshness = build_freshness_meta(
        source_at=published_at,
        fetched_at=recorded_at,
        generated_at=recorded_at,
        fresh_seconds=300,
        warm_seconds=3600,
    )
    impact = str(item.get("impact") or "neutral").strip().lower()
    sentiment = (
        "negative"
        if impact in {"negative", "block"}
        else "positive"
        if impact == "positive"
        else "neutral"
    )
    payload = {
        "tags": list(item.get("tags") or []),
        "impact": impact,
        "event_type": str(item.get("event_type") or category),
    }
    return EventRecord(
        event_id=_structured_event_dedupe_key(
            {
                "category": category,
                "symbol": item.get("symbol"),
                "source": item.get("source"),
                "title": item.get("title"),
                "event_at": published_at,
            }
        ),
        symbol=str(item.get("symbol") or ""),
        name=str(item.get("name") or ""),
        source=str(item.get("source") or "event_fetcher"),
        source_type="event_fetcher",
        category=category,
        title=str(item.get("title") or ""),
        summary=str(item.get("summary") or ""),
        severity=str(item.get("severity") or "info"),
        sentiment=sentiment,
        event_at=published_at,
        recorded_at=recorded_at,
        dedupe_key=_structured_event_dedupe_key(
            {
                "category": category,
                "symbol": item.get("symbol"),
                "source": item.get("source"),
                "title": item.get("title"),
                "event_at": published_at,
            }
        ),
        impact_scope=str(item.get("impact_scope") or "symbol"),
        evidence_url=str(item.get("evidence_url") or ""),
        payload=payload,
        **freshness.model_dump(),
    )


def _build_research_summary_payload(
    *,
    news: list[dict[str, Any]],
    announcements: list[dict[str, Any]],
    policy: list[dict[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    all_items = news + announcements + policy
    sorted_items = sorted(
        all_items,
        key=lambda item: str(item.get("recorded_at") or item.get("event_at") or ""),
        reverse=True,
    )
    return {
        "symbols": sorted({str(item.get("symbol") or "") for item in all_items if item.get("symbol")}),
        "news_count": len(news),
        "announcement_count": len(announcements),
        "policy_count": len(policy),
        "event_titles": [str(item.get("title") or "") for item in sorted_items[:10]],
        "latest_news": sorted(news, key=lambda item: str(item.get("recorded_at") or item.get("event_at") or ""), reverse=True)[:5],
        "latest_announcements": sorted(
            announcements,
            key=lambda item: str(item.get("recorded_at") or item.get("event_at") or ""),
            reverse=True,
        )[:5],
        "latest_policy": sorted(policy, key=lambda item: str(item.get("recorded_at") or item.get("event_at") or ""), reverse=True)[:5],
        "updated_at": updated_at,
    }


def _build_structured_event_context(
    *,
    trade_date: str,
    events: list[dict[str, Any]],
    generated_at: str,
    summary_lines: list[str] | None = None,
) -> dict[str, Any]:
    counts_by_scope = {scope: 0 for scope in ("market", "sector", "symbol", "macro", "unknown")}
    counts_by_category: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    latest_titles: list[str] = []
    latest_event_at: str | None = None
    highlights: list[dict[str, Any]] = []
    by_scope: dict[str, list[dict[str, Any]]] = {scope: [] for scope in ("market", "sector", "symbol", "macro", "unknown")}
    blocked_symbols: set[str] = set()
    for item in events:
        scope = str(item.get("impact_scope") or "unknown").strip().lower()
        if scope not in counts_by_scope:
            scope = "unknown"
        category = str(item.get("category") or "announcements")
        counts_by_scope[scope] = counts_by_scope.get(scope, 0) + 1
        counts_by_category[category] = counts_by_category.get(category, 0) + 1
        symbol = str(item.get("symbol") or "").strip()
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        title = str(item.get("title") or "")
        if title and title not in latest_titles and len(latest_titles) < 10:
            latest_titles.append(title)
        event_at = str(item.get("published_at") or "")
        if event_at and (latest_event_at is None or event_at > latest_event_at):
            latest_event_at = event_at
        impact = str(item.get("impact") or "").strip().lower()
        severity = str(item.get("severity") or "").strip().lower()
        if symbol and (impact == "block" or severity == "block"):
            blocked_symbols.add(symbol)
        digest = {
            "event_id": _structured_event_dedupe_key(
                {
                    "category": item.get("category"),
                    "symbol": symbol,
                    "source": item.get("source"),
                    "title": title,
                    "event_at": event_at,
                }
            ),
            "symbol": symbol,
            "title": title,
            "category": category,
            "event_type": item.get("event_type"),
            "impact": impact or "neutral",
            "severity": item.get("severity"),
            "sentiment": (
                "negative"
                if impact in {"negative", "block"}
                else "positive"
                if impact == "positive"
                else "neutral"
            ),
            "impact_scope": scope,
            "event_at": event_at,
            "tags": list(item.get("tags") or []),
            "evidence_url": item.get("evidence_url", ""),
            "source": item.get("source", ""),
        }
        if len(highlights) < 12:
            highlights.append(digest)
        if len(by_scope.setdefault(scope, [])) < 8:
            by_scope[scope].append(digest)
    freshness = build_freshness_meta(
        source_at=latest_event_at or generated_at,
        fetched_at=generated_at,
        generated_at=generated_at,
        fresh_seconds=300,
        warm_seconds=3600,
    )
    return {
        **freshness.model_dump(),
        "trade_date": trade_date,
        "generated_at": generated_at,
        "symbol_event_counts": symbol_counts,
        "counts_by_scope": counts_by_scope,
        "counts_by_category": counts_by_category,
        "latest_titles": latest_titles,
        "total_event_count": len(events),
        "highlights": highlights,
        "by_scope": by_scope,
        "blocked_symbols": sorted(blocked_symbols),
        "summary_lines": list(summary_lines or []),
    }


def _build_blocked_symbols_from_events(event_context: dict[str, Any] | None) -> set[str]:
    payload = dict(event_context or {})
    blocked = set(str(item).strip() for item in list(payload.get("blocked_symbols") or []) if str(item).strip())
    by_scope = payload.get("by_scope") or {}
    related = list(payload.get("highlights") or [])
    related.extend(list(by_scope.get("symbol") or []))
    related.extend(list(by_scope.get("market") or []))
    for item in related:
        event = _as_payload(item)
        symbol = str(event.get("symbol") or "").strip()
        impact = str(event.get("impact") or "").strip().lower()
        severity = str(event.get("severity") or "").strip().lower()
        tags = {str(tag).strip().lower() for tag in list(event.get("tags") or [])}
        if symbol and (impact == "block" or severity == "block" or "suspension" in tags):
            blocked.add(symbol)
    return blocked


def _persist_event_fetch_result(
    *,
    research_state_store: StateStore,
    archive_store: DataArchiveStore,
    result: EventFetchResult | dict[str, Any],
) -> dict[str, Any]:
    payload = _as_payload(result)
    generated_at = str(payload.get("generated_at") or datetime.now().isoformat())
    trade_date = str(payload.get("trade_date") or generated_at[:10])
    events = [_as_payload(item) for item in list(payload.get("events") or [])]
    grouped_records: dict[str, list[EventRecord]] = {"news": [], "announcements": [], "policy": []}
    for item in events:
        record = _structured_event_to_record(item, recorded_at=generated_at)
        grouped_records[record.category].append(record)
    updated_state: dict[str, list[dict[str, Any]]] = {}
    for category in ("news", "announcements", "policy"):
        existing = list(research_state_store.get(category, []) or [])
        merged = {
            _structured_event_dedupe_key(item): item
            for item in existing
            if isinstance(item, dict)
        }
        for record in grouped_records[category]:
            dumped = record.model_dump()
            merged[str(record.dedupe_key or record.event_id)] = dumped
        items = sorted(
            merged.values(),
            key=lambda item: str(item.get("recorded_at") or item.get("event_at") or ""),
        )[-200:]
        research_state_store.set(category, items)
        updated_state[category] = items
        archive_store.persist_event_records(category, grouped_records[category])
    summary = _build_research_summary_payload(
        news=updated_state["news"],
        announcements=updated_state["announcements"],
        policy=updated_state["policy"],
        updated_at=generated_at,
    )
    research_state_store.set("summary", summary)
    research_state_store.set("latest_event_fetch_result", payload)
    event_context = _build_structured_event_context(
        trade_date=trade_date,
        events=events,
        generated_at=generated_at,
        summary_lines=list(payload.get("summary_lines") or []),
    )
    archive_store.persist_event_context(trade_date, event_context)
    blocked_symbols = sorted(_build_blocked_symbols_from_events(event_context))
    return {
        "trade_date": trade_date,
        "generated_at": generated_at,
        "event_count": len(events),
        "news_count": len(grouped_records["news"]),
        "announcement_count": len(grouped_records["announcements"]),
        "policy_count": len(grouped_records["policy"]),
        "blocked_symbols": blocked_symbols,
        "summary_lines": list(payload.get("summary_lines") or []),
    }


def _build_intraday_rank_candidate_payloads(
    runtime_context: dict[str, Any],
    candidate_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    top_picks = [_as_payload(item) for item in list(runtime_context.get("top_picks") or [])]
    playbook_map = {
        str(item.get("symbol") or ""): _as_payload(item)
        for item in list(runtime_context.get("playbook_contexts") or [])
        if str(item.get("symbol") or "").strip()
    }
    if candidate_symbols:
        symbol_order = list(dict.fromkeys(str(symbol) for symbol in candidate_symbols if str(symbol).strip()))
    elif top_picks:
        symbol_order = [str(item.get("symbol") or "") for item in top_picks if str(item.get("symbol") or "").strip()]
    else:
        symbol_order = list(playbook_map.keys())
    top_pick_map = {str(item.get("symbol") or ""): item for item in top_picks if str(item.get("symbol") or "").strip()}
    candidates: list[dict[str, Any]] = []
    for symbol in symbol_order:
        top_pick = dict(top_pick_map.get(symbol) or {})
        playbook_context = dict(playbook_map.get(symbol) or top_pick.get("playbook_context") or {})
        candidate = {
            "symbol": symbol,
            "name": str(top_pick.get("name") or ""),
            "rank": int(top_pick.get("rank", 0) or 0),
            "selection_score": float(top_pick.get("selection_score", 0.0) or 0.0),
            "playbook_context": playbook_context,
            "assigned_playbook": str(
                top_pick.get("assigned_playbook") or playbook_context.get("playbook") or ""
            ),
            "resolved_sector": str(
                top_pick.get("resolved_sector") or playbook_context.get("sector") or ""
            ),
            "playbook_match_score": _as_payload(
                top_pick.get("playbook_match_score") or playbook_context.get("playbook_match_score")
            ),
        }
        candidates.append(candidate)
    return candidates


def _call_intraday_ranker(
    *,
    settings: AppSettings,
    runtime_context: dict[str, Any],
    event_context: dict[str, Any],
    candidate_symbols: list[str] | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_factory or datetime.now)()
    generated_at = now.isoformat()
    market_profile = _as_payload(runtime_context.get("market_profile"))
    sector_profiles = [_as_payload(item) for item in list(runtime_context.get("sector_profiles") or [])]
    playbook_contexts = [_as_payload(item) for item in list(runtime_context.get("playbook_contexts") or [])]
    candidates = _build_intraday_rank_candidate_payloads(runtime_context, candidate_symbols)
    ranker = _load_intraday_ranker()
    actions: list[dict[str, Any]] = []
    ranker_summary_lines: list[str] = []
    freeze_all = False
    if ranker is not None:
        for method_name in ("rank_candidates", "rank_intraday", "rank", "evaluate", "build"):
            method = getattr(ranker, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(
                    candidates=candidates,
                    sector_profiles=sector_profiles,
                    market_profile=market_profile,
                    event_context=event_context,
                    playbook_contexts=playbook_contexts,
                )
            except TypeError:
                continue
            except Exception:
                logger.exception("IntradayRanker 执行失败，将回退 scheduler 本地规则。")
                break
            payload = _as_payload(result)
            actions = _coerce_intraday_rank_actions(payload or result, generated_at)
            ranker_summary_lines = [str(item) for item in list(payload.get("summary_lines") or []) if str(item).strip()]
            freeze_all = bool(payload.get("freeze_all_active", False))
            if actions or ranker_summary_lines or freeze_all:
                break
    if not actions:
        actions = _build_fallback_intraday_rank_actions(
            candidates=candidates,
            sector_profiles=sector_profiles,
            market_profile=market_profile,
            event_context=event_context,
            generated_at=generated_at,
        )
    freeze_all = freeze_all or any(item["action"] == "FREEZE_ALL" for item in actions)
    freeze_count = sum(1 for item in actions if item["action"] in {"FREEZE", "FREEZE_ALL"})
    upgrade_count = sum(1 for item in actions if item["action"] == "UPGRADE")
    downgrade_count = sum(1 for item in actions if item["action"] == "DOWNGRADE")
    market_regime = str(market_profile.get("regime") or "unknown")
    summary_lines = ranker_summary_lines or [
        (
            f"盘中重排完成：candidates={len(candidates)} actions={len(actions)} "
            f"freeze={freeze_count} upgrade={upgrade_count} downgrade={downgrade_count} regime={market_regime}。"
        )
    ]
    if not ranker_summary_lines:
        if freeze_all:
            summary_lines.append("市场处于 chaos，当前候选统一冻结。")
        elif freeze_count:
            summary_lines.append("存在负面事件或板块退潮，候选已触发冻结/降级提示。")
        elif upgrade_count:
            summary_lines.append("板块进入 ferment 且涨停扩散增强，候选已触发升级提示。")
    return {
        "status": "ok",
        "source": "scheduler_intraday",
        "trade_date": str(runtime_context.get("trade_date") or now.date().isoformat()),
        "generated_at": generated_at,
        "market_regime": market_regime,
        "candidate_count": len(candidates),
        "action_count": len(actions),
        "freeze_count": freeze_count,
        "upgrade_count": upgrade_count,
        "downgrade_count": downgrade_count,
        "freeze_all": freeze_all,
        "actions": actions,
        "summary_lines": summary_lines,
    }


def _build_fallback_intraday_rank_actions(
    *,
    candidates: list[dict[str, Any]],
    sector_profiles: list[dict[str, Any]],
    market_profile: dict[str, Any],
    event_context: dict[str, Any],
    generated_at: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    sector_map = {
        str(item.get("sector_name") or ""): item
        for item in sector_profiles
        if str(item.get("sector_name") or "").strip()
    }
    market_regime = str(market_profile.get("regime") or "").strip().lower()
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").strip()
        if not symbol:
            continue
        sector_name = str(candidate.get("resolved_sector") or "").strip()
        sector_profile = sector_map.get(sector_name, {})
        life_cycle = str(sector_profile.get("life_cycle") or "").strip().lower()
        zt_count_delta = int(sector_profile.get("zt_count_delta", 0) or 0)
        if market_regime == "chaos":
            action = "FREEZE_ALL"
            trigger = "market_chaos"
            reason = "市场 regime=chaos，当前候选统一冻结，等待风险收敛。"
            priority_delta = -3
        elif _has_negative_event(symbol=symbol, event_context=event_context):
            action = "FREEZE"
            trigger = "negative_event"
            reason = "event_context 命中负面事件，当前候选先冻结。"
            priority_delta = -2
        elif life_cycle == "retreat":
            action = "DOWNGRADE"
            trigger = "sector_retreat"
            reason = "板块生命周期进入 retreat，当前候选优先级下调。"
            priority_delta = -1
        elif life_cycle == "ferment" and zt_count_delta >= 2:
            action = "UPGRADE"
            trigger = "sector_ferment_expansion"
            reason = "板块处于 ferment 且 zt_count_delta 明显上升，当前候选优先级上调。"
            priority_delta = 1
        else:
            continue
        actions.append(
            {
                "symbol": symbol,
                "action": action,
                "reason": reason,
                "trigger": trigger,
                "priority_delta": priority_delta,
                "generated_at": generated_at,
            }
        )
    return actions


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


def _build_position_watch_action_suggestions(items: list[dict[str, Any]]) -> list[str]:
    suggestions: list[str] = []
    for item in items:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        name = str(item.get("name") or symbol).strip()
        status = str(item.get("status") or "").strip()
        signal_type = str(item.get("signal_type") or "").strip()
        if signal_type in {"sell", "sell_and_day_trading"}:
            reason = str(item.get("exit_reason") or item.get("signal_summary") or "").strip() or "卖出信号"
            if status == "submitted":
                suggestions.append(f"{symbol} {name} 已自动提交卖出，原因 {reason}。")
            elif status == "queued_for_gateway":
                suggestions.append(f"{symbol} {name} 已派发卖出意图，等待 Windows 网关执行，原因 {reason}。")
            elif status in {"preview", "skipped_pending_sell", "skipped_min_lot"}:
                preview_reason = str(item.get("preview_reason") or status).strip()
                suggestions.append(f"{symbol} {name} 命中卖出信号，但当前仅预演，原因 {preview_reason}。")
            elif status == "error":
                suggestions.append(f"{symbol} {name} 卖出执行失败，需人工复核，原因 {item.get('error') or reason}。")
            else:
                suggestions.append(f"{symbol} {name} 命中卖出信号，建议立即复核 {reason}。")
        elif str(item.get("t_signal_action") or "").strip().upper() in {"HIGH_SELL", "LOW_BUY"}:
            action = str(item.get("t_signal_action") or "").strip().upper()
            reason = str(item.get("t_signal_reason") or item.get("signal_summary") or "").strip()
            suggestions.append(f"{symbol} {name} 出现做T信号 {action}，建议关注 {reason}。")
    return suggestions[:8]


def _build_intraday_signal_snapshot(
    *,
    mode: str,
    symbol: str,
    signal_reason: str,
    signal_type: str,
    current_price: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "mode": str(mode or "intraday"),
        "symbol": symbol,
        "signal_reason": str(signal_reason or "").strip(),
        "signal_type": str(signal_type or "").strip(),
        "current_price": round(float(current_price or 0.0), 6),
        "triggered_at": datetime.now().isoformat(),
        "details": dict(payload or {}),
    }


def _fast_track_exit_review(
    discussion_cycle_service,
    *,
    trade_date: str,
    symbol: str,
    signal_reason: str,
    signal_payload: dict[str, Any],
) -> dict[str, Any]:
    if discussion_cycle_service is None:
        return {"available": False, "approved": True, "reason": "discussion_cycle_service_unavailable"}
    try:
        return discussion_cycle_service.fast_track_exit(
            trade_date=trade_date,
            symbol=symbol,
            signal_reason=signal_reason,
            signal_payload=signal_payload,
        )
    except Exception as exc:
        logger.exception("fast track exit review failed: trade_date=%s symbol=%s", trade_date, symbol)
        return {
            "available": False,
            "approved": True,
            "reason": "fast_track_exit_failed",
            "error": str(exc),
        }


def _sync_market_regime_guard(
    runtime_state_store: StateStore | None,
    *,
    trade_date: str,
    detected_regime: dict[str, Any] | None,
) -> dict[str, Any]:
    if runtime_state_store is None or not isinstance(detected_regime, dict) or not detected_regime:
        return {"available": False, "active": False, "status": "unavailable"}

    current_regime = dict(detected_regime)
    previous_regime = dict(runtime_state_store.get("latest_market_regime", {}) or {})
    existing_guard = dict(runtime_state_store.get("latest_regime_transition_guard", {}) or {})
    if "transition" not in current_regime:
        current_regime["transition"] = detect_regime_transition(
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
    runtime_state_store.set("latest_market_regime", current_regime)

    current_label = str(current_regime.get("regime_label") or "").strip()
    current_runtime_regime = str(current_regime.get("runtime_regime") or "").strip()
    transition = dict(current_regime.get("transition") or {})
    required_days = max(
        int(existing_guard.get("required_confirmation_days", REGIME_CONFIRMATION_DAYS) or REGIME_CONFIRMATION_DAYS),
        1,
    )
    today = str(trade_date or datetime.now().date().isoformat())

    guard: dict[str, Any]
    if bool(transition.get("available")):
        transition_key = (
            f"{str(transition.get('from_regime') or '').strip()}->"
            f"{str(transition.get('to_regime') or '').strip()}@"
            f"{str(transition.get('detected_at') or '').strip()}"
        )
        confirmed_trade_dates = list(existing_guard.get("confirmed_trade_dates") or [])
        if str(existing_guard.get("transition_key") or "") != transition_key:
            confirmed_trade_dates = [today]
        elif today not in confirmed_trade_dates:
            confirmed_trade_dates.append(today)
        confirmed_trade_dates = list(dict.fromkeys(confirmed_trade_dates))[-required_days:]
        confirmed_days = len(confirmed_trade_dates)
        active = confirmed_days < required_days
        guard = {
            "available": True,
            "active": active,
            "status": ("confirming" if active else "confirmed"),
            "transition_key": transition_key,
            "from_regime": str(transition.get("from_regime") or "").strip(),
            "to_regime": str(transition.get("to_regime") or "").strip(),
            "target_runtime_regime": current_runtime_regime,
            "detected_at": str(transition.get("detected_at") or datetime.now().isoformat()),
            "required_confirmation_days": required_days,
            "confirmed_days": confirmed_days,
            "remaining_confirmation_days": max(required_days - confirmed_days, 0),
            "confirmed_trade_dates": confirmed_trade_dates,
            "tightened_stop_atr_mult": 1.0,
            "last_evaluated_at": datetime.now().isoformat(),
            "summary_lines": [
                (
                    f"regime 已从 {str(transition.get('from_regime') or 'unknown')} 切到 "
                    f"{str(transition.get('to_regime') or 'unknown')}，确认进度 {confirmed_days}/{required_days}。"
                ),
                "确认期内暂停旧 regime 强相关新买入，并对不匹配持仓收紧退出参数。",
            ],
        }
    elif existing_guard and str(existing_guard.get("to_regime") or "").strip() == current_label:
        confirmed_trade_dates = list(existing_guard.get("confirmed_trade_dates") or [])
        if today not in confirmed_trade_dates:
            confirmed_trade_dates.append(today)
        confirmed_trade_dates = list(dict.fromkeys(confirmed_trade_dates))[-required_days:]
        confirmed_days = len(confirmed_trade_dates)
        active = confirmed_days < required_days
        guard = {
            **existing_guard,
            "available": True,
            "active": active,
            "status": ("confirming" if active else "confirmed"),
            "target_runtime_regime": current_runtime_regime or str(existing_guard.get("target_runtime_regime") or ""),
            "confirmed_days": confirmed_days,
            "remaining_confirmation_days": max(required_days - confirmed_days, 0),
            "confirmed_trade_dates": confirmed_trade_dates,
            "last_evaluated_at": datetime.now().isoformat(),
            "summary_lines": [
                (
                    f"regime 确认继续推进: {str(existing_guard.get('to_regime') or current_label or 'unknown')} "
                    f"{confirmed_days}/{required_days}。"
                ),
            ],
        }
    else:
        guard = {
            "available": False,
            "active": False,
            "status": "inactive",
            "current_regime": current_label,
            "target_runtime_regime": current_runtime_regime,
            "required_confirmation_days": required_days,
            "confirmed_days": 0,
            "remaining_confirmation_days": 0,
            "confirmed_trade_dates": [],
            "last_evaluated_at": datetime.now().isoformat(),
            "summary_lines": ["当前未检测到新的 regime 转换守门状态。"],
        }

    runtime_state_store.set("latest_regime_transition_guard", guard)
    runtime_state_store.set(
        "regime_transition_confirmation_state",
        {
            "trade_date": today,
            "current_regime": current_label,
            "target_runtime_regime": current_runtime_regime,
            "guard_status": guard.get("status"),
            "guard_active": bool(guard.get("active")),
            "confirmed_days": int(guard.get("confirmed_days", 0) or 0),
            "required_confirmation_days": int(guard.get("required_confirmation_days", required_days) or required_days),
            "remaining_confirmation_days": int(guard.get("remaining_confirmation_days", 0) or 0),
            "updated_at": datetime.now().isoformat(),
        },
    )
    return guard


def _resolve_regime_sell_adjustment(
    regime_guard: dict[str, Any] | None,
    *,
    position_regime: str | None,
    playbook: str | None,
) -> dict[str, Any]:
    guard = dict(regime_guard or {})
    if not bool(guard.get("active")):
        return {
            "mismatch": False,
            "effective_regime": str(position_regime or "").strip(),
            "param_overrides": {},
            "review_tags": [],
            "summary_lines": [],
        }

    target_runtime_regime = str(guard.get("target_runtime_regime") or "").strip().lower()
    position_regime_value = str(position_regime or "").strip().lower()
    playbook_value = str(playbook or "").strip().lower()
    offensive_guard = target_runtime_regime in {"defensive", "chaos"} and playbook_value in OFFENSIVE_PLAYBOOKS
    regime_guarded = bool(position_regime_value and target_runtime_regime and position_regime_value != target_runtime_regime)
    mismatch = offensive_guard or regime_guarded
    if not mismatch:
        return {
            "mismatch": False,
            "effective_regime": str(position_regime or "").strip(),
            "param_overrides": {},
            "review_tags": [],
            "summary_lines": [],
        }

    effective_regime = "chaos" if target_runtime_regime == "chaos" else "defensive"
    reason = (
        "offensive_playbook_blocked"
        if offensive_guard
        else f"position_regime={position_regime_value or 'unknown'} target_runtime={target_runtime_regime or 'unknown'}"
    )
    return {
        "mismatch": True,
        "effective_regime": effective_regime,
        "param_overrides": {"atr_stop_mult": float(guard.get("tightened_stop_atr_mult", 1.0) or 1.0)},
        "review_tags": ["regime_mismatch_hold", "tightened_stop"],
        "summary_lines": [f"regime 守门触发，持仓按 {effective_regime} 退出参数收紧: {reason}。"],
    }


def _record_scheduler_sell_submission(
    quality_tracker: ExecutionQualityTracker | None,
    *,
    request: PlaceOrderRequest,
    quote: QuoteSnapshot | None,
    submit_time: str,
    status: str,
    order_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if quality_tracker is None:
        return
    quality_tracker.record_submission(
        intent_id=request.request_id,
        order_id=order_id,
        trace_id=request.trace_id,
        symbol=request.symbol,
        side=request.side,
        signal_price=float(request.signal_price or request.price or 0.0),
        signal_time=str(request.signal_time or submit_time),
        submit_price=float(request.price or 0.0),
        submit_time=submit_time,
        bid_price=(float(quote.bid_price or 0.0) if quote is not None else None),
        ask_price=(float(quote.ask_price or 0.0) if quote is not None else None),
        metadata={**dict(metadata or {}), "submission_status": status},
    )


def _append_scheduler_trade_trace(
    trace_service: TradeTraceService | None,
    *,
    trace_id: str | None,
    stage: str,
    trade_date: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if trace_service is None or not str(trace_id or "").strip():
        return
    trace_service.append_event(
        trace_id=str(trace_id),
        stage=stage,
        trade_date=trade_date,
        payload=dict(payload or {}),
    )


def _compute_bar_vwap(bars: list[Any], fallback_price: float) -> float:
    total_amount = sum(max(float(getattr(item, "amount", 0.0) or 0.0), 0.0) for item in bars)
    total_volume = sum(max(float(getattr(item, "volume", 0.0) or 0.0), 0.0) for item in bars)
    if total_amount > 0 and total_volume > 0:
        return round(total_amount / total_volume, 6)
    closes = [float(getattr(item, "close", 0.0) or 0.0) for item in bars if float(getattr(item, "close", 0.0) or 0.0) > 0]
    if closes:
        return round(sum(closes) / len(closes), 6)
    return round(float(fallback_price or 0.0), 6)


def _compute_open_price(
    micro_bars: list[Any],
    intraday_bars: list[Any],
    fallback_price: float,
) -> float:
    for series in (micro_bars, intraday_bars):
        if series:
            try:
                return float(getattr(series[0], "open", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
    return float(fallback_price or 0.0)


def _build_position_watch_exit_snapshot(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    items = list(payload.get("items") or [])
    signal_items = [
        item
        for item in items
        if str(item.get("exit_reason") or "").strip()
        or str(item.get("t_signal_action") or "").strip().upper() not in {"", "HOLD"}
    ]
    reason_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    normalized_items: list[dict[str, Any]] = []
    for item in signal_items:
        reason = str(item.get("exit_reason") or "").strip()
        if not reason:
            reason = str(item.get("t_signal_action") or "").strip().lower()
        severity = str(item.get("severity") or "info").strip().lower() or "info"
        symbol = str(item.get("symbol") or "").strip()
        tags = [str(tag).strip() for tag in list(item.get("review_tags") or []) if str(tag).strip()]
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        normalized_items.append(
            {
                "symbol": symbol,
                "name": item.get("name") or symbol,
                "reason": reason,
                "severity": severity,
                "status": item.get("status"),
                "signal_type": item.get("signal_type") or ("sell" if item.get("planned_quantity") else "day_trading"),
                "message": item.get("signal_summary") or item.get("t_signal_reason") or reason,
                "tags": tags,
                "current_price": item.get("current_price"),
                "planned_quantity": item.get("planned_quantity") or item.get("t_signal_quantity") or 0,
                "review_tags": tags,
            }
        )
    return {
        "version": "v1",
        "checked_at": now.timestamp(),
        "signal_count": len(normalized_items),
        "watched_symbols": [str(item.get("symbol") or "") for item in items if str(item.get("symbol") or "").strip()],
        "by_symbol": [
            {"key": key, "count": count}
            for key, count in sorted(symbol_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_reason": [
            {"key": key, "count": count}
            for key, count in sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_severity": [
            {"key": key, "count": count}
            for key, count in sorted(severity_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_tag": [
            {"key": key, "count": count}
            for key, count in sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "summary_lines": list(payload.get("summary_lines") or []),
        "items": normalized_items,
    }


def _load_fast_position_watch_runtime(state_store: StateStore | None) -> dict[str, Any]:
    if not state_store:
        return {"symbols": {}}
    payload = dict(state_store.get("fast_position_watch_runtime", {}) or {})
    symbols = payload.get("symbols")
    payload["symbols"] = dict(symbols or {})
    return payload


def _persist_fast_position_watch_runtime(state_store: StateStore | None, payload: dict[str, Any]) -> None:
    if not state_store:
        return
    state_store.set("fast_position_watch_runtime", payload)


def _load_fast_pending_sell_tracker(
    runtime: dict[str, Any],
    now: datetime,
    *,
    ttl_seconds: int = 600,
) -> tuple[dict[str, Any], set[str]]:
    tracker = dict(runtime.get("pending_sell_tracker") or {})
    normalized_by_symbol: dict[str, dict[str, Any]] = {}
    open_symbols: set[str] = set()
    for symbol, item in dict(tracker.get("by_symbol") or {}).items():
        normalized_symbol = str(symbol or "").strip()
        if not normalized_symbol:
            continue
        payload = dict(item or {})
        marked_at = _parse_iso_datetime(str(payload.get("marked_at") or ""))
        if marked_at is not None:
            marked_at, normalized_now = _normalize_datetime_pair(marked_at, now)
            age_seconds = max((normalized_now - marked_at).total_seconds(), 0.0)
            if age_seconds > ttl_seconds:
                continue
        payload["marked_at"] = str(payload.get("marked_at") or now.isoformat())
        normalized_by_symbol[normalized_symbol] = payload
        open_symbols.add(normalized_symbol)
    tracker["by_symbol"] = normalized_by_symbol
    return tracker, open_symbols


def _refresh_fast_pending_sell_tracker_from_orders(
    tracker: dict[str, Any],
    *,
    execution_adapter: ExecutionAdapter,
    account_id: str,
    now: datetime,
    refresh_interval_seconds: int = 20,
) -> tuple[dict[str, Any], set[str]]:
    existing_symbols = {
        str(symbol).strip()
        for symbol in dict(tracker.get("by_symbol") or {}).keys()
        if str(symbol).strip()
    }
    last_refreshed_at = _parse_iso_datetime(str(tracker.get("orders_refreshed_at") or ""))
    if last_refreshed_at is not None:
        last_refreshed_at, normalized_now = _normalize_datetime_pair(last_refreshed_at, now)
        if max((normalized_now - last_refreshed_at).total_seconds(), 0.0) < refresh_interval_seconds:
            return tracker, existing_symbols
    try:
        orders = execution_adapter.get_orders(account_id)
    except Exception:
        logger.warning("快路径刷新挂单缓存失败，继续使用本地 pending_sell_tracker。", exc_info=True)
        tracker["orders_refreshed_at"] = now.isoformat()
        return tracker, existing_symbols
    pending_statuses = {"PENDING", "ACCEPTED", "PARTIAL_FILLED", "CANCEL_REQUESTED", "UNKNOWN"}
    rebuilt_by_symbol: dict[str, dict[str, Any]] = {}
    for order in orders:
        side = str(order.side or "").upper()
        status = str(order.status or "").upper()
        symbol = str(order.symbol or "").strip()
        if side != "SELL" or status not in pending_statuses or not symbol:
            continue
        rebuilt_by_symbol[symbol] = {
            "status": status.lower(),
            "source": "execution_adapter_orders",
            "marked_at": now.isoformat(),
            "order_id": str(order.order_id or ""),
        }
    tracker["by_symbol"] = rebuilt_by_symbol
    tracker["orders_refreshed_at"] = now.isoformat()
    return tracker, set(rebuilt_by_symbol.keys())


def _mark_fast_pending_sell_symbol(
    tracker: dict[str, Any],
    *,
    symbol: str,
    now: datetime,
    status: str,
    source: str,
    order_id: str | None = None,
    intent_id: str | None = None,
) -> None:
    normalized_symbol = str(symbol or "").strip()
    if not normalized_symbol:
        return
    by_symbol = dict(tracker.get("by_symbol") or {})
    payload = {
        "status": str(status or "pending").strip().lower() or "pending",
        "source": str(source or "fast_position_watch"),
        "marked_at": now.isoformat(),
    }
    if order_id:
        payload["order_id"] = str(order_id)
    if intent_id:
        payload["intent_id"] = str(intent_id)
    by_symbol[normalized_symbol] = payload
    tracker["by_symbol"] = by_symbol


def _build_fast_position_watch_signal(
    *,
    symbol: str,
    entry_price: float,
    current_price: float,
    peak_price: float,
    available: int,
    last_change_pct: float,
    spread_pct: float,
    atr_pct: float,
) -> dict[str, Any] | None:
    if entry_price <= 0 or current_price <= 0 or available <= 0:
        return None
    pnl_pct = (current_price - entry_price) / max(entry_price, 1e-9)
    pullback_pct = (peak_price - current_price) / max(peak_price, 1e-9) if peak_price > 0 else 0.0
    adaptive_stop_loss_pct = max(-2.0 * max(float(atr_pct or 0.0), 0.0), -0.05)
    max_unrealized_gain = (peak_price - entry_price) / max(entry_price, 1e-9) if peak_price > 0 else 0.0
    peak_drawdown_from_entry = max(peak_price - current_price, 0.0) / max(entry_price, 1e-9)
    if pnl_pct <= adaptive_stop_loss_pct:
        return {
            "signal_type": "sell",
            "exit_reason": "fast_stop_loss",
            "sell_ratio": 1.0,
            "severity": "critical",
            "adaptive_stop_loss_pct": round(adaptive_stop_loss_pct, 6),
            "signal_summary": f"快路径止损触发: 浮亏 {pnl_pct:.2%}，自适应阈值 {adaptive_stop_loss_pct:.2%}",
        }
    if max_unrealized_gain >= 0.02 and peak_drawdown_from_entry >= 0.5 * max_unrealized_gain:
        return {
            "signal_type": "sell",
            "exit_reason": "fast_profit_protect",
            "sell_ratio": 0.5,
            "severity": "warning",
            "peak_drawdown_from_entry": round(peak_drawdown_from_entry, 6),
            "signal_summary": (
                f"快路径保护利润: 峰值回撤 {peak_drawdown_from_entry:.2%}，"
                f"最大浮盈 {max_unrealized_gain:.2%}"
            ),
        }
    if pnl_pct >= 0.018 and last_change_pct < -0.006 and spread_pct <= 0.003:
        return {
            "signal_type": "day_trading",
            "t_signal_action": "HIGH_SELL",
            "t_signal_quantity": max(min((available // 2 // 100) * 100, available), 0),
            "severity": "warning",
            "signal_summary": f"快路径高抛: 近一跳回落 {last_change_pct:.2%}",
            "exit_reason": "fast_intraday_high_sell",
        }
    return None


def _extract_fast_opportunity_symbols(
    runtime_state_store: StateStore | None,
    position_watch_state_store: StateStore | None,
    *,
    max_symbols: int = 24,
) -> list[str]:
    symbols: list[str] = []
    runtime_context = dict(runtime_state_store.get("latest_runtime_context", {}) or {}) if runtime_state_store else {}
    symbols.extend(str(item).strip() for item in list(runtime_context.get("selected_symbols") or []) if str(item).strip())
    for item in list(runtime_context.get("top_picks") or [])[: max_symbols * 2]:
        symbol = str((item or {}).get("symbol") or "").strip()
        if symbol:
            symbols.append(symbol)
    if position_watch_state_store:
        latest_position_watch = dict(position_watch_state_store.get("latest_position_watch_scan", {}) or {})
        for item in list(latest_position_watch.get("items") or [])[:10]:
            symbol = str((item or {}).get("symbol") or "").strip()
            if symbol:
                symbols.append(symbol)
        latest_scan = dict(position_watch_state_store.get("latest_fast_opportunity_scan", {}) or {})
        for item in list(latest_scan.get("items") or [])[:10]:
            symbol = str((item or {}).get("symbol") or "").strip()
            if symbol:
                symbols.append(symbol)
    return list(dict.fromkeys(symbols))[:max_symbols]


def _persist_fast_opportunity_scan(state_store: StateStore | None, payload: dict[str, Any]) -> None:
    if not state_store:
        return
    history = list(state_store.get("fast_opportunity_history", []) or [])
    history.append(
        {
            "generated_at": payload.get("generated_at"),
            "trade_date": payload.get("trade_date"),
            "count": payload.get("count", 0),
            "pre_limit_up_count": payload.get("pre_limit_up_count", 0),
            "abnormal_drop_count": payload.get("abnormal_drop_count", 0),
        }
    )
    state_store.set("latest_fast_opportunity_scan", payload)
    state_store.set("fast_opportunity_history", history[-240:])


def run_fast_opportunity_scan(
    *,
    settings: AppSettings,
    market: MarketDataAdapter,
    runtime_state_store: StateStore | None,
    position_watch_state_store: StateStore | None,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_factory or datetime.now)()
    symbols = _extract_fast_opportunity_symbols(runtime_state_store, position_watch_state_store)
    if not symbols:
        payload = {
            "available": False,
            "generated_at": now.isoformat(),
            "trade_date": now.date().isoformat(),
            "count": 0,
            "pre_limit_up_count": 0,
            "abnormal_drop_count": 0,
            "summary_lines": ["快路径机会扫描完成: 当前无可扫描标的。"],
            "items": [],
        }
        _persist_fast_opportunity_scan(position_watch_state_store, payload)
        return payload
    runtime_context = dict(runtime_state_store.get("latest_runtime_context", {}) or {}) if runtime_state_store else {}
    market_profile = dict(runtime_context.get("market_profile") or {})
    hot_sectors = {
        str(item).strip()
        for item in list(market_profile.get("hot_sectors") or [])
        if str(item).strip()
    }
    playbook_map = {
        str(item.get("symbol") or "").strip(): dict(item)
        for item in list(runtime_context.get("playbook_contexts") or [])
        if str(item.get("symbol") or "").strip()
    }
    snapshots = market.get_snapshots(symbols)
    grouped_bars: dict[str, list[Any]] = {}
    try:
        for bar in market.get_bars(symbols, "5m", count=12):
            grouped_bars.setdefault(bar.symbol, []).append(bar)
    except Exception:
        grouped_bars = {}
    sector_stats: dict[str, dict[str, Any]] = {}
    symbol_sector_tags: dict[str, list[str]] = {}
    symbol_primary_sector: dict[str, str] = {}
    for snap in snapshots:
        playbook_context = playbook_map.get(snap.symbol, {})
        sector_relative = dict((playbook_context.get("symbol_context") or {}).get("sector_relative") or {})
        sector_tags = [
            str(item).strip()
            for item in list(sector_relative.get("sector_tags") or [])
            if str(item).strip()
        ]
        resolved_sector = str(playbook_context.get("sector") or sector_tags[0] if sector_tags else "").strip()
        symbol_sector_tags[snap.symbol] = sector_tags
        symbol_primary_sector[snap.symbol] = resolved_sector
        if resolved_sector:
            change_pct = _snapshot_change_pct(snap)
            stats = dict(sector_stats.get(resolved_sector) or {"over_3pct": 0, "over_5pct": 0, "leader_limit_up": False})
            if change_pct >= 0.03:
                stats["over_3pct"] = int(stats.get("over_3pct", 0) or 0) + 1
            if change_pct >= 0.05:
                stats["over_5pct"] = int(stats.get("over_5pct", 0) or 0) + 1
            if change_pct >= _resolve_symbol_limit_pct(snap.symbol) - 0.005:
                stats["leader_limit_up"] = True
            sector_stats[resolved_sector] = stats
    items: list[dict[str, Any]] = []
    early_momentum_count = 0
    acceleration_count = 0
    pre_limit_up_count = 0
    abnormal_drop_count = 0
    for snap in snapshots:
        pre_close = float(snap.pre_close or 0.0)
        last_price = float(snap.last_price or 0.0)
        if pre_close <= 0 or last_price <= 0:
            continue
        limit_pct = _resolve_symbol_limit_pct(snap.symbol)
        change_pct = (last_price - pre_close) / max(pre_close, 1e-9)
        spread_pct = abs(float(snap.ask_price or last_price) - float(snap.bid_price or last_price)) / max(last_price, 1e-9)
        bars = list(grouped_bars.get(snap.symbol) or [])
        latest_bar_volume = float(getattr(bars[-1], "volume", 0.0) or 0.0) if bars else 0.0
        previous_volumes = [
            float(getattr(item, "volume", 0.0) or 0.0)
            for item in bars[-6:-1]
            if float(getattr(item, "volume", 0.0) or 0.0) > 0
        ]
        avg_bar_volume = (sum(previous_volumes) / len(previous_volumes)) if previous_volumes else 0.0
        volume_ratio_5m = (latest_bar_volume / avg_bar_volume) if avg_bar_volume > 0 else 0.0
        momentum_slope_5m = 0.0
        if len(bars) >= 3:
            base_close = float(getattr(bars[-3], "close", 0.0) or 0.0)
            latest_close = float(getattr(bars[-1], "close", 0.0) or 0.0)
            if base_close > 0 and latest_close > 0:
                momentum_slope_5m = (latest_close - base_close) / max(base_close, 1e-9) / 3.0
        sector_tags = symbol_sector_tags.get(snap.symbol, [])
        primary_sector = symbol_primary_sector.get(snap.symbol, "")
        sector_stat = dict(sector_stats.get(primary_sector) or {})
        sector_link_count = int(sector_stat.get("over_3pct", 0) or 0) if primary_sector else 0
        sector_sync_signal = "sector_isolated"
        if primary_sector:
            if bool(sector_stat.get("leader_limit_up")) and int(sector_stat.get("over_5pct", 0) or 0) >= 3:
                sector_sync_signal = "sector_cascade"
            elif int(sector_stat.get("over_3pct", 0) or 0) >= 3:
                sector_sync_signal = "sector_sync_strong"
        sector_linked = sector_sync_signal in {"sector_sync_strong", "sector_cascade"}
        if primary_sector in hot_sectors and sector_sync_signal == "sector_isolated":
            sector_sync_signal = "sector_sync_watch"
        entry: dict[str, Any] | None = None
        if 0.03 <= change_pct < 0.05 and spread_pct <= 0.016:
            early_momentum_count += 1
            entry = {
                "symbol": snap.symbol,
                "name": snap.name or snap.symbol,
                "signal_type": "early_momentum",
                "last_price": last_price,
                "pre_close": pre_close,
                "change_pct": round(change_pct, 6),
                "spread_pct": round(spread_pct, 6),
                "volume_ratio_5m": round(volume_ratio_5m, 4),
                "momentum_slope_5m": round(momentum_slope_5m, 6),
                "sector_tags": sector_tags,
                "resolved_sector": primary_sector,
                "sector_link_count": sector_link_count,
                "sector_linked": sector_linked,
                "sector_sync_signal": sector_sync_signal,
                "priority": round(change_pct * 65 + volume_ratio_5m * 4, 3),
                "summary": (
                    f"早动量观察: 涨幅 {change_pct:.2%}，5m斜率 {momentum_slope_5m:.2%}，"
                    f"量比 {volume_ratio_5m:.2f}，板块同步 {sector_sync_signal}"
                ),
            }
        elif 0.05 <= change_pct < 0.07 and spread_pct <= 0.014 and momentum_slope_5m >= 0.02 and volume_ratio_5m >= 1.5:
            acceleration_count += 1
            entry = {
                "symbol": snap.symbol,
                "name": snap.name or snap.symbol,
                "signal_type": "acceleration",
                "last_price": last_price,
                "pre_close": pre_close,
                "change_pct": round(change_pct, 6),
                "spread_pct": round(spread_pct, 6),
                "volume_ratio_5m": round(volume_ratio_5m, 4),
                "momentum_slope_5m": round(momentum_slope_5m, 6),
                "sector_tags": sector_tags,
                "resolved_sector": primary_sector,
                "sector_link_count": sector_link_count,
                "sector_linked": sector_linked,
                "sector_sync_signal": sector_sync_signal,
                "priority": round(change_pct * 95 + volume_ratio_5m * 8 + (8.0 if sector_linked else 0.0), 3),
                "summary": (
                    f"加速段观察: 涨幅 {change_pct:.2%}，5m斜率 {momentum_slope_5m:.2%}，"
                    f"量比 {volume_ratio_5m:.2f}，板块同步 {sector_sync_signal}"
                ),
            }
        elif change_pct >= 0.07 and spread_pct <= 0.012:
            pre_limit_up_count += 1
            entry = {
                "symbol": snap.symbol,
                "name": snap.name or snap.symbol,
                "signal_type": "pre_limit_up",
                "last_price": last_price,
                "pre_close": pre_close,
                "change_pct": round(change_pct, 6),
                "spread_pct": round(spread_pct, 6),
                "volume_ratio_5m": round(volume_ratio_5m, 4),
                "momentum_slope_5m": round(momentum_slope_5m, 6),
                "sector_tags": sector_tags,
                "resolved_sector": primary_sector,
                "sector_link_count": sector_link_count,
                "sector_linked": sector_linked,
                "sector_sync_signal": sector_sync_signal,
                "priority": round(change_pct * 120 + max(0.0, (limit_pct - change_pct) * -100) + volume_ratio_5m * 8, 3),
                "summary": (
                    f"预涨停观察: 涨幅 {change_pct:.2%}，5m斜率 {momentum_slope_5m:.2%}，"
                    f"量比 {volume_ratio_5m:.2f}，板块同步 {sector_sync_signal}"
                ),
            }
        elif change_pct <= min(-0.06, -(limit_pct - 0.02)):
            abnormal_drop_count += 1
            entry = {
                "symbol": snap.symbol,
                "name": snap.name or snap.symbol,
                "signal_type": "abnormal_drop",
                "last_price": last_price,
                "pre_close": pre_close,
                "change_pct": round(change_pct, 6),
                "spread_pct": round(spread_pct, 6),
                "volume_ratio_5m": round(volume_ratio_5m, 4),
                "sector_tags": sector_tags,
                "resolved_sector": primary_sector,
                "sector_link_count": sector_link_count,
                "sector_linked": sector_linked,
                "sector_sync_signal": sector_sync_signal,
                "priority": round(abs(change_pct) * 100, 3),
                "summary": f"异常下跌观察: 跌幅 {change_pct:.2%}",
            }
        if entry:
            items.append(entry)
    items = sorted(items, key=lambda item: (-float(item.get("priority", 0.0) or 0.0), item.get("symbol", "")))[:12]
    payload = {
        "available": bool(items),
        "generated_at": now.isoformat(),
        "trade_date": now.date().isoformat(),
        "count": len(items),
        "early_momentum_count": early_momentum_count,
        "acceleration_count": acceleration_count,
        "pre_limit_up_count": pre_limit_up_count,
        "abnormal_drop_count": abnormal_drop_count,
        "summary_lines": [
            (
                f"快路径机会扫描完成: symbols={len(symbols)} opportunities={len(items)} "
                f"early_momentum={early_momentum_count} acceleration={acceleration_count} "
                f"pre_limit_up={pre_limit_up_count} abnormal_drop={abnormal_drop_count}."
            )
        ],
        "items": items,
    }
    _persist_fast_opportunity_scan(position_watch_state_store, payload)
    return payload


def _upsert_intraday_candidate_tickets(
    *,
    trade_date: str,
    opportunity_payload: dict[str, Any],
    candidate_case_service,
    position_watch_state_store: StateStore | None,
) -> dict[str, Any]:
    if not candidate_case_service:
        return {"ok": False, "reason": "candidate_case_service_unavailable", "ticket_count": 0, "case_count": 0}
    runtime = dict(position_watch_state_store.get("intraday_candidate_injection_runtime", {}) or {}) if position_watch_state_store else {}
    seen_keys = list(runtime.get("seen_keys") or [])
    seen_key_set = set(str(item) for item in seen_keys if str(item).strip())
    tickets: list[dict[str, Any]] = []
    injected_keys: list[str] = []
    for item in list(opportunity_payload.get("items") or []):
        signal_type = str(item.get("signal_type") or "").strip()
        if signal_type not in {"pre_limit_up", "acceleration"}:
            continue
        change_pct = float(item.get("change_pct") or 0.0)
        volume_ratio_5m = float(item.get("volume_ratio_5m") or 0.0)
        momentum_slope_5m = float(item.get("momentum_slope_5m") or 0.0)
        if change_pct < 0.05 or volume_ratio_5m < 1.5:
            continue
        if signal_type == "acceleration" and momentum_slope_5m < 0.02:
            continue
        if str(item.get("sector_sync_signal") or "") == "sector_isolated":
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        dedupe_key = (
            f"{trade_date}:{symbol}:{signal_type}:"
            f"{round(change_pct, 4)}:{round(volume_ratio_5m, 3)}:{str(opportunity_payload.get('generated_at') or '')[:16]}"
        )
        if dedupe_key in seen_key_set:
            continue
        sector_tags = [str(tag) for tag in list(item.get("sector_tags") or []) if str(tag).strip()]
        resolved_sector = str(item.get("resolved_sector") or "").strip()
        tickets.append(
            {
                "symbol": symbol,
                "name": str(item.get("name") or symbol),
                "source": "intraday_opportunity_scan",
                "source_tags": ["intraday", signal_type],
                "source_role": "scheduler.micro_watch",
                "market_logic": (
                    f"{symbol} 盘中出现{'预涨停' if signal_type == 'pre_limit_up' else '加速'}基因，"
                    f"涨幅 {change_pct:.2%}，5m 量比 {volume_ratio_5m:.2f}，"
                    f"5m 斜率 {momentum_slope_5m:.2%}，板块同步 {str(item.get('sector_sync_signal') or 'unknown')}。"
                ),
                "core_evidence": [
                    str(item.get("summary") or "").strip(),
                    f"5m量比={volume_ratio_5m:.2f}",
                    f"5m斜率={momentum_slope_5m:.2%}",
                    f"板块同步={str(item.get('sector_sync_signal') or 'unknown')}",
                ],
                "risk_points": [
                    "盘中异动持续性待验证",
                    "若资金回落或板块联动减弱，应及时降级为观察票",
                ],
                "why_now": (
                    f"涨幅 {change_pct:.2%} 且量能放大到 {volume_ratio_5m:.2f}x，"
                    f"{resolved_sector or '所属方向'}出现同步联动，处于{signal_type}窗口。"
                ),
                "trigger_type": signal_type,
                "trigger_time": str(opportunity_payload.get("generated_at") or datetime.now().isoformat()),
                "recommended_action": "进入讨论候选，优先走盘中快讨论" if signal_type == "pre_limit_up" else "进入盘中加速观察并触发快讨论",
                "evidence_refs": [f"intraday_opportunity:{trade_date}:{symbol}:{signal_type}"],
                "submitted_by": "scheduler.micro_watch",
                "selection_score": round(change_pct * 100 + volume_ratio_5m * 2 + momentum_slope_5m * 100, 4),
                "action": "WATCH",
                "resolved_sector": resolved_sector,
                "sector_tags": sector_tags,
            }
        )
        injected_keys.append(dedupe_key)
    cases = candidate_case_service.upsert_candidate_tickets(trade_date, tickets) if tickets else []
    if position_watch_state_store and injected_keys:
        merged_keys = list(dict.fromkeys([*seen_keys, *injected_keys]))[-200:]
        runtime["seen_keys"] = merged_keys
        runtime["updated_at"] = datetime.now().isoformat()
        position_watch_state_store.set("intraday_candidate_injection_runtime", runtime)
    return {
        "ok": True,
        "reason": "success",
        "ticket_count": len(tickets),
        "case_count": len(cases),
        "symbols": [item.symbol for item in cases],
    }


def run_fast_position_watch_scan(
    *,
    settings: AppSettings,
    market: MarketDataAdapter,
    execution_adapter: ExecutionAdapter,
    meeting_state_store: StateStore | None,
    runtime_state_store: StateStore | None,
    execution_gateway_state_store: StateStore | None = None,
    position_watch_state_store: StateStore | None = None,
    monitor_state_service=None,
    discussion_cycle_service=None,
    dispatcher=None,
    order_strategy_resolver: OrderStrategyResolver | None = None,
    quality_tracker: ExecutionQualityTracker | None = None,
    trace_service: TradeTraceService | None = None,
    execution_plane: str | None = None,
    account_id: str | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_factory or datetime.now)()
    function_started_at = time.perf_counter()
    account_id = account_id or _resolve_scheduler_account_id(settings, execution_adapter)
    gateway_state_store = resolve_execution_gateway_state_store(execution_gateway_state_store, runtime_state_store)
    runtime = _load_fast_position_watch_runtime(position_watch_state_store)
    symbol_runtime = dict(runtime.get("symbols") or {})
    runtime_context = dict(runtime_state_store.get("latest_runtime_context", {}) or {}) if runtime_state_store else {}
    playbook_map = _resolve_runtime_symbol_context_map(runtime_context)
    execution_plane = str(execution_plane or getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant")
    queue_for_gateway = execution_plane == "windows_gateway"
    submit_orders = bool(settings.run_mode == "live" and settings.live_trade_enabled and not queue_for_gateway)
    pending_sell_tracker, open_sell_symbols = _load_fast_pending_sell_tracker(runtime, now)
    if not queue_for_gateway:
        pending_sell_tracker, refreshed_open_sell_symbols = _refresh_fast_pending_sell_tracker_from_orders(
            pending_sell_tracker,
            execution_adapter=execution_adapter,
            account_id=account_id,
            now=now,
        )
        open_sell_symbols = refreshed_open_sell_symbols or open_sell_symbols
    try:
        positions = [item for item in execution_adapter.get_positions(account_id) if int(item.quantity) > 0]
    except Exception as exc:
        payload = {
            "status": "error",
            "mode": "fast_intraday",
            "account_id": account_id,
            "trade_date": now.date().isoformat(),
            "scanned_at": now.isoformat(),
            "execution_plane": execution_plane,
            "position_count": 0,
            "sell_signal_count": 0,
            "day_trading_signal_count": 0,
            "signal_count": 0,
            "submitted_count": 0,
            "queued_count": 0,
            "preview_count": 0,
            "error_count": 1,
            "summary_lines": [f"快路径持仓巡视失败: {exc}"],
            "items": [],
        }
        _persist_position_watch_scan(position_watch_state_store or meeting_state_store, payload)
        return payload

    if not positions:
        payload = {
            "status": "ok",
            "mode": "fast_intraday",
            "account_id": account_id,
            "trade_date": now.date().isoformat(),
            "scanned_at": now.isoformat(),
            "execution_plane": execution_plane,
            "position_count": 0,
            "sell_signal_count": 0,
            "day_trading_signal_count": 0,
            "signal_count": 0,
            "submitted_count": 0,
            "queued_count": 0,
            "preview_count": 0,
            "error_count": 0,
            "summary_lines": ["快路径持仓巡视完成: 当前无可用持仓。"],
            "items": [],
        }
        _persist_position_watch_scan(position_watch_state_store or meeting_state_store, payload)
        return payload

    quote_map = {item.symbol: item for item in market.get_snapshots([item.symbol for item in positions])}

    sell_signal_count = 0
    day_trading_signal_count = 0
    submitted_count = 0
    queued_count = 0
    preview_count = 0
    error_count = 0
    items: list[dict[str, Any]] = []
    latency_alerts: list[dict[str, Any]] = []
    updated_symbol_runtime: dict[str, Any] = {}
    for position in positions:
        symbol = position.symbol
        quote = quote_map.get(symbol)
        if quote is None:
            quote = QuoteSnapshot(
                symbol=symbol,
                name=market.get_symbol_name(symbol),
                last_price=float(position.last_price or position.cost_price or 0.0),
                bid_price=float(position.last_price or position.cost_price or 0.0),
                ask_price=float(position.last_price or position.cost_price or 0.0),
                volume=0.0,
                pre_close=0.0,
            )
        prev_runtime = dict(symbol_runtime.get(symbol) or {})
        playbook_context = dict(playbook_map.get(symbol) or {})
        atr = _estimate_position_atr(
            float(position.cost_price or 0.0),
            float(quote.last_price or position.last_price or position.cost_price or 0.0),
            dict(playbook_context.get("exit_params") or {}),
        )
        atr_pct = atr / max(float(position.cost_price or 0.0), 1e-9) if float(position.cost_price or 0.0) > 0 else 0.0
        current_price = float(quote.last_price or position.last_price or position.cost_price or 0.0)
        peak_price = max(float(prev_runtime.get("peak_price", 0.0) or 0.0), current_price)
        prev_price = float(prev_runtime.get("last_price", current_price) or current_price)
        last_change_pct = (current_price - prev_price) / max(prev_price, 1e-9) if prev_price > 0 else 0.0
        spread_pct = (
            abs(float(quote.ask_price or current_price) - float(quote.bid_price or current_price)) / max(current_price, 1e-9)
            if current_price > 0
            else 0.0
        )
        item = {
            "symbol": symbol,
            "name": quote.name or market.get_symbol_name(symbol),
            "available": int(position.available),
            "entry_price": float(position.cost_price or 0.0),
            "current_price": current_price,
            "peak_price": peak_price,
            "pnl_pct": round((current_price - float(position.cost_price or 0.0)) / max(float(position.cost_price or 0.0), 1e-9), 6)
            if float(position.cost_price or 0.0) > 0
            else 0.0,
            "last_change_pct": round(last_change_pct, 6),
            "spread_pct": round(spread_pct, 6),
            "status": "hold",
            "execution_plane": execution_plane,
            "review_tags": ["fast_path", "intraday"],
        }
        signal = _build_fast_position_watch_signal(
            symbol=symbol,
            entry_price=float(position.cost_price or 0.0),
            current_price=current_price,
            peak_price=peak_price,
            available=int(position.available),
            last_change_pct=last_change_pct,
            spread_pct=spread_pct,
            atr_pct=atr_pct,
        )
        if signal is None:
            items.append(item)
            updated_symbol_runtime[symbol] = {
                "last_price": current_price,
                "peak_price": peak_price,
                "last_scanned_at": now.isoformat(),
                "last_signal": "",
            }
            continue
        item.update(signal)
        signal_type = str(signal.get("signal_type") or "").strip().lower()
        is_executable_day_trading_sell = signal_type == "day_trading" and str(signal.get("t_signal_action") or "").upper() == "HIGH_SELL"
        if signal_type == "sell":
            sell_signal_count += 1
            item["review_tags"] = list(dict.fromkeys(list(item.get("review_tags") or []) + [str(signal.get("exit_reason") or "")]))
        else:
            day_trading_signal_count += 1
            item["review_tags"] = list(dict.fromkeys(list(item.get("review_tags") or []) + [str(signal.get("t_signal_action") or "").lower()]))
        if signal_type == "sell" or is_executable_day_trading_sell:
            sell_signal_count += 1 if is_executable_day_trading_sell else 0
        if symbol in open_sell_symbols and (signal_type == "sell" or is_executable_day_trading_sell):
            item["status"] = "skipped_pending_sell"
            item["preview_reason"] = "existing_pending_sell_order"
            preview_count += 1
            items.append(item)
            updated_symbol_runtime[symbol] = {
                "last_price": current_price,
                "peak_price": peak_price,
                "last_scanned_at": now.isoformat(),
                "last_signal": str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
            }
            continue
        if signal_type == "sell" or is_executable_day_trading_sell:
            signal_started_at = time.perf_counter()
            order_plan: OrderExecutionPlan | None = None
            if order_strategy_resolver is not None:
                order_plan = order_strategy_resolver.resolve(
                    side="SELL",
                    quote=quote,
                    scenario="urgent_exit",
                    signal_price=current_price,
                )
            sell_ratio = (
                float(signal.get("sell_ratio", 1.0) or 1.0)
                if signal_type == "sell"
                else (
                    float(signal.get("t_signal_quantity") or 0.0) / max(int(position.available), 1)
                    if int(position.available) > 0
                    else 0.0
                )
            )
            sell_quantity = _normalize_sell_quantity(int(position.available), sell_ratio)
            item["planned_quantity"] = sell_quantity
            item["intraday_signal"] = _build_intraday_signal_snapshot(
                mode="fast_intraday",
                symbol=symbol,
                signal_reason=str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
                signal_type="sell",
                current_price=current_price,
                payload=signal,
            )
            if sell_quantity <= 0:
                item["status"] = "skipped_min_lot"
                item["preview_reason"] = "sell_quantity_below_min_lot"
                preview_count += 1
                items.append(item)
                updated_symbol_runtime[symbol] = {
                    "last_price": current_price,
                    "peak_price": peak_price,
                    "last_scanned_at": now.isoformat(),
                    "last_signal": str(signal.get("exit_reason") or ""),
                }
                continue
            request = PlaceOrderRequest(
                account_id=account_id,
                symbol=symbol,
                side="SELL",
                quantity=sell_quantity,
                price=float(
                    (order_plan.price if order_plan is not None else 0.0)
                    or quote.bid_price
                    or quote.last_price
                    or position.last_price
                    or position.cost_price
                    or 0.0
                ),
                request_id=f"fast-sell-{symbol.replace('.', '-')}-{uuid4().hex[:8]}",
                decision_id=None,
                trade_date=now.date().isoformat(),
                exit_reason=str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
                trace_id=f"trace-fast-sell-{symbol.replace('.', '-')}-{uuid4().hex[:10]}",
                signal_price=current_price,
                signal_time=now.isoformat(),
                order_type=(order_plan.order_type if order_plan is not None else "opponent_best"),
                time_in_force=(order_plan.time_in_force if order_plan is not None else "day"),
                urgency_tag=(order_plan.urgency_tag if order_plan is not None else "immediate"),
            )
            item["order_execution_plan"] = order_plan.to_payload() if order_plan is not None else None
            _append_scheduler_trade_trace(
                trace_service,
                trace_id=request.trace_id,
                stage="scheduler_fast_sell_signal",
                trade_date=str(request.trade_date or now.date().isoformat()),
                payload={
                    "symbol": symbol,
                    "signal_type": signal_type,
                    "signal_reason": str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
                    "planned_quantity": sell_quantity,
                    "execution_plan": dict(item.get("order_execution_plan") or {}),
                },
            )
            fast_track_review = _fast_track_exit_review(
                discussion_cycle_service,
                trade_date=str(request.trade_date or now.date().isoformat()),
                symbol=symbol,
                signal_reason=str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
                signal_payload={
                    "mode": "fast_intraday",
                    "current_price": current_price,
                    "planned_quantity": sell_quantity,
                    "peak_price": peak_price,
                    "pnl_pct": item.get("pnl_pct"),
                    "last_change_pct": item.get("last_change_pct"),
                },
            )
            item["fast_track_review"] = fast_track_review
            if not bool(fast_track_review.get("approved", True)):
                item["status"] = "blocked_by_fast_track_review"
                item["preview_reason"] = str(fast_track_review.get("reason") or "fast_track_review_blocked")
                preview_count += 1
                item["request"] = request.model_dump()
                items.append(item)
                updated_symbol_runtime[symbol] = {
                    "last_price": current_price,
                    "peak_price": peak_price,
                    "last_scanned_at": now.isoformat(),
                    "last_signal": str(signal.get("exit_reason") or ""),
                }
                continue
            gateway_intent = _build_tail_market_gateway_intent(
                request,
                name=item["name"],
                decision_id=request.decision_id,
                signal_reason=str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
                playbook=None,
                regime=None,
                resolved_sector=None,
                review_tags=list(item.get("review_tags") or []),
                exit_context_snapshot={"fast_path": True, "peak_price": peak_price},
            )
            gateway_intent["discussion_context"]["trigger_source"] = "fast_position_watch"
            gateway_intent["discussion_context"]["intraday_signal"] = dict(item.get("intraday_signal") or {})
            gateway_intent["discussion_context"]["fast_track_review"] = dict(fast_track_review or {})
            if queue_for_gateway:
                if gateway_state_store is None:
                    item["status"] = "preview"
                    item["preview_reason"] = "execution_gateway_state_unavailable"
                    preview_count += 1
                else:
                    queued_packet = enqueue_execution_gateway_intent(
                        gateway_state_store,
                        gateway_intent,
                        run_mode=str(settings.run_mode),
                        approval_source="fast_position_watch",
                        summary_lines=["快路径卖出意图已批准，等待 Windows Execution Gateway 拉取。"],
                    )
                    item["status"] = "queued_for_gateway"
                    item["gateway_intent"] = queued_packet
                    item["queued_at"] = queued_packet.get("approved_at")
                    item["gateway_pull_path"] = EXECUTION_GATEWAY_PENDING_PATH
                    queued_count += 1
                    _record_scheduler_sell_submission(
                        quality_tracker,
                        request=request,
                        quote=quote,
                        submit_time=str(queued_packet.get("approved_at") or now.isoformat()),
                        status="queued_for_gateway",
                        metadata={"source": "scheduler_fast_position_watch"},
                    )
                    _append_scheduler_trade_trace(
                        trace_service,
                        trace_id=request.trace_id,
                        stage="scheduler_fast_sell_queued",
                        trade_date=str(request.trade_date or now.date().isoformat()),
                        payload={
                            "symbol": symbol,
                            "intent_id": str(queued_packet.get("intent_id") or ""),
                            "gateway_pull_path": EXECUTION_GATEWAY_PENDING_PATH,
                        },
                    )
                    _mark_fast_pending_sell_symbol(
                        pending_sell_tracker,
                        symbol=symbol,
                        now=now,
                        status="queued_for_gateway",
                        source="execution_gateway",
                        intent_id=str(queued_packet.get("intent_id") or ""),
                    )
                    sample = record_latency_sample(
                        position_watch_state_store or meeting_state_store,
                        chain="sell_signal_to_order_submit",
                        stage="fast_path_queue_for_gateway",
                        elapsed_ms=(time.perf_counter() - signal_started_at) * 1000.0,
                        threshold_ms=5000.0,
                        trade_date=str(request.trade_date or now.date().isoformat()),
                        metadata={"symbol": symbol, "mode": "fast_intraday", "execution_plane": execution_plane},
                    )
                    if sample.get("status") == "alert":
                        latency_alerts.append(sample)
            elif submit_orders:
                try:
                    order = execution_adapter.place_order(request)
                    item["status"] = "submitted"
                    item["order_id"] = order.order_id
                    item["submitted_at"] = now.isoformat()
                    submitted_count += 1
                    _record_scheduler_sell_submission(
                        quality_tracker,
                        request=request,
                        quote=quote,
                        submit_time=item["submitted_at"],
                        status="submitted",
                        order_id=str(order.order_id or ""),
                        metadata={"source": "scheduler_fast_position_watch"},
                    )
                    _append_scheduler_trade_trace(
                        trace_service,
                        trace_id=request.trace_id,
                        stage="scheduler_fast_sell_submitted",
                        trade_date=str(request.trade_date or now.date().isoformat()),
                        payload={
                            "symbol": symbol,
                            "order_id": str(order.order_id or ""),
                            "price": float(order.price or request.price or 0.0),
                            "quantity": int(order.quantity or request.quantity or 0),
                        },
                    )
                    _mark_fast_pending_sell_symbol(
                        pending_sell_tracker,
                        symbol=symbol,
                        now=now,
                        status=str(order.status or "PENDING"),
                        source="execution_adapter",
                        order_id=str(order.order_id or ""),
                    )
                    _append_scheduler_order_journal(
                        meeting_state_store,
                        request,
                        order_id=order.order_id,
                        name=item["name"],
                        submitted_at=now.isoformat(),
                        source="scheduler_fast_position_watch",
                        extra_metadata={
                            "fast_path": True,
                            "review_tags": list(item.get("review_tags") or []),
                            "intraday_signal": dict(item.get("intraday_signal") or {}),
                            "fast_track_review": dict(fast_track_review or {}),
                        },
                    )
                    sample = record_latency_sample(
                        position_watch_state_store or meeting_state_store,
                        chain="sell_signal_to_order_submit",
                        stage="fast_path_submit_order",
                        elapsed_ms=(time.perf_counter() - signal_started_at) * 1000.0,
                        threshold_ms=5000.0,
                        trade_date=str(request.trade_date or now.date().isoformat()),
                        metadata={"symbol": symbol, "mode": "fast_intraday", "execution_plane": execution_plane},
                    )
                    if sample.get("status") == "alert":
                        latency_alerts.append(sample)
                except Exception as exc:
                    item["status"] = "error"
                    item["error"] = str(exc)
                    error_count += 1
                    _append_scheduler_trade_trace(
                        trace_service,
                        trace_id=request.trace_id,
                        stage="scheduler_fast_sell_failed",
                        trade_date=str(request.trade_date or now.date().isoformat()),
                        payload={"symbol": symbol, "error": str(exc)},
                    )
            else:
                item["status"] = "preview"
                item["preview_reason"] = f"submit_disabled_in_{settings.run_mode}"
                preview_count += 1
            item["request"] = request.model_dump()
        else:
            item["status"] = "t_signal"
        items.append(item)
        updated_symbol_runtime[symbol] = {
            "last_price": current_price,
            "peak_price": peak_price,
            "last_scanned_at": now.isoformat(),
            "last_signal": str(signal.get("exit_reason") or signal.get("t_signal_action") or ""),
        }

    runtime["symbols"] = updated_symbol_runtime
    runtime["pending_sell_tracker"] = pending_sell_tracker
    runtime["updated_at"] = now.isoformat()
    _persist_fast_position_watch_runtime(position_watch_state_store, runtime)
    payload = {
        "status": "ok" if error_count == 0 else "error",
        "mode": "fast_intraday",
        "account_id": account_id,
        "trade_date": now.date().isoformat(),
        "scanned_at": now.isoformat(),
        "execution_plane": execution_plane,
        "position_count": len(positions),
        "sell_signal_count": sell_signal_count,
        "day_trading_signal_count": day_trading_signal_count,
        "signal_count": sell_signal_count + day_trading_signal_count,
        "submitted_count": submitted_count,
        "queued_count": queued_count,
        "preview_count": preview_count,
        "error_count": error_count,
        "summary_lines": [
            (
                f"快路径持仓巡视完成: positions={len(positions)} sell_signals={sell_signal_count} "
                f"t_signals={day_trading_signal_count} submitted={submitted_count} "
                f"queued={queued_count} preview={preview_count} errors={error_count}."
            )
        ],
        "items": items,
        "action_suggestions": _build_position_watch_action_suggestions(items),
    }
    cycle_sample = record_latency_sample(
        position_watch_state_store or meeting_state_store,
        chain="position_watch_cycle",
        stage="fast_intraday_cycle",
        elapsed_ms=(time.perf_counter() - function_started_at) * 1000.0,
        threshold_ms=3000.0,
        trade_date=payload["trade_date"],
        metadata={"mode": "fast_intraday", "position_count": len(positions)},
    )
    payload["latency_tracker"] = get_latency_tracker_snapshot(position_watch_state_store or meeting_state_store)
    if cycle_sample.get("status") == "alert":
        latency_alerts.append(cycle_sample)
    if latency_alerts:
        payload["latency_alerts"] = latency_alerts
        payload["summary_lines"].append(f"延迟告警 {len(latency_alerts)} 条，需检查快路径执行链。")
    if queue_for_gateway:
        payload["gateway_pull_path"] = EXECUTION_GATEWAY_PENDING_PATH
    _persist_position_watch_scan(position_watch_state_store or meeting_state_store, payload)
    should_persist_monitor = bool(monitor_state_service) and payload.get("signal_count", 0) > 0
    if should_persist_monitor:
        monitor_state_service.save_exit_snapshot(
            _build_position_watch_exit_snapshot(payload, now),
            trigger="position_watch_fast_intraday",
        )
        monitor_state_service.save_position_watch_snapshot(payload, trigger="position_watch_fast_intraday")
    return payload


def _should_run_fast_opportunity_scan(
    state_store: StateStore | None,
    now: datetime,
    *,
    min_interval_seconds: int = 12,
) -> bool:
    runtime = _load_fast_position_watch_runtime(state_store)
    last_scanned_at = _parse_iso_datetime(str(runtime.get("last_opportunity_scan_at") or ""))
    if last_scanned_at is not None:
        last_scanned_at, normalized_now = _normalize_datetime_pair(last_scanned_at, now)
        if max((normalized_now - last_scanned_at).total_seconds(), 0.0) < min_interval_seconds:
            return False
    runtime["last_opportunity_scan_at"] = now.isoformat()
    _persist_fast_position_watch_runtime(state_store, runtime)
    return True


def _build_intraday_opportunity_watchlist(
    alerts: list[Any],
    snapshots: list[QuoteSnapshot],
    *,
    generated_at: str,
    trade_date: str,
) -> dict[str, Any]:
    snapshot_map = {item.symbol: item for item in snapshots}
    grouped: dict[str, dict[str, Any]] = {}
    alert_score_map = {
        "price_spike": 2.5,
        "volume_surge": 1.5,
        "limit_up": 1.0,
    }
    severity_boost = {
        "critical": 2.0,
        "warning": 1.0,
        "info": 0.5,
    }
    for alert in alerts:
        alert_type = str(getattr(alert, "alert_type", "") or "").strip().lower()
        change_pct = float(getattr(alert, "change_pct", 0.0) or 0.0)
        if alert_type not in alert_score_map:
            continue
        if alert_type != "volume_surge" and change_pct <= 0:
            continue
        symbol = str(getattr(alert, "symbol", "") or "").strip()
        if not symbol:
            continue
        entry = grouped.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": snapshot_map.get(symbol).name if symbol in snapshot_map else symbol,
                "alert_types": [],
                "messages": [],
                "score": 0.0,
                "last_price": float(getattr(alert, "price", 0.0) or 0.0),
                "change_pct": change_pct,
                "severity": str(getattr(alert, "severity", "info") or "info"),
            },
        )
        entry["score"] += alert_score_map.get(alert_type, 0.0) + severity_boost.get(entry["severity"], 0.5)
        if alert_type not in entry["alert_types"]:
            entry["alert_types"].append(alert_type)
        message = str(getattr(alert, "message", "") or "").strip()
        if message and message not in entry["messages"]:
            entry["messages"].append(message)
        snap = snapshot_map.get(symbol)
        if snap is not None:
            entry["last_price"] = float(snap.last_price or entry["last_price"] or 0.0)
            if float(snap.pre_close or 0.0) > 0 and float(snap.last_price or 0.0) > 0:
                entry["change_pct"] = round((float(snap.last_price) - float(snap.pre_close)) / float(snap.pre_close), 6)
    items = sorted(grouped.values(), key=lambda item: (-float(item.get("score", 0.0) or 0.0), -float(item.get("change_pct", 0.0) or 0.0)))[:10]
    summary_lines = [
        f"盘中机会观察名单已刷新: {len(items)} 只标的进入快照。"
    ]
    if items:
        summary_lines.append(
            "优先观察: " + "；".join(
                f"{item['symbol']} {item['change_pct']:+.2%} {','.join(item['alert_types'][:2])}"
                for item in items[:5]
            )
        )
    return {
        "available": bool(items),
        "generated_at": generated_at,
        "trade_date": trade_date,
        "count": len(items),
        "items": items,
        "summary_lines": summary_lines,
    }


def run_position_watch_scan(
    *,
    settings: AppSettings,
    market: MarketDataAdapter,
    execution_adapter: ExecutionAdapter,
    meeting_state_store: StateStore | None,
    runtime_state_store: StateStore | None,
    execution_gateway_state_store: StateStore | None = None,
    position_watch_state_store: StateStore | None = None,
    monitor_state_service=None,
    candidate_case_service=None,
    discussion_cycle_service=None,
    dispatcher=None,
    order_strategy_resolver: OrderStrategyResolver | None = None,
    quality_tracker: ExecutionQualityTracker | None = None,
    trace_service: TradeTraceService | None = None,
    runtime_context: dict | None = None,
    discussion_context: dict | None = None,
    event_context: dict | None = None,
    execution_plane: str | None = None,
    account_id: str | None = None,
    mode: str = "intraday",
    include_day_trading: bool = True,
    allow_live_sell_submit: bool = True,
    now_factory: Callable[[], datetime] | None = None,
) -> dict:
    now = (now_factory or datetime.now)()
    function_started_at = time.perf_counter()
    resolved_mode = str(mode or "intraday").strip().lower() or "intraday"
    account_id = account_id or _resolve_scheduler_account_id(settings, execution_adapter)
    gateway_state_store = resolve_execution_gateway_state_store(execution_gateway_state_store, runtime_state_store)
    if runtime_context is None and runtime_state_store:
        runtime_context = runtime_state_store.get("latest_runtime_context", {}) or {}
    runtime_context = runtime_context or {}
    discussion_context = discussion_context or {}
    event_context = _build_intraday_event_context(settings, event_context)
    trade_date = (
        runtime_context.get("trade_date")
        or discussion_context.get("trade_date")
        or now.date().isoformat()
    )
    market_profile = runtime_context.get("market_profile") or {}
    regime_guard = _sync_market_regime_guard(
        runtime_state_store,
        trade_date=str(trade_date),
        detected_regime=(runtime_state_store.get("latest_market_regime", {}) if runtime_state_store else {}) or {},
    )
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
    latest_journal_by_symbol: dict[str, dict] = {}
    latest_buy_by_symbol: dict[str, dict] = {}
    open_sell_symbols: set[str] = set()
    journal_loaded = False

    def _ensure_order_context_loaded() -> None:
        nonlocal journal_loaded, latest_journal_by_symbol, latest_buy_by_symbol, open_sell_symbols
        if journal_loaded:
            return
        journal_loaded = True
        journal = meeting_state_store.get("execution_order_journal", []) if meeting_state_store else []
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
    case_map_loaded = False

    def _ensure_case_map_loaded() -> None:
        nonlocal case_map_loaded, case_map_by_symbol
        if case_map_loaded:
            return
        case_map_loaded = True
        if candidate_case_service and trade_date:
            case_map_by_symbol = {
                item.symbol: item
                for item in candidate_case_service.list_cases(trade_date=trade_date, limit=500)
            }

    if resolved_mode == "tail":
        _ensure_order_context_loaded()
        _ensure_case_map_loaded()

    try:
        positions = [item for item in execution_adapter.get_positions(account_id) if int(item.quantity) > 0]
    except Exception as exc:
        payload = {
            "status": "error",
            "mode": resolved_mode,
            "account_id": account_id,
            "trade_date": runtime_context.get("trade_date") or now.date().isoformat(),
            "scanned_at": now.isoformat(),
            "execution_plane": str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
            "position_count": 0,
            "sell_signal_count": 0,
            "day_trading_signal_count": 0,
            "submitted_count": 0,
            "queued_count": 0,
            "preview_count": 0,
            "error_count": 1,
            "summary_lines": [f"{'尾盘' if resolved_mode == 'tail' else '盘中'}持仓巡视失败: {exc}"],
            "items": [],
        }
        _persist_position_watch_scan(position_watch_state_store or meeting_state_store, payload)
        if resolved_mode == "tail":
            _persist_tail_market_scan(meeting_state_store, payload)
        return payload

    if not positions:
        payload = {
            "status": "ok",
            "mode": resolved_mode,
            "account_id": account_id,
            "trade_date": trade_date,
            "scanned_at": now.isoformat(),
            "execution_plane": str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
            "position_count": 0,
            "sell_signal_count": 0,
            "day_trading_signal_count": 0,
            "submitted_count": 0,
            "queued_count": 0,
            "preview_count": 0,
            "error_count": 0,
            "summary_lines": [f"{'尾盘卖出扫描' if resolved_mode == 'tail' else '盘中持仓巡视'}完成: 当前无可用持仓。"],
            "items": [],
        }
        _persist_position_watch_scan(position_watch_state_store or meeting_state_store, payload)
        if resolved_mode == "tail":
            _persist_tail_market_scan(meeting_state_store, payload)
        if monitor_state_service:
            monitor_state_service.save_exit_snapshot(
                _build_position_watch_exit_snapshot(payload, now),
                trigger=f"position_watch_{resolved_mode}",
            )
            monitor_state_service.save_position_watch_snapshot(payload, trigger=f"position_watch_{resolved_mode}")
        return payload

    quote_map = {
        item.symbol: item
        for item in market.get_snapshots([item.symbol for item in positions])
    }
    intraday_bar_map: dict[str, dict] = {}
    intraday_return_map: dict[str, list[dict]] = {}
    micro_bar_map_1m: dict[str, dict] = {}
    micro_return_map_1m: dict[str, list[dict]] = {}
    grouped_bars: dict[str, list[Any]] = {}
    grouped_micro_bars_1m: dict[str, list[Any]] = {}
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
    settings_fields_set = set(getattr(settings, "__pydantic_fields_set__", set()) or set())
    resolved_execution_plane = execution_plane
    if resolved_execution_plane is None:
        resolved_execution_plane = (
            str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant")
            if "execution_plane" in settings_fields_set
            else "local_xtquant"
        )
    execution_plane = str(resolved_execution_plane or "local_xtquant")
    submit_orders = settings.run_mode == "live" and settings.live_trade_enabled
    queue_for_gateway = execution_plane == "windows_gateway"
    if queue_for_gateway:
        submit_orders = False
    elif settings.run_mode == "paper" and adapter_mode in {"mock", "mock-fallback"}:
        submit_orders = True
    if not allow_live_sell_submit:
        submit_orders = False

    engine = SellDecisionEngine()
    day_trading_engine = DayTradingEngine()
    sell_signal_count = 0
    day_trading_signal_count = 0
    submitted_count = 0
    queued_count = 0
    preview_count = 0
    error_count = 0
    items: list[dict] = []
    latency_alerts: list[dict[str, Any]] = []
    day_trading_rebuy_tickets: list[dict[str, Any]] = []

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
        symbol_intraday_bars = list(grouped_bars.get(symbol) or [])
        symbol_micro_bars = list(grouped_micro_bars_1m.get(symbol) or [])
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
        regime_sell_adjustment = _resolve_regime_sell_adjustment(
            regime_guard,
            position_regime=str(regime or ""),
            playbook=str(playbook or ""),
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
                exit_params={
                    **exit_params,
                    **(
                        {
                            "regime_transition_guard_active": True,
                            "regime_transition_target": str(regime_guard.get("target_runtime_regime") or ""),
                        }
                        if regime_sell_adjustment.get("mismatch")
                        else {}
                    ),
                },
            )
        review_tags = _build_tail_market_review_tags(ctx) if ctx is not None else []
        signal = (
            engine.evaluate_with_context(
                state,
                position,
                ctx=ctx,
                quote=quote,
                sector=sector,
                playbook=str(playbook or ""),
                regime=str(regime_sell_adjustment.get("effective_regime") or regime or ""),
                param_overrides=dict(regime_sell_adjustment.get("param_overrides") or {}),
            )
            if ctx is not None
            else engine.evaluate(
                state,
                playbook=str(playbook or ""),
                regime=str(regime_sell_adjustment.get("effective_regime") or regime or ""),
                param_overrides=dict(regime_sell_adjustment.get("param_overrides") or {}),
            )
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
        item_review_tags = list(review_tags)
        if regime_sell_adjustment.get("mismatch"):
            item["regime_transition_guard"] = dict(regime_guard or {})
            item["effective_sell_regime"] = regime_sell_adjustment.get("effective_regime")
            item["regime_param_overrides"] = dict(regime_sell_adjustment.get("param_overrides") or {})
            item_review_tags.extend(list(regime_sell_adjustment.get("review_tags") or []))
            item.setdefault("diagnostics", {})
            item["diagnostics"]["regime_transition_summary"] = list(regime_sell_adjustment.get("summary_lines") or [])
        if behavior_profile:
            item["behavior_profile"] = behavior_profile
        day_trading_signal = None
        if include_day_trading:
            vwap = _compute_bar_vwap(symbol_micro_bars or symbol_intraday_bars, current_price)
            open_price = _compute_open_price(symbol_micro_bars, symbol_intraday_bars, entry_price)
            day_trading_signal = day_trading_engine.evaluate(position, quote, vwap=vwap, open_price=open_price)
            if day_trading_signal.action != "HOLD":
                day_trading_signal_count += 1
                item.update(
                    {
                        "t_signal_action": day_trading_signal.action,
                        "t_signal_price": day_trading_signal.price,
                        "t_signal_quantity": day_trading_signal.quantity,
                        "t_signal_reason": day_trading_signal.reason,
                    }
                )
                if day_trading_signal.action == "HIGH_SELL":
                    item["signal_type"] = "day_trading"
                    item["severity"] = "warning"
                    item["signal_summary"] = f"盘中做T高抛建议: {day_trading_signal.reason}"
                    item_review_tags.extend(["day_trading", "intraday", "high_sell"])
                    if signal is None:
                        signal = SellSignal(
                            symbol=symbol,
                            reason=SellReason.DAY_TRADING_HIGH_SELL,
                            sell_ratio=(
                                float(day_trading_signal.quantity or 0) / max(int(position.available), 1)
                                if int(position.available) > 0
                                else 0.0
                            ),
                            current_price=current_price,
                            stop_price=current_price,
                        )
                elif day_trading_signal.action == "LOW_BUY":
                    item["signal_type"] = "day_trading"
                    item["severity"] = "info"
                    item["signal_summary"] = f"盘中做T低吸建议: {day_trading_signal.reason}"
                    item_review_tags.extend(["day_trading", "intraday", "low_buy"])
                    if signal is None:
                        item["exit_reason"] = "intraday_t_low_buy"
                        item["status"] = "t_signal"
                        day_trading_rebuy_tickets.append(
                            {
                                "symbol": symbol,
                                "name": name,
                                "source": "day_trading_rebuy",
                                "source_tags": ["day_trading_rebuy", "intraday", "low_buy"],
                                "source_role": "scheduler.position_watch",
                                "market_logic": f"{symbol} 持仓出现盘中低吸回补窗口，原因: {day_trading_signal.reason}",
                                "core_evidence": [
                                    f"VWAP 偏离触发低吸: {day_trading_signal.reason}",
                                    f"建议回补数量={int(day_trading_signal.quantity or 0)}",
                                    f"盘中跌幅={float(intraday_metrics.get('intraday_change_pct', 0.0) or 0.0):.2%}",
                                ],
                                "risk_points": [
                                    "低吸仅用于做T回补，不应替代独立开仓决策",
                                    "若分时继续走弱，应维持等待而非追单",
                                ],
                                "why_now": f"做T低吸窗口出现，建议价格 {float(day_trading_signal.price or current_price):.3f}",
                                "trigger_type": "day_trading_rebuy",
                                "trigger_time": now.isoformat(),
                                "recommended_action": "进入盘中快讨论，评估是否做T回补",
                                "evidence_refs": [f"day_trading_rebuy:{trade_date}:{symbol}"],
                                "submitted_by": "scheduler.position_watch",
                                "selection_score": round(
                                    max(abs(float(intraday_metrics.get('intraday_change_pct', 0.0) or 0.0)) * 100, 1.0) + 2.0,
                                    4,
                                ),
                                "action": "WATCH",
                                "resolved_sector": sector_name,
                                "sector_tags": [sector_name] if sector_name else [],
                            }
                        )

        if signal is None:
            if item_review_tags:
                item["review_tags"] = list(dict.fromkeys(item_review_tags))
            items.append(item)
            continue
        sell_signal_count += 1
        sell_quantity = _normalize_sell_quantity(int(position.available), float(signal.sell_ratio))
        item.update(
            {
                "status": "signal",
                "signal_type": "sell",
                "severity": "critical" if float(signal.sell_ratio) >= 0.999 else "warning",
                "signal_summary": f"{signal.reason.value} 卖出信号",
                "exit_reason": signal.reason.value,
                "sell_ratio": signal.sell_ratio,
                "stop_price": signal.stop_price,
                "planned_quantity": sell_quantity,
            }
        )
        if day_trading_signal is not None and day_trading_signal.action != "HOLD":
            item["signal_type"] = "sell_and_day_trading"
        if signal.reason.value not in item_review_tags:
            item_review_tags.append(signal.reason.value)
        if item_review_tags:
            item["review_tags"] = list(dict.fromkeys(item_review_tags))
        if resolved_mode != "tail":
            _ensure_order_context_loaded()
            latest_buy = latest_buy_by_symbol.get(symbol, latest_buy)
            latest_order = latest_journal_by_symbol.get(symbol, latest_order)
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
        signal_started_at = time.perf_counter()
        decision_id = latest_buy.get("decision_id") or latest_order.get("decision_id")
        if not decision_id:
            _ensure_case_map_loaded()
        if not decision_id and symbol in case_map_by_symbol:
            decision_id = case_map_by_symbol[symbol].case_id
        order_plan: OrderExecutionPlan | None = None
        if order_strategy_resolver is not None:
            order_plan = order_strategy_resolver.resolve(
                side="SELL",
                quote=quote,
                scenario="urgent_exit",
                signal_price=current_price,
            )
        request = PlaceOrderRequest(
            account_id=account_id,
            symbol=symbol,
            side="SELL",
            quantity=sell_quantity,
            price=float(
                (order_plan.price if order_plan is not None else 0.0)
                or quote.bid_price
                or quote.last_price
                or position.last_price
                or entry_price
            ),
            request_id=f"{resolved_mode}-sell-{symbol.replace('.', '-')}-{uuid4().hex[:8]}",
            decision_id=decision_id,
            trade_date=trade_date,
            playbook=playbook,
            regime=regime,
            exit_reason=signal.reason.value,
            trace_id=f"trace-{resolved_mode}-sell-{symbol.replace('.', '-')}-{uuid4().hex[:10]}",
            signal_price=current_price,
            signal_time=now.isoformat(),
            order_type=(order_plan.order_type if order_plan is not None else "opponent_best"),
            time_in_force=(order_plan.time_in_force if order_plan is not None else "day"),
            urgency_tag=(order_plan.urgency_tag if order_plan is not None else "immediate"),
        )
        item["order_execution_plan"] = order_plan.to_payload() if order_plan is not None else None
        _append_scheduler_trade_trace(
            trace_service,
            trace_id=request.trace_id,
            stage=f"scheduler_{resolved_mode}_sell_signal",
            trade_date=trade_date,
            payload={
                "symbol": symbol,
                "signal_reason": signal.reason.value,
                "planned_quantity": sell_quantity,
                "execution_plan": dict(item.get("order_execution_plan") or {}),
                "regime_transition_guard": dict(item.get("regime_transition_guard") or {}),
            },
        )
        item["intraday_signal"] = _build_intraday_signal_snapshot(
            mode=resolved_mode,
            symbol=symbol,
            signal_reason=signal.reason.value,
            signal_type="sell",
            current_price=current_price,
            payload={
                "sell_ratio": signal.sell_ratio,
                "stop_price": signal.stop_price,
                "planned_quantity": sell_quantity,
                "holding_days": holding_days,
                "holding_minutes": holding_minutes,
                "relative_strength_5m": relative_strength,
                "intraday_change_pct": intraday_metrics.get("intraday_change_pct"),
                "intraday_drawdown_pct": intraday_metrics.get("intraday_drawdown_pct"),
            },
        )
        fast_track_review = _fast_track_exit_review(
            discussion_cycle_service,
            trade_date=trade_date,
            symbol=symbol,
            signal_reason=signal.reason.value,
            signal_payload={
                "mode": resolved_mode,
                "current_price": current_price,
                "planned_quantity": sell_quantity,
                "holding_days": holding_days,
                "holding_minutes": holding_minutes,
                "playbook": playbook,
                "regime": regime,
                "pnl_pct": round((current_price - entry_price) / max(entry_price, 1e-9), 6) if entry_price > 0 else 0.0,
            },
        )
        item["fast_track_review"] = fast_track_review
        if not bool(fast_track_review.get("approved", True)):
            preview_count += 1
            item["status"] = "blocked_by_fast_track_review"
            item["preview_reason"] = str(fast_track_review.get("reason") or "fast_track_review_blocked")
            item["request"] = request.model_dump()
            items.append(item)
            continue
        exit_context_snapshot = ctx.model_dump() if ctx is not None else {}
        tail_market_intent = _build_tail_market_gateway_intent(
            request,
            name=name,
            decision_id=decision_id,
            signal_reason=signal.reason.value,
            playbook=playbook,
            regime=regime,
            resolved_sector=sector_name,
            review_tags=list(item.get("review_tags") or []),
            exit_context_snapshot=exit_context_snapshot,
        )
        tail_market_intent["discussion_context"]["trigger_source"] = (
            "tail_market_scan" if resolved_mode == "tail" else "position_watch"
        )
        tail_market_intent["discussion_context"]["intraday_signal"] = dict(item.get("intraday_signal") or {})
        tail_market_intent["discussion_context"]["fast_track_review"] = dict(fast_track_review or {})
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
                approval_source=f"{resolved_mode}_position_watch",
                summary_lines=[
                    (
                        "尾盘卖出执行意图已批准，等待 Windows Execution Gateway 拉取。"
                        if resolved_mode == "tail"
                        else "盘中持仓巡视卖出意图已批准，等待 Windows Execution Gateway 拉取。"
                    )
                ],
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
            _record_scheduler_sell_submission(
                quality_tracker,
                request=request,
                quote=quote,
                submit_time=str(queued_packet.get("approved_at") or now.isoformat()),
                status="queued_for_gateway",
                metadata={"source": f"scheduler_{resolved_mode}_position_watch"},
            )
            _append_scheduler_trade_trace(
                trace_service,
                trace_id=request.trace_id,
                stage=f"scheduler_{resolved_mode}_sell_queued",
                trade_date=trade_date,
                payload={
                    "symbol": symbol,
                    "intent_id": str(queued_packet.get("intent_id") or ""),
                    "gateway_pull_path": EXECUTION_GATEWAY_PENDING_PATH,
                },
            )
            sample = record_latency_sample(
                position_watch_state_store or meeting_state_store,
                chain="sell_signal_to_order_submit",
                stage=f"{resolved_mode}_queue_for_gateway",
                elapsed_ms=(time.perf_counter() - signal_started_at) * 1000.0,
                threshold_ms=5000.0,
                trade_date=trade_date,
                metadata={"symbol": symbol, "mode": resolved_mode, "execution_plane": execution_plane},
            )
            if sample.get("status") == "alert":
                latency_alerts.append(sample)
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
            _ensure_case_map_loaded()
            if not decision_id and symbol in case_map_by_symbol:
                decision_id = case_map_by_symbol[symbol].case_id
                request.decision_id = decision_id
            order = execution_adapter.place_order(request)
            submitted_at = now.isoformat()
            extra_metadata = {}
            if ctx is not None:
                extra_metadata = {
                    "exit_context_snapshot": ctx.model_dump(),
                    "review_tags": list(item.get("review_tags") or []),
                    "position_watch_mode": resolved_mode,
                }
            extra_metadata["intraday_signal"] = dict(item.get("intraday_signal") or {})
            extra_metadata["fast_track_review"] = dict(fast_track_review or {})
            _append_scheduler_order_journal(
                meeting_state_store,
                request,
                order_id=order.order_id,
                name=name,
                submitted_at=submitted_at,
                source=f"scheduler_{resolved_mode}_position_watch",
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
            _record_scheduler_sell_submission(
                quality_tracker,
                request=request,
                quote=quote,
                submit_time=submitted_at,
                status="submitted",
                order_id=str(order.order_id or ""),
                metadata={"source": f"scheduler_{resolved_mode}_position_watch"},
            )
            _append_scheduler_trade_trace(
                trace_service,
                trace_id=request.trace_id,
                stage=f"scheduler_{resolved_mode}_sell_submitted",
                trade_date=trade_date,
                payload={
                    "symbol": symbol,
                    "order_id": str(order.order_id or ""),
                    "price": float(order.price or request.price or 0.0),
                    "quantity": int(order.quantity or request.quantity or 0),
                },
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
            sample = record_latency_sample(
                position_watch_state_store or meeting_state_store,
                chain="sell_signal_to_order_submit",
                stage=f"{resolved_mode}_submit_order",
                elapsed_ms=(time.perf_counter() - signal_started_at) * 1000.0,
                threshold_ms=5000.0,
                trade_date=trade_date,
                metadata={"symbol": symbol, "mode": resolved_mode, "execution_plane": execution_plane},
            )
            if sample.get("status") == "alert":
                latency_alerts.append(sample)
        except Exception as exc:
            error_count += 1
            item["status"] = "error"
            item["error"] = str(exc)
            _append_scheduler_trade_trace(
                trace_service,
                trace_id=request.trace_id,
                stage=f"scheduler_{resolved_mode}_sell_failed",
                trade_date=trade_date,
                payload={"symbol": symbol, "error": str(exc)},
            )
        items.append(item)

    rebuy_injection = {
        "ok": False,
        "ticket_count": 0,
        "case_count": 0,
        "symbols": [],
    }
    if day_trading_rebuy_tickets and candidate_case_service:
        injected_cases = candidate_case_service.upsert_candidate_tickets(trade_date, day_trading_rebuy_tickets)
        rebuy_injection = {
            "ok": True,
            "ticket_count": len(day_trading_rebuy_tickets),
            "case_count": len(injected_cases),
            "symbols": [item.symbol for item in injected_cases],
        }

    if error_count and not submitted_count and not queued_count:
        status = "error"
    elif queued_count and not submitted_count:
        status = "queued_for_gateway"
    else:
        status = "ok"
    summary_lines = [
        (
            f"{'尾盘卖出扫描' if resolved_mode == 'tail' else '盘中持仓巡视'}完成: "
            f"positions={len(positions)} sell_signals={sell_signal_count} "
            f"t_signals={day_trading_signal_count} submitted={submitted_count} "
            f"queued={queued_count} preview={preview_count} errors={error_count}."
        )
    ]
    if day_trading_signal_count > 0:
        summary_lines.append(
            "做T信号: "
            + "；".join(
                f"{item.get('symbol')} {item.get('t_signal_action')}"
                for item in items
                if str(item.get("t_signal_action") or "").strip().upper() not in {"", "HOLD"}
            )
        )
    if rebuy_injection.get("ticket_count", 0) > 0:
        summary_lines.append(
            f"做T低吸回补已注入快讨论: tickets={rebuy_injection['ticket_count']} cases={rebuy_injection['case_count']}。"
        )
    if bool(regime_guard.get("active")):
        summary_lines.extend(list(regime_guard.get("summary_lines") or [])[:2])
    intraday_rank_result = {}
    if resolved_mode == "tail":
        intraday_rank_result = _call_intraday_ranker(
            settings=settings,
            runtime_context=runtime_context,
            event_context=event_context,
            candidate_symbols=[item.symbol for item in positions],
            now_factory=lambda: now,
        )
        _persist_intraday_rank_result(meeting_state_store, runtime_state_store, intraday_rank_result)
        summary_lines.extend(list(intraday_rank_result.get("summary_lines") or [])[:2])
    if queue_for_gateway:
        if queued_count > 0:
            summary_lines.append(
                "当前为 windows_gateway 执行平面，"
                + ("尾盘卖出" if resolved_mode == "tail" else "盘中卖出")
                + "已生成 queued intent，等待 Windows Execution Gateway 拉取。"
            )
        elif preview_count > 0:
            summary_lines.append(
                "当前为 windows_gateway 执行平面，但未能写入 gateway state，"
                + ("尾盘卖出" if resolved_mode == "tail" else "盘中卖出")
                + "仅保留预演信号。"
            )
    elif not submit_orders:
        summary_lines.append(
            "当前为 "
            + f"{settings.run_mode} 模式，"
            + ("自动卖出仅预演不报单。" if allow_live_sell_submit else "盘中只给建议，不自动卖出。")
        )
    payload = {
        "status": status,
        "mode": resolved_mode,
        "account_id": account_id,
        "trade_date": trade_date,
        "scanned_at": now.isoformat(),
        "execution_plane": execution_plane,
        "position_count": len(positions),
        "sell_signal_count": sell_signal_count,
        "day_trading_signal_count": day_trading_signal_count,
        "signal_count": sell_signal_count + day_trading_signal_count,
        "submitted_count": submitted_count,
        "queued_count": queued_count,
        "preview_count": preview_count,
        "error_count": error_count,
        "market_regime": market_profile.get("regime") or "unknown",
        "regime_transition_guard": regime_guard,
        "summary_lines": summary_lines,
        "items": items,
        "action_suggestions": _build_position_watch_action_suggestions(items),
        "day_trading_rebuy_injection": rebuy_injection,
    }
    cycle_sample = record_latency_sample(
        position_watch_state_store or meeting_state_store,
        chain="position_watch_cycle",
        stage=f"{resolved_mode}_cycle",
        elapsed_ms=(time.perf_counter() - function_started_at) * 1000.0,
        threshold_ms=3000.0,
        trade_date=trade_date,
        metadata={"mode": resolved_mode, "position_count": len(positions)},
    )
    payload["latency_tracker"] = get_latency_tracker_snapshot(position_watch_state_store or meeting_state_store)
    if cycle_sample.get("status") == "alert":
        latency_alerts.append(cycle_sample)
    if latency_alerts:
        payload["latency_alerts"] = latency_alerts
        payload["summary_lines"].append(f"延迟告警 {len(latency_alerts)} 条，需排查盘中执行链。")
    if intraday_rank_result:
        payload["intraday_rank_result"] = intraday_rank_result
    if queue_for_gateway:
        payload["gateway_pull_path"] = EXECUTION_GATEWAY_PENDING_PATH
    _persist_position_watch_scan(position_watch_state_store or meeting_state_store, payload)
    if resolved_mode == "tail":
        _persist_tail_market_scan(meeting_state_store, payload)
    should_persist_monitor_snapshot = bool(monitor_state_service)
    if should_persist_monitor_snapshot and resolved_mode == "intraday" and payload.get("signal_count", 0) <= 0:
        latest_watch_snapshot = monitor_state_service.get_latest_position_watch_snapshot()
        age_seconds = _seconds_since(str(latest_watch_snapshot.get("generated_at") or ""))
        should_persist_monitor_snapshot = age_seconds is None or age_seconds >= 30.0
    if should_persist_monitor_snapshot and monitor_state_service:
        monitor_state_service.save_exit_snapshot(
            _build_position_watch_exit_snapshot(payload, now),
            trigger=f"position_watch_{resolved_mode}",
        )
        monitor_state_service.save_position_watch_snapshot(payload, trigger=f"position_watch_{resolved_mode}")
    return payload


def run_tail_market_scan(
    *,
    settings: AppSettings,
    market: MarketDataAdapter,
    execution_adapter: ExecutionAdapter,
    meeting_state_store: StateStore | None,
    runtime_state_store: StateStore | None,
    execution_gateway_state_store: StateStore | None = None,
    position_watch_state_store: StateStore | None = None,
    monitor_state_service=None,
    candidate_case_service=None,
    discussion_cycle_service=None,
    dispatcher=None,
    order_strategy_resolver: OrderStrategyResolver | None = None,
    quality_tracker: ExecutionQualityTracker | None = None,
    trace_service: TradeTraceService | None = None,
    runtime_context: dict | None = None,
    discussion_context: dict | None = None,
    event_context: dict | None = None,
    execution_plane: str | None = None,
    account_id: str | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> dict:
    return run_position_watch_scan(
        settings=settings,
        market=market,
        execution_adapter=execution_adapter,
        meeting_state_store=meeting_state_store,
        runtime_state_store=runtime_state_store,
        execution_gateway_state_store=execution_gateway_state_store,
        position_watch_state_store=position_watch_state_store,
        monitor_state_service=monitor_state_service,
        candidate_case_service=candidate_case_service,
        discussion_cycle_service=discussion_cycle_service,
        dispatcher=dispatcher,
        order_strategy_resolver=order_strategy_resolver,
        quality_tracker=quality_tracker,
        trace_service=trace_service,
        runtime_context=runtime_context,
        discussion_context=discussion_context,
        event_context=event_context,
        execution_plane=execution_plane,
        account_id=account_id,
        mode="tail",
        include_day_trading=True,
        allow_live_sell_submit=True,
        now_factory=now_factory,
    )


def build_postclose_review_board_summary(
    *,
    inspection_payload: dict | None = None,
    tail_market_payload: dict | None = None,
    discussion_context: dict | None = None,
    playbook_override_snapshot: dict | None = None,
) -> dict:
    inspection_payload = inspection_payload or {}
    tail_market_payload = tail_market_payload or {}
    discussion_context = discussion_context or {}
    playbook_override_snapshot = playbook_override_snapshot or {}
    client_brief = dict(discussion_context.get("client_brief") or {})
    finalize_packet = dict(discussion_context.get("finalize_packet") or {})
    execution_precheck = dict(
        finalize_packet.get("execution_precheck")
        or client_brief.get("execution_precheck")
        or {}
    )
    active_overrides = list(playbook_override_snapshot.get("overrides") or [])
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
            f"tail-market 命中 {len(matched_tail_items)} 项，discussion 状态 {discussion_status}，"
            f"override {len(active_overrides)} 项。"
        )
    ]
    if inspection_payload.get("summary_lines"):
        summary_lines.append("治理: " + str((inspection_payload.get("summary_lines") or [""])[0]))
    if playbook_override_snapshot.get("summary_lines"):
        summary_lines.append("治理 override: " + str((playbook_override_snapshot.get("summary_lines") or [""])[0]))
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
            "playbook_override_count": len(active_overrides),
            "tail_market_count": len(matched_tail_items),
            "discussion_blocked_count": int(execution_precheck.get("blocked_count", 0) or 0),
        },
        "playbook_override_snapshot": playbook_override_snapshot,
        "summary_lines": summary_lines,
    }


@dataclass
class Scheduler:
    tasks: list[ScheduledTask] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tasks = ALL_TASKS

    def list_tasks(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "cron": t.cron,
                "interval_seconds": t.interval_seconds,
                "enabled": t.enabled,
            }
            for t in self.tasks
        ]


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
        from apscheduler.triggers.interval import IntervalTrigger
        sched = BlockingScheduler(timezone="Asia/Shanghai")
        for task in ALL_TASKS:
            if not task.enabled:
                continue
            fn = self._task_registry.get(task.handler)
            if fn is None:
                fn = self._make_stub(task.name, task.handler)
            if task.interval_seconds is not None:
                trigger = IntervalTrigger(seconds=max(int(task.interval_seconds or 1), 1), timezone="Asia/Shanghai")
                job_id = f"{task.handler}:interval:{task.interval_seconds}"
                sched.add_job(
                    fn,
                    trigger=trigger,
                    id=job_id,
                    name=task.name,
                    misfire_grace_time=max(int(task.interval_seconds or 1), 1),
                    coalesce=True,
                    max_instances=1,
                )
                logger.info("注册任务: [every %ss] %s", task.interval_seconds, task.name)
                continue
            parts = task.cron.split()
            trigger = CronTrigger(
                minute=parts[0], hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=_normalize_crontab_day_of_week(parts[4]),
                timezone="Asia/Shanghai",
            )
            job_id = f"{task.handler}:{task.cron}"
            sched.add_job(
                fn,
                trigger=trigger,
                id=job_id,
                name=task.name,
                misfire_grace_time=60,
                coalesce=True,
                max_instances=1,
            )
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


def _should_supervision_followthrough_execution_chain(
    *,
    trade_date: str | None,
    session_open: bool,
    cycle: Any,
    execution_dispatch: dict[str, Any] | None,
) -> bool:
    if not trade_date or not session_open or cycle is None:
        return False
    discussion_state = str(getattr(cycle, "discussion_state", "") or "").strip()
    if discussion_state not in {
        "round_summarized",
        "final_review_ready",
        "final_selection_ready",
        "final_selection_blocked",
        "finalized",
    }:
        return False
    execution_pool_case_ids = list(getattr(cycle, "execution_pool_case_ids", []) or [])
    if not execution_pool_case_ids:
        return False
    if isinstance(execution_dispatch, dict) and execution_dispatch:
        dispatch_trade_date = str(execution_dispatch.get("trade_date") or "").strip()
        if not dispatch_trade_date or dispatch_trade_date == trade_date:
            return False
    return True


def run_scheduler(dry_run: bool = False) -> None:
    """CLI 入口: 启动调度器，自动注册所有业务处理函数"""
    from .container import (
        get_audit_store,
        get_agent_score_service,
        get_candidate_case_service,
        get_discussion_state_store,
        get_discussion_cycle_service,
        get_execution_adapter,
        get_execution_gateway_state_store,
        get_learned_asset_service,
        get_market_adapter,
        get_message_dispatcher,
        get_meeting_state_store,
        get_monitor_state_service,
        get_position_watch_state_store,
        get_parameter_service,
        get_research_state_store,
        get_runtime_config_manager,
        get_runtime_state_store,
        get_settings,
    )
    from .data.auction_fetcher import AuctionFetcher
    from .data.event_bus import EventBus
    from .data.serving import ServingStore
    from .data.fetcher import DataFetcher, DataPipeline
    from .data.special import SpecialDataFetcher
    from .execution.bridge_guardian import check as run_bridge_guardian_check
    from .execution.reconciliation import run as run_execution_bridge_reconciliation
    from .execution.stale_order import cleanup as run_stale_order_cleanup
    from .learning.auto_governance import AutoGovernance
    from .learning.continuous import ContinuousLearner
    from .learning.prompt_patcher import PromptPatcher
    from .learning.registry_updater import RegistryUpdater
    from .learning.self_evolve import SelfEvolver
    from .pending_order_remediation import PendingOrderRemediationService
    from .pending_order_inspection import PendingOrderInspectionService
    from .sentiment.calculator import SentimentCalculator
    from .monitor.alert_engine import AlertEngine
    from .monitor.stock_pool import StockPoolManager
    from .report.daily import DailyReporter
    from .risk.stress_test import StressTestService
    from .strategy.auction_engine import AuctionEngine
    from .strategy.factor_registry import bootstrap_factor_registry, factor_registry
    from .strategy.factor_monitor import FactorMonitor
    from .strategy.nightly_sandbox import NightlySandbox
    from .strategy.playbook_registry import bootstrap_playbook_registry, playbook_registry

    settings = get_settings()
    migrate_legacy_state_files(settings.storage_root)
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
    discussion_state_store = get_discussion_state_store()
    execution_gateway_state_store = get_execution_gateway_state_store()
    position_watch_state_store = get_position_watch_state_store()
    serving_store = ServingStore(settings.storage_root)
    archive_store = DataArchiveStore(settings.storage_root)
    control_plane_db = ControlPlaneDB(settings.control_plane_db_path)
    history_catalog = CatalogService(control_plane_db)
    history_store = HistoryStore(settings.storage_root, control_plane_db, history_catalog)
    config_mgr = get_runtime_config_manager()
    parameter_service = get_parameter_service()
    learned_asset_service = get_learned_asset_service()
    trade_attribution_service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
    candidate_case_service = get_candidate_case_service()
    discussion_cycle_service = get_discussion_cycle_service()
    agent_score_service = get_agent_score_service()
    monitor_state_service = get_monitor_state_service()
    execution_adapter = get_execution_adapter()
    repo_root = Path(__file__).resolve().parents[2]
    prompt_patcher = PromptPatcher(repo_root / "openclaw" / "prompts")
    registry_updater = RegistryUpdater(repo_root / "openclaw" / "team_registry.final.json")
    from .strategy.evaluation_ledger import EvaluationLedgerService
    bootstrap_factor_registry()
    bootstrap_playbook_registry()
    auto_governance = AutoGovernance()
    self_evolver = SelfEvolver()
    continuous_learner = ContinuousLearner()
    nightly_sandbox = NightlySandbox(
        settings.storage_root,
        replay_packet_builder=lambda symbol: discussion_cycle_service.build_openclaw_replay_proposal_packet(
            [],
            trade_date=_resolve_trade_date_from_contexts(),
            expected_case_ids=[],
        ),
    )
    strategy_router = StrategyRouter()
    leader_ranker = LeaderRanker()
    evaluation_ledger_service = EvaluationLedgerService(
        state_store=runtime_state,
        audit_store=audit_store,
        meeting_state_store=meeting_state_store,
        candidate_case_service=candidate_case_service,
        trade_attribution_service=trade_attribution_service,
        agent_score_service=agent_score_service,
        nightly_sandbox=nightly_sandbox,
        learned_asset_service=learned_asset_service,
        registry_updater=registry_updater,
    )
    factor_monitor = FactorMonitor(
        registry=factor_registry,
        market_adapter=market,
        state_store=runtime_state,
    )
    stress_test_service = StressTestService(settings.storage_root / "risk" / "latest_stress_test.json")
    event_bus = EventBus()
    auction_gateway_url = (
        str(getattr(settings.service, "public_base_url", "") or "").strip()
        or f"http://{settings.service.host}:{settings.service.port}"
    )
    auction_fetcher = AuctionFetcher(gateway_url=auction_gateway_url)
    auction_engine = AuctionEngine()

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
    history_ingest_service = HistoryIngestService(
        market_adapter=market,
        history_store=history_store,
        catalog_service=history_catalog,
        research_state_store=research_state,
        audit_store=audit_store,
    )
    freshness_monitor = DataFreshnessMonitor(market)
    reverse_repo_service = ReverseRepoService(
        settings=settings,
        execution_adapter=execution_adapter,
        market_adapter=market,
        state_store=runtime_state,
        config_mgr=config_mgr,
        parameter_service=parameter_service,
        dispatcher=dispatcher,
    )
    order_strategy_resolver = OrderStrategyResolver()
    quality_tracker = ExecutionQualityTracker(settings.storage_root / "execution" / "quality_tracker.json")
    trace_service = TradeTraceService(settings.storage_root / "infra" / "trade_traces.json")
    live_drift_tracker = LiveBacktestDriftTracker(settings.storage_root / "backtest" / "live_drift.json")
    circuit_breaker = CircuitBreakerRegistry(settings.storage_root / "infra" / "circuit_breakers.json")

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

    def persist_scheduler_runtime_context(runtime_snapshot: dict) -> dict:
        trade_date = str(runtime_snapshot.get("trade_date") or date.today().isoformat())
        top_picks = list(runtime_snapshot.get("top_picks") or [])
        runtime_context = {
            "available": True,
            "resource": "runtime_context",
            "trade_date": trade_date,
            "generated_at": runtime_snapshot.get("generated_at") or datetime.now().isoformat(),
            "job_id": runtime_snapshot.get("job_id"),
            "job_type": runtime_snapshot.get("job_type"),
            "run_mode": settings.run_mode,
            "market_mode": getattr(market, "mode", settings.market_mode),
            "execution_mode": getattr(execution_adapter, "mode", settings.execution_mode),
            "account_id": runtime_snapshot.get("account_id"),
            "auto_trade": runtime_snapshot.get("auto_trade", False),
            "universe_scope": runtime_snapshot.get("universe_scope", "main-board"),
            "decision_count": len(top_picks),
            "buy_count": sum(1 for item in top_picks if str(item.get("action") or "").upper() == "BUY"),
            "hold_count": sum(1 for item in top_picks if str(item.get("action") or "").upper() == "HOLD"),
            "selected_symbols": [str(item.get("symbol") or "") for item in top_picks if str(item.get("symbol") or "").strip()],
            "top_picks": top_picks,
            "selection_preferences": runtime_snapshot.get("selection_preferences", {}),
            "summary": runtime_snapshot.get("summary", {}),
            "execution": runtime_snapshot.get("execution"),
            "report_path": runtime_snapshot.get("report_path"),
            "market_profile": runtime_snapshot.get("market_profile", {}),
            "sector_profiles": runtime_snapshot.get("sector_profiles", []),
            "playbook_contexts": runtime_snapshot.get("playbook_contexts", []),
            "playbook_count": len(list(runtime_snapshot.get("playbook_contexts") or [])),
            "behavior_profiles": runtime_snapshot.get("behavior_profiles", []),
            "hot_sectors": runtime_snapshot.get("hot_sectors", []),
            "pool": {
                "date": trade_date,
                "source": str(runtime_snapshot.get("job_type") or "scheduler_pipeline"),
                "symbols": [str(item.get("symbol") or "") for item in top_picks if str(item.get("symbol") or "").strip()],
                "names": {
                    str(item.get("symbol") or ""): str(item.get("name") or item.get("symbol"))
                    for item in top_picks
                    if str(item.get("symbol") or "").strip()
                },
                "scores": {
                    str(item.get("symbol") or ""): float(item.get("selection_score", 0.0) or 0.0)
                    for item in top_picks
                    if str(item.get("symbol") or "").strip()
                },
            },
        }
        archive_store.persist_runtime_context(trade_date, runtime_context)
        runtime_state.set("latest_runtime_context", runtime_context)
        return runtime_context

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

    def _resolve_trade_date_from_contexts() -> str:
        for payload in (
            serving_store.get_latest_workspace_context(),
            serving_store.get_latest_dossier_pack(),
            serving_store.get_latest_runtime_context(),
            discussion_state_store.get("latest_discussion_context", {}) if discussion_state_store else {},
            serving_store.get_latest_discussion_context(),
        ):
            resolved = str((payload or {}).get("trade_date") or "").strip()
            if resolved:
                return resolved
        if candidate_case_service:
            latest_cases = candidate_case_service.list_cases(limit=1)
            if latest_cases:
                return str(latest_cases[0].trade_date or "").strip() or date.today().isoformat()
        return date.today().isoformat()

    def _chunked_symbols(items: list[str], chunk_size: int) -> list[list[str]]:
        resolved_chunk_size = max(int(chunk_size or 1), 1)
        return [items[index : index + resolved_chunk_size] for index in range(0, len(items), resolved_chunk_size)]

    def _resolve_history_daily_symbols(limit: int | None = None) -> list[str]:
        candidate_symbols = _resolve_candidate_symbols(limit=60)
        if candidate_symbols:
            return candidate_symbols
        universe = list(market.get_main_board_universe())
        if limit is None or limit <= 0:
            return universe
        return universe[:limit]

    def _resolve_history_minute_symbols(limit: int = 60) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()

        def _push(items: list[str]) -> None:
            for symbol in items:
                normalized = str(symbol or "").strip().upper()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                resolved.append(normalized)
                if len(resolved) >= limit:
                    return

        try:
            account_id = _resolve_scheduler_account_id(settings, execution_adapter)
            positions = execution_adapter.get_positions(account_id)
            _push([item.symbol for item in positions if getattr(item, "symbol", "")])
        except Exception:
            pass

        _push(_resolve_candidate_symbols(limit=limit))

        latest_runtime_context = (
            serving_store.get_latest_runtime_context()
            or runtime_state.get("latest_runtime_context", {})
            or {}
        )
        _push([str(item.get("symbol") or "") for item in list(latest_runtime_context.get("top_picks") or []) if isinstance(item, dict)])

        latest_fast_opportunity = (
            dict(position_watch_state_store.get("latest_fast_opportunity_scan", {}) or {})
            if position_watch_state_store
            else {}
        )
        _push(
            [
                str(item.get("symbol") or "")
                for item in list(latest_fast_opportunity.get("items") or [])
                if isinstance(item, dict)
            ]
        )

        latest_precheck = dict(meeting_state_store.get("latest_execution_precheck", {}) or {}) if meeting_state_store else {}
        _push(
            [
                str(item.get("symbol") or "")
                for item in list(latest_precheck.get("items") or [])
                if isinstance(item, dict)
            ]
        )

        if len(resolved) < limit:
            _push(list(market.get_main_board_universe())[:limit])
        return resolved[:limit]

    def _load_latest_discussion_context() -> dict[str, Any]:
        if discussion_state_store:
            payload = dict(discussion_state_store.get("latest_discussion_context", {}) or {})
            if payload:
                return payload
        return dict(serving_store.get_latest_discussion_context() or {})

    def _load_discussion_context_for_trade_date(trade_date: str) -> dict[str, Any]:
        if discussion_state_store:
            payload = dict(discussion_state_store.get(f"discussion_context:{trade_date}", {}) or {})
            if payload:
                return payload
        latest = _load_latest_discussion_context()
        if str(latest.get("trade_date") or "").strip() == trade_date:
            return latest
        return {}

    def _prune_discussion_context(keep_days: int = 3) -> dict[str, Any]:
        if not discussion_state_store:
            return {"ok": False, "reason": "discussion_state_store_unavailable", "pruned_keys": []}
        archive_dir = settings.storage_root / "archive" / "discussion_context"
        archive_dir.mkdir(parents=True, exist_ok=True)
        pruned_keys: list[str] = []
        archived_dates: list[str] = []
        with _file_lock(discussion_state_store.storage_path):
            payload = _load_json_payload(discussion_state_store.storage_path, {})
            if not isinstance(payload, dict):
                payload = {}
            dated_items = [
                (key, key.split(":", 1)[1])
                for key in payload.keys()
                if isinstance(key, str) and key.startswith("discussion_context:")
            ]
            dated_items.sort(key=lambda item: item[1], reverse=True)
            keep_dates = {trade_date for _, trade_date in dated_items[: max(int(keep_days or 3), 1)]}
            for key, trade_date in dated_items:
                if trade_date in keep_dates:
                    continue
                archived_payload = payload.pop(key, None)
                if archived_payload is None:
                    continue
                archive_path = archive_dir / f"{trade_date}.json"
                _atomic_write_json(archive_path, archived_payload)
                pruned_keys.append(key)
                archived_dates.append(trade_date)
            _atomic_write_json(discussion_state_store.storage_path, payload)
            discussion_state_store.data = payload
            discussion_state_store._cached_mtime_ns = discussion_state_store._stat_mtime_ns()
        return {
            "ok": True,
            "keep_days": keep_days,
            "pruned_count": len(pruned_keys),
            "pruned_keys": pruned_keys,
            "archived_dates": archived_dates,
            "archive_dir": str(archive_dir),
        }

    def _resolve_internal_control_plane_base_url() -> str:
        host = str(settings.service.host or "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        return f"http://{host}:{settings.service.port}".rstrip("/")

    def _advance_execution_chain_via_control_plane(
        *,
        trigger: str,
        try_finalize: bool = True,
    ) -> dict[str, Any]:
        trade_date = _resolve_trade_date_from_contexts()
        if not trade_date:
            return {"ok": False, "reason": "trade_date_unavailable"}
        control_plane_base_url = _resolve_internal_control_plane_base_url()
        timeout_sec = min(max(_resolve_agent_autonomy_timeout_sec(), 20.0), 60.0)
        with httpx.Client(
            timeout=httpx.Timeout(timeout=timeout_sec, connect=min(10.0, timeout_sec)),
        ) as client:
            cycle_response = client.get(f"{control_plane_base_url}/system/discussions/cycles/{trade_date}")
            cycle_response.raise_for_status()
            cycle_payload = cycle_response.json()
            cycle_available = bool(cycle_payload.get("cycle_id") or cycle_payload.get("available"))
            if not cycle_available:
                bootstrap_response = client.post(
                    f"{control_plane_base_url}/system/discussions/cycles/bootstrap",
                    json={"trade_date": trade_date},
                )
                bootstrap_response.raise_for_status()
                cycle_payload = bootstrap_response.json().get("cycle") or {}

            refreshed_response = client.post(f"{control_plane_base_url}/system/discussions/cycles/{trade_date}/refresh")
            refreshed_response.raise_for_status()
            refreshed_payload = refreshed_response.json()
            result: dict[str, Any] = {
                "ok": bool(refreshed_payload.get("ok", True)),
                "trade_date": trade_date,
                "trigger": trigger,
                "cycle": dict(refreshed_payload.get("cycle") or {}),
                "refresh_skipped": bool(refreshed_payload.get("refresh_skipped")),
                "execution_chain_progress": dict(refreshed_payload.get("execution_chain_progress") or {}),
            }
            cycle_state = str(((result.get("cycle") or {}).get("discussion_state") or "")).strip()
            if try_finalize and cycle_state in {"final_selection_ready", "final_selection_blocked"}:
                finalize_response = client.post(f"{control_plane_base_url}/system/discussions/cycles/{trade_date}/finalize")
                finalize_response.raise_for_status()
                finalize_payload = finalize_response.json()
                result["finalize"] = {
                    "ok": bool(finalize_payload.get("ok", True)),
                    "finalize_skipped": bool(finalize_payload.get("finalize_skipped")),
                    "auto_dispatch_status": str(finalize_payload.get("auto_dispatch_status") or ""),
                    "notification_reason": str((finalize_payload.get("notification") or {}).get("reason") or ""),
                }
            precheck_response = client.get(
                f"{control_plane_base_url}/system/discussions/execution-precheck",
                params={"trade_date": trade_date},
            )
            precheck_response.raise_for_status()
            precheck_payload = precheck_response.json()
            intents_response = client.get(
                f"{control_plane_base_url}/system/discussions/execution-intents",
                params={"trade_date": trade_date},
            )
            intents_response.raise_for_status()
            intents_payload = intents_response.json()
            dispatch_response = client.get(
                f"{control_plane_base_url}/system/discussions/execution-dispatch/latest",
                params={"trade_date": trade_date},
            )
            dispatch_response.raise_for_status()
            dispatch_payload = dispatch_response.json()
            result["execution_precheck"] = {
                "status": precheck_payload.get("status"),
                "approved_count": int(precheck_payload.get("approved_count", 0) or 0),
                "blocked_count": int(precheck_payload.get("blocked_count", 0) or 0),
                "session_open": bool(precheck_payload.get("session_open")),
            }
            result["execution_intents"] = {
                "status": intents_payload.get("status"),
                "intent_count": int(intents_payload.get("intent_count", 0) or 0),
                "blocked_count": int(intents_payload.get("blocked_count", 0) or 0),
            }
            result["execution_dispatch"] = {
                "status": dispatch_payload.get("status"),
                "submitted_count": int(dispatch_payload.get("submitted_count", 0) or 0),
                "queued_count": int(dispatch_payload.get("queued_count", 0) or 0),
                "blocked_count": int(dispatch_payload.get("blocked_count", 0) or 0),
            }
            return result

    def _resolve_candidate_symbols(limit: int = 30) -> list[str]:
        runtime_context = serving_store.get_latest_runtime_context() or runtime_state.get("latest_runtime_context", {}) or {}
        selected_symbols = list(runtime_context.get("selected_symbols") or [])
        if selected_symbols:
            return [str(item) for item in selected_symbols[:limit]]
        trade_date = _resolve_trade_date_from_contexts()
        return [
            item.symbol
            for item in candidate_case_service.list_cases(trade_date=trade_date, limit=limit)
            if getattr(item, "symbol", "")
        ]

    def _load_nightly_priority_boosts(trade_date: str) -> dict[str, float]:
        payload = research_state.get("latest_nightly_sandbox", {}) or {}
        if str(payload.get("trade_date") or "") != trade_date:
            return {}
        priorities = list(payload.get("tomorrow_priorities") or [])
        return {str(symbol): 12.0 for symbol in priorities if str(symbol)}

    def _load_previous_auction_signal_map(trade_date: str) -> dict[str, dict]:
        history = list(research_state.get("auction_signal_history", []) or [])
        for item in reversed(history):
            if str(item.get("trade_date") or "") == trade_date:
                continue
            signal_map: dict[str, dict] = {}
            for signal in list(item.get("signals") or []):
                if isinstance(signal, dict) and signal.get("symbol"):
                    signal_map[str(signal["symbol"])] = signal
            if signal_map:
                return signal_map
        return {}

    def _on_negative_news(event) -> None:
        payload = dict(event.payload or {})
        history = meeting_state_store.get("event_bus_negative_news", []) if meeting_state_store else []
        history.append(
            {
                "symbol": event.symbol,
                "timestamp": event.timestamp or datetime.now().isoformat(),
                "title": payload.get("title", ""),
                "severity": payload.get("severity", ""),
                "summary": payload.get("summary", ""),
            }
        )
        if meeting_state_store:
            meeting_state_store.set("event_bus_negative_news", history[-100:])
        logger.info("事件总线负面新闻: symbol=%s title=%s", event.symbol, payload.get("title", ""))

    def _on_price_alert(event) -> None:
        payload = dict(event.payload or {})
        history = meeting_state_store.get("event_bus_price_alerts", []) if meeting_state_store else []
        history.append(
            {
                "symbol": event.symbol,
                "timestamp": event.timestamp or datetime.now().isoformat(),
                "alert_type": payload.get("alert_type", ""),
                "severity": payload.get("severity", ""),
                "change_pct": payload.get("change_pct", 0.0),
                "message": payload.get("message", ""),
            }
        )
        if meeting_state_store:
            meeting_state_store.set("event_bus_price_alerts", history[-200:])
        logger.info("事件总线价格异动: symbol=%s alert=%s", event.symbol, payload.get("alert_type", ""))

    event_bus.subscribe("NEGATIVE_NEWS", _on_negative_news)
    event_bus.subscribe("PRICE_ALERT", _on_price_alert)

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
            for alert in alerts:
                event_bus.publish_sync(
                    MarketEvent(
                        event_type="PRICE_ALERT",
                        symbol=alert.symbol,
                        payload={
                            "alert_type": alert.alert_type,
                            "message": alert.message,
                            "severity": alert.severity,
                            "price": alert.price,
                            "change_pct": alert.change_pct,
                        },
                        timestamp=datetime.now().isoformat(),
                        priority=2 if alert.severity == "critical" else 1,
                        source="scheduler.check_once",
                    )
                )
            monitor_state_service.record_alert_events(alerts, snaps)
            opportunity_watchlist = _build_intraday_opportunity_watchlist(
                alerts,
                snaps,
                generated_at=datetime.now().isoformat(),
                trade_date=_resolve_trade_date_from_contexts(),
            )
            if meeting_state_store:
                meeting_state_store.set("latest_intraday_opportunity_watchlist", opportunity_watchlist)
            if runtime_state:
                runtime_state.set("latest_intraday_opportunity_watchlist", opportunity_watchlist)
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
            latest_runtime_context = (
                serving_store.get_latest_runtime_context()
                or runtime_state.get("latest_runtime_context", {})
                or {}
            )
            intraday_rank_result = _call_intraday_ranker(
                settings=settings,
                runtime_context=latest_runtime_context,
                event_context=serving_store.get_latest_event_context() or {},
            )
            _persist_intraday_rank_result(meeting_state_store, runtime_state, intraday_rank_result)
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
                    "intraday_rank_result": {
                        "action_count": intraday_rank_result.get("action_count", 0),
                        "freeze_count": intraday_rank_result.get("freeze_count", 0),
                        "upgrade_count": intraday_rank_result.get("upgrade_count", 0),
                        "downgrade_count": intraday_rank_result.get("downgrade_count", 0),
                        "freeze_all": intraday_rank_result.get("freeze_all", False),
                        "summary_lines": list(intraday_rank_result.get("summary_lines") or [])[:3],
                    },
                    "opportunity_watchlist": {
                        "count": opportunity_watchlist.get("count", 0),
                        "summary_lines": list(opportunity_watchlist.get("summary_lines") or [])[:2],
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
            prune_payload = _prune_discussion_context(keep_days=3)
            refresh_idle_dossier("fetch_daily")
            record_audit(
                "data",
                "完成日终数据拉取",
                {
                    "symbol_count": min(len(universe), 100),
                    "discussion_context_prune": prune_payload,
                },
            )
        except Exception as e:
            logger.warning("日终数据拉取失败: %s", e)

    def task_fetch_news():
        universe = fetcher.fetch_universe("main-board")[:20]
        event_fetcher = _load_event_fetcher()
        event_fetch_payload = {
            "trade_date": date.today().isoformat(),
            "generated_at": datetime.now().isoformat(),
            "events": [],
            "summary_lines": ["EventFetcher 未可用，本次仅保留 research_state 同步痕迹。"],
        }
        persisted_payload = _persist_event_fetch_result(
            research_state_store=research_state,
            archive_store=archive_store,
            result=event_fetch_payload,
        )
        if event_fetcher is not None:
            try:
                last_sync = (research_state.get("sync_history", []) or [{}])[-1] if research_state.get("sync_history", []) else {}
                event_fetch_result = event_fetcher.fetch_incremental(
                    universe,
                    trade_date=event_fetch_payload["trade_date"],
                    since=last_sync.get("requested_at"),
                    event_bus=event_bus,
                )
                event_fetch_payload = _as_payload(event_fetch_result)
                persisted_payload = _persist_event_fetch_result(
                    research_state_store=research_state,
                    archive_store=archive_store,
                    result=event_fetch_result,
                )
            except Exception as exc:
                logger.warning("结构化事件抓取失败: %s", exc)
                event_fetch_payload["summary_lines"] = [f"结构化事件抓取失败，已回退为空结果: {exc}"]
                persisted_payload = _persist_event_fetch_result(
                    research_state_store=research_state,
                    archive_store=archive_store,
                    result=event_fetch_payload,
                )
        sync_item = {
            "symbols": universe,
            "requested_at": datetime.now().isoformat(),
            "news_count": len(research_state.get("news", [])),
            "announcement_count": len(research_state.get("announcements", [])),
            "policy_count": len(research_state.get("policy", [])),
            "event_count": persisted_payload.get("event_count", 0),
            "blocked_symbols": persisted_payload.get("blocked_symbols", []),
            "summary_lines": list(event_fetch_payload.get("summary_lines") or []),
        }
        history = research_state.get("sync_history", [])
        history.append(sync_item)
        research_state.set("sync_history", history[-50:])
        refresh_idle_dossier("fetch_news")
        record_audit("research", "完成盘前新闻同步任务", sync_item)

    def task_check_micro():
        task_check_once()
        trade_date = _resolve_trade_date_from_contexts()
        latest_opportunity_payload = (
            dict(position_watch_state_store.get("latest_fast_opportunity_scan", {}) or {})
            if position_watch_state_store
            else {}
        )
        opportunity_injection = _upsert_intraday_candidate_tickets(
            trade_date=trade_date,
            opportunity_payload=latest_opportunity_payload,
            candidate_case_service=candidate_case_service,
            position_watch_state_store=position_watch_state_store,
        )
        record_audit(
            "monitor",
            "完成微观节奏巡检",
            {
                "trade_date": trade_date,
                "event_bus_price_alert_count": len(meeting_state_store.get("event_bus_price_alerts", []) if meeting_state_store else []),
                "opportunity_injection": opportunity_injection,
            },
        )

    def task_position_watch():
        now = datetime.now()
        if not is_trading_session(now):
            return
        payload = run_position_watch_scan(
            settings=settings,
            market=market,
            execution_adapter=execution_adapter,
            meeting_state_store=meeting_state_store,
            runtime_state_store=runtime_state,
            execution_gateway_state_store=execution_gateway_state_store,
            position_watch_state_store=position_watch_state_store,
            monitor_state_service=monitor_state_service,
            candidate_case_service=candidate_case_service,
            discussion_cycle_service=discussion_cycle_service,
            dispatcher=dispatcher,
            order_strategy_resolver=order_strategy_resolver,
            quality_tracker=quality_tracker,
            trace_service=trace_service,
            runtime_context=(
                serving_store.get_latest_runtime_context()
                or runtime_state.get("latest_runtime_context", {})
                or {}
            ),
            discussion_context=_load_latest_discussion_context(),
            event_context=serving_store.get_latest_event_context() or {},
            execution_plane=str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
            mode="intraday",
            include_day_trading=True,
            allow_live_sell_submit=True,
            now_factory=lambda: now,
        )
        record_audit(
            "risk",
            "完成盘中持仓深巡视",
            {
                "mode": payload.get("mode"),
                "position_count": payload.get("position_count", 0),
                "sell_signal_count": payload.get("sell_signal_count", 0),
                "day_trading_signal_count": payload.get("day_trading_signal_count", 0),
                "submitted_count": payload.get("submitted_count", 0),
                "queued_count": payload.get("queued_count", 0),
                "preview_count": payload.get("preview_count", 0),
                "error_count": payload.get("error_count", 0),
                "summary_lines": list(payload.get("summary_lines") or [])[:3],
                "action_suggestions": list(payload.get("action_suggestions") or [])[:5],
            },
        )
        if dispatcher and payload.get("action_suggestions"):
            dispatcher.dispatch_trade(
                "盘中持仓巡视",
                position_watch_notification_template(
                    "盘中持仓巡视",
                    list(payload.get("summary_lines") or []),
                    list(payload.get("action_suggestions") or []),
                ),
                level="warning",
                force=True,
            )

    def task_fast_position_watch():
        now = datetime.now()
        if not is_trading_session(now):
            return
        payload = run_fast_position_watch_scan(
            settings=settings,
            market=market,
            execution_adapter=execution_adapter,
            meeting_state_store=meeting_state_store,
            runtime_state_store=runtime_state,
            execution_gateway_state_store=execution_gateway_state_store,
            position_watch_state_store=position_watch_state_store,
            monitor_state_service=monitor_state_service,
            discussion_cycle_service=discussion_cycle_service,
            dispatcher=dispatcher,
            order_strategy_resolver=order_strategy_resolver,
            quality_tracker=quality_tracker,
            trace_service=trace_service,
            execution_plane=str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
            now_factory=lambda: now,
        )
        opportunity_payload: dict[str, Any] | None = None
        opportunity_injection = {"ok": False, "ticket_count": 0, "case_count": 0, "symbols": []}
        if _should_run_fast_opportunity_scan(position_watch_state_store, now):
            opportunity_started_at = time.perf_counter()
            opportunity_payload = run_fast_opportunity_scan(
                settings=settings,
                market=market,
                runtime_state_store=runtime_state,
                position_watch_state_store=position_watch_state_store,
                now_factory=lambda: now,
            )
        if payload.get("signal_count", 0) > 0 or payload.get("error_count", 0) > 0:
            record_audit(
                "risk",
                "完成盘中持仓快巡视",
                {
                    "mode": payload.get("mode"),
                    "position_count": payload.get("position_count", 0),
                    "signal_count": payload.get("signal_count", 0),
                    "submitted_count": payload.get("submitted_count", 0),
                    "queued_count": payload.get("queued_count", 0),
                    "preview_count": payload.get("preview_count", 0),
                    "error_count": payload.get("error_count", 0),
                    "summary_lines": list(payload.get("summary_lines") or [])[:3],
                },
            )
        if opportunity_payload and opportunity_payload.get("count", 0) > 0:
            opportunity_injection = _upsert_intraday_candidate_tickets(
                trade_date=str(opportunity_payload.get("trade_date") or now.date().isoformat()),
                opportunity_payload=opportunity_payload,
                candidate_case_service=candidate_case_service,
                position_watch_state_store=position_watch_state_store,
            )
            opportunity_latency = record_latency_sample(
                position_watch_state_store,
                chain="opportunity_scan_to_candidate_inject",
                stage="fast_opportunity_scan",
                elapsed_ms=(time.perf_counter() - opportunity_started_at) * 1000.0,
                threshold_ms=2000.0,
                trade_date=str(opportunity_payload.get("trade_date") or now.date().isoformat()),
                metadata={"count": opportunity_payload.get("count", 0), "ticket_count": opportunity_injection.get("ticket_count", 0)},
            )
            payload["latency_tracker"] = get_latency_tracker_snapshot(position_watch_state_store)
            if opportunity_latency.get("status") == "alert":
                payload.setdefault("latency_alerts", []).append(opportunity_latency)
            record_audit(
                "monitor",
                "完成盘中机会快扫",
                {
                    "count": opportunity_payload.get("count", 0),
                    "early_momentum_count": opportunity_payload.get("early_momentum_count", 0),
                    "acceleration_count": opportunity_payload.get("acceleration_count", 0),
                    "pre_limit_up_count": opportunity_payload.get("pre_limit_up_count", 0),
                    "abnormal_drop_count": opportunity_payload.get("abnormal_drop_count", 0),
                    "summary_lines": list(opportunity_payload.get("summary_lines") or [])[:3],
                    "injection_result": opportunity_injection,
                },
            )
        if dispatcher and payload.get("action_suggestions"):
            dispatcher.dispatch_trade(
                "快路径持仓巡视",
                position_watch_notification_template(
                    "快路径持仓巡视",
                    list(payload.get("summary_lines") or []),
                    list(payload.get("action_suggestions") or []),
                ),
                level="warning",
                force=True,
            )
        latency_alerts = list(payload.get("latency_alerts") or [])
        if dispatcher and latency_alerts:
            dispatcher.dispatch_trade(
                "快路径延迟告警",
                latency_alert_notification_template("快路径延迟告警", latency_alerts),
                level="warning",
                force=True,
            )

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

    def task_history_ingest_daily():
        try:
            trade_date = _resolve_trade_date_from_contexts()
            universe_limit = int(os.getenv("ASHARE_HISTORY_DAILY_UNIVERSE_LIMIT", "600") or 600)
            bar_count = int(os.getenv("ASHARE_HISTORY_DAILY_BAR_COUNT", "120") or 120)
            batch_size = int(os.getenv("ASHARE_HISTORY_DAILY_BATCH_SIZE", "200") or 200)
            symbols = _resolve_history_daily_symbols(limit=universe_limit)
            batches = _chunked_symbols(symbols, batch_size)
            batch_results: list[dict[str, Any]] = []
            total_rows = 0
            total_symbols = 0
            for index, batch in enumerate(batches, start=1):
                result = history_ingest_service.ingest_daily_bars(
                    symbols=batch,
                    trade_date=trade_date,
                    count=bar_count,
                    source=f"scheduler_history_daily_batch_{index}",
                )
                batch_results.append(result)
                total_rows += int(result.get("row_count", 0) or 0)
                total_symbols += int(result.get("symbol_count", 0) or 0)
            payload = {
                "trade_date": trade_date,
                "batch_count": len(batch_results),
                "requested_symbol_count": len(symbols),
                "ingested_symbol_count": total_symbols,
                "row_count": total_rows,
                "partition_count": sum(int(item.get("partition_count", 0) or 0) for item in batch_results),
                "bar_count": bar_count,
                "batch_size": batch_size,
                "latest_path": str(batch_results[-1].get("latest_path") if batch_results else ""),
            }
            runtime_state.set("latest_history_daily_ingest", payload)
            record_audit("history", "完成日线入湖任务", payload)
        except Exception as exc:
            logger.warning("日线入湖任务失败: %s", exc)

    def task_history_ingest_minute():
        try:
            trade_date = _resolve_trade_date_from_contexts()
            count = int(os.getenv("ASHARE_HISTORY_MINUTE_COUNT", "240") or 240)
            symbols = _resolve_history_minute_symbols(limit=int(os.getenv("ASHARE_HISTORY_MINUTE_UNIVERSE_LIMIT", "60") or 60))
            result = history_ingest_service.ingest_minute_bars(
                symbols=symbols,
                trade_date=trade_date,
                count=count,
                period="1m",
                source="scheduler_history_minute",
            )
            payload = {
                "trade_date": trade_date,
                "symbol_count": result.get("symbol_count", 0),
                "row_count": result.get("row_count", 0),
                "partition_count": result.get("partition_count", 0),
                "trade_dates": result.get("trade_dates", []),
                "path": result.get("latest_path"),
                "count": count,
                "symbols": symbols[:20],
            }
            runtime_state.set("latest_history_minute_ingest", payload)
            record_audit("history", "完成分钟线入湖任务", payload)
        except Exception as exc:
            logger.warning("分钟线入湖任务失败: %s", exc)

    def task_history_ingest_behavior_profiles():
        try:
            trade_date = _resolve_trade_date_from_contexts()
            result = history_ingest_service.sync_behavior_profiles(trade_date=trade_date)
            payload = {
                "trade_date": trade_date,
                "symbol_count": result.get("symbol_count", 0),
                "row_count": result.get("row_count", 0),
                "source_counts": result.get("source_counts", {}),
            }
            runtime_state.set("latest_history_behavior_profile_ingest", payload)
            record_audit("history", "完成股性画像入库任务", payload)
        except Exception as exc:
            logger.warning("股性画像入库任务失败: %s", exc)

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
        playbook_override_snapshot = _build_playbook_override_snapshot(
            settings=settings,
            report=trade_attribution_service.latest_report(),
        )
        if playbook_override_snapshot:
            playbook_override_snapshot = _persist_playbook_override_snapshot(
                settings,
                meeting_state_store,
                runtime_state,
                playbook_override_snapshot,
            )
        payload["playbook_override_snapshot"] = playbook_override_snapshot
        payload["playbook_override_count"] = len(list(playbook_override_snapshot.get("overrides") or []))
        review_board_summary = build_postclose_review_board_summary(
            inspection_payload=payload,
            tail_market_payload=(
                meeting_state_store.get("latest_tail_market_scan", {})
                if meeting_state_store
                else {}
            ),
            discussion_context=_load_latest_discussion_context(),
            playbook_override_snapshot=playbook_override_snapshot,
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
                "playbook_override_count": payload.get("playbook_override_count", 0),
                "playbook_override_snapshot": playbook_override_snapshot,
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
        breaker_state = circuit_breaker.check("market_data_pipeline")
        if not breaker_state.get("available", True):
            cached = dict(breaker_state.get("cached_result") or runtime_state.get("latest_runtime_report", {}) or {})
            record_audit(
                "runtime",
                "基础样本生成走熔断降级",
                {
                    "breaker": {"subsystem": "market_data_pipeline", "status": breaker_state.get("status")},
                    "cached_job_id": cached.get("job_id"),
                },
            )
            return
        try:
            universe = fetcher.fetch_universe("main-board")[:200]
            from .strategy.screener import StockScreener
            screener = StockScreener()
            blocked_symbols = _build_blocked_symbols_from_events(serving_store.get_latest_event_context() or {})
            trade_date = date.today().isoformat()
            nightly_priority_boosts = _load_nightly_priority_boosts(trade_date)
            profile = _profile[0] or sentiment.calc_from_market_data(market) or MarketProfile(sentiment_phase="回暖")
            snapshots = fetcher.fetch_snapshots(universe)
            pre_ranked = sorted(
                snapshots,
                key=lambda item: (
                    (
                        (float(item.last_price or 0.0) - float(item.pre_close or item.last_price or 0.0))
                        / max(float(item.pre_close or item.last_price or 1.0), 1e-9)
                    ),
                    float(item.volume or 0.0),
                    float(item.last_price or 0.0),
                ),
                reverse=True,
            )
            snapshot_map = {item.symbol: item for item in pre_ranked}
            seed_scores: dict[str, float] = {}
            for index, item in enumerate(pre_ranked, start=1):
                decision = score_runtime_snapshot(
                    symbol=item.symbol,
                    last_price=float(item.last_price or 0.0),
                    pre_close=float(item.pre_close or item.last_price or 0.0),
                    volume=float(item.volume or 0.0),
                    rank=index,
                )
                seed_scores[item.symbol] = float(decision["selection_score"]) + float(nightly_priority_boosts.get(item.symbol, 0.0) or 0.0)

            candidate_symbols = [
                item.symbol
                for item in sorted(pre_ranked, key=lambda snap: seed_scores.get(snap.symbol, 0.0), reverse=True)[:90]
            ]
            result = screener.run(
                candidate_symbols,
                profile=profile,
                factor_scores=seed_scores,
                runtime_config=config_mgr.get(),
                blocked_symbols=blocked_symbols,
                top_n=60,
            )
            score_map = {symbol: float(seed_scores.get(symbol, 0.0) or 0.0) for symbol in result.passed}
            name_map = {
                symbol: (snapshot_map[symbol].name if symbol in snapshot_map and snapshot_map[symbol].name else market.get_symbol_name(symbol))
                for symbol in result.passed
            }
            workspace_context = dict(serving_store.get_latest_workspace_context() or {})
            latest_runtime_context = dict(runtime_state.get("latest_runtime_context", {}) or {})
            regime_driven_payload = _build_regime_driven_candidate_payloads(
                fetcher=fetcher,
                runtime_context=latest_runtime_context,
                workspace_context=workspace_context,
            )
            regime_guard = _sync_market_regime_guard(
                runtime_state,
                trade_date=trade_date,
                detected_regime=dict(regime_driven_payload.get("market_regime") or {}),
            )
            
            pool_mgr.update(result.passed, score_map, names=name_map)
            ranked_symbols = sorted(result.passed, key=lambda item: score_map.get(item, 0.0), reverse=True)
            runtime_top_picks = []
            for index, symbol in enumerate(ranked_symbols, start=1):
                snapshot = snapshot_map.get(symbol)
                scored = score_runtime_snapshot(
                    symbol=symbol,
                    last_price=float(getattr(snapshot, "last_price", 0.0) or 0.0),
                    pre_close=float(getattr(snapshot, "pre_close", getattr(snapshot, "last_price", 0.0)) or 0.0),
                    volume=float(getattr(snapshot, "volume", 0.0) or 0.0),
                    rank=index,
                )
                scored["name"] = name_map[symbol]
                scored["selection_score"] = round(float(scored.get("selection_score", 0.0) or 0.0) + float(nightly_priority_boosts.get(symbol, 0.0) or 0.0), 2)
                scored["source"] = "scheduler_pipeline"
                scored["source_tags"] = ["scheduler_pipeline", "default_strategy"]
                scored["summary"] = (
                    f"{name_map[symbol] or symbol} 默认策略分 {scored['selection_score']}，"
                    "已接入盘前环境/热点排序，可直接供竞价与盘中任务复用。"
                )
                scored["score_breakdown"] = {
                    **dict(scored.get("score_breakdown") or {}),
                    "nightly_priority_boost": float(nightly_priority_boosts.get(symbol, 0.0) or 0.0),
                }
                scored["market_snapshot"] = (
                    {
                        "last_price": float(getattr(snapshot, "last_price", 0.0) or 0.0),
                        "pre_close": float(getattr(snapshot, "pre_close", 0.0) or 0.0),
                        "volume": float(getattr(snapshot, "volume", 0.0) or 0.0),
                    }
                    if snapshot is not None
                    else {}
                )
                runtime_top_picks.append(scored)
            merged_top_pick_map = {
                str(item.get("symbol") or "").strip(): dict(item)
                for item in runtime_top_picks
                if str(item.get("symbol") or "").strip()
            }
            regime_items = list(regime_driven_payload.get("items") or [])
            for item in regime_items:
                symbol = str(item.get("symbol") or "").strip()
                if not symbol:
                    continue
                if symbol in merged_top_pick_map:
                    merged = dict(merged_top_pick_map[symbol])
                    merged["source"] = "regime_driven"
                    merged["source_tags"] = list(
                        dict.fromkeys(
                            [
                                *list(merged.get("source_tags") or []),
                                *list(item.get("source_tags") or []),
                            ]
                        )
                    )
                    merged["selection_score"] = max(
                        float(merged.get("selection_score") or 0.0),
                        float(item.get("selection_score") or 0.0),
                    ) + 3.0
                    merged["resolved_sector"] = item.get("resolved_sector") or merged.get("resolved_sector")
                    merged["headline_reason"] = item.get("headline_reason") or item.get("summary") or merged.get("summary")
                    merged["summary"] = item.get("summary") or merged.get("summary")
                    merged["market_profile"] = item.get("market_profile") or merged.get("market_profile")
                    merged_score_breakdown = dict(merged.get("score_breakdown") or {})
                    merged_score_breakdown["regime_driven_bonus"] = 3.0
                    merged_score_breakdown.update(dict(item.get("score_breakdown") or {}))
                    merged["score_breakdown"] = merged_score_breakdown
                    merged_top_pick_map[symbol] = merged
                    continue
                merged_top_pick_map[symbol] = dict(item)
            merged_top_picks = sorted(
                merged_top_pick_map.values(),
                key=lambda item: float(item.get("selection_score", 0.0) or 0.0),
                reverse=True,
            )
            for index, item in enumerate(merged_top_picks, start=1):
                item["rank"] = index
            merged_symbols = [str(item.get("symbol") or "").strip() for item in merged_top_picks if str(item.get("symbol") or "").strip()]
            dossier_pack = dossier_precompute_service.precompute(
                trade_date=trade_date,
                source="candidate_pool",
            )
            pack_items = list((dossier_pack or {}).get("items") or [])
            base_market_profile = MarketProfile.model_validate(
                {
                    **(_profile[0].model_dump() if _profile[0] is not None else profile.model_dump()),
                    "regime": (regime_driven_payload.get("market_regime") or {}).get("runtime_regime")
                    or getattr(profile, "regime", "unknown"),
                    "hot_sectors": (regime_driven_payload.get("market_regime") or {}).get("hot_sector_chain")
                    or list(getattr(profile, "hot_sectors", []) or []),
                }
            )
            sector_map = resolve_default_symbol_sector_map(
                pack_items,
                merged_symbols,
                market,
                fallback_top_picks=merged_top_picks,
            )
            sector_profiles = infer_default_sector_profiles(
                pack_items,
                merged_symbols,
                sector_map,
                base_market_profile,
            )
            effective_market_profile = base_market_profile.model_copy(
                update={
                    "hot_sectors": [item.sector_name for item in sector_profiles[:8]] or list(base_market_profile.hot_sectors),
                    "sector_profiles": sector_profiles,
                }
            )
            routing_profile, routing_meta = build_default_routing_market_profile(
                effective_market_profile,
                sector_profiles,
            )
            behavior_profiles = infer_default_behavior_profiles(
                pack_items,
                merged_symbols,
                sector_map,
                sector_profiles,
                merged_top_picks,
                routing_profile,
            )
            leader_ranks = build_default_leader_ranks(
                leader_ranker=leader_ranker,
                pack_items=pack_items,
                selected_symbols=merged_symbols,
                sector_map=sector_map,
                decisions=merged_top_picks,
                behavior_profiles=behavior_profiles,
            )
            playbook_contexts = strategy_router.route(
                profile=routing_profile,
                sector_profiles=sector_profiles,
                candidates=merged_symbols,
                stock_info={symbol: {"sector": sector_map.get(symbol, "")} for symbol in merged_symbols},
                behavior_profiles=behavior_profiles,
                leader_ranks=leader_ranks,
            )
            merged_top_picks = apply_default_playbook_order(merged_top_picks, playbook_contexts)
            merged_top_picks = apply_default_market_alignment_order(
                merged_top_picks,
                pack_items=pack_items,
                playbook_contexts=playbook_contexts,
                behavior_profiles=behavior_profiles,
                sector_map=sector_map,
                market_profile_payload={
                    **effective_market_profile.model_dump(),
                    "source": "scheduler_default_strategy",
                    "inferred": False,
                },
            )
            merged_symbols = [str(item.get("symbol") or "").strip() for item in merged_top_picks if str(item.get("symbol") or "").strip()]
            runtime_snapshot = {
                "job_id": f"scheduler-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "job_type": "scheduler_pipeline",
                "generated_at": datetime.now().isoformat(),
                "trade_date": trade_date,
                "selected_symbols": merged_symbols,
                "decision_count": len(merged_symbols),
                "nightly_priority_boosts": nightly_priority_boosts,
                "market_profile": {
                    **effective_market_profile.model_dump(),
                    "source": "scheduler_default_strategy",
                    "inferred": False,
                    "detected_market_regime": regime_driven_payload.get("market_regime") or {},
                },
                "sector_profiles": [item.model_dump() for item in sector_profiles],
                "playbook_contexts": [item.model_dump() for item in playbook_contexts],
                "behavior_profiles": [item.model_dump() for item in behavior_profiles.values()],
                "hot_sectors": [item.sector_name for item in sector_profiles[:8]],
                "playbook_routing_hint": routing_meta,
                "detected_market_regime": regime_driven_payload.get("market_regime") or {},
                "regime_transition_guard": regime_guard,
                "summary_lines": [
                    f"调度器默认策略已生成盘前候选 (base={len(ranked_symbols)} merged={len(merged_symbols)})。",
                    f"热点主线={','.join([item.sector_name for item in sector_profiles[:3]]) or '无'}。",
                    f"战法上下文={len(playbook_contexts)} 股性画像={len(behavior_profiles)}。",
                    *list(regime_driven_payload.get("summary_lines") or []),
                    *(
                        list(regime_guard.get("summary_lines") or [])
                        if bool(regime_guard.get("active"))
                        else []
                    ),
                ],
                "top_picks": merged_top_picks,
            }
            persist_scheduler_runtime_context(runtime_snapshot)
            sync_payload = persist_scheduler_runtime_snapshot(runtime_snapshot, source="scheduler_pipeline")
            circuit_breaker.record_success("market_data_pipeline", {"latest_runtime_report": runtime_snapshot})
            record_audit(
                "runtime",
                "调度器完成默认策略候选生成",
                {
                    **runtime_snapshot,
                    **sync_payload,
                },
            )
        except Exception as e:
            circuit_breaker.record_failure(
                "market_data_pipeline",
                str(e),
                cached_result=(runtime_state.get("latest_runtime_report", {}) or {}),
            )
            logger.warning("基础样本生成失败: %s", e)

    def task_tail_market():
        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=execution_adapter,
            meeting_state_store=meeting_state_store,
            runtime_state_store=runtime_state,
            execution_gateway_state_store=execution_gateway_state_store,
            position_watch_state_store=position_watch_state_store,
            monitor_state_service=monitor_state_service,
            candidate_case_service=candidate_case_service,
            discussion_cycle_service=discussion_cycle_service,
            dispatcher=dispatcher,
            order_strategy_resolver=order_strategy_resolver,
            quality_tracker=quality_tracker,
            trace_service=trace_service,
            runtime_context=(
                serving_store.get_latest_runtime_context()
                or runtime_state.get("latest_runtime_context", {})
                or {}
            ),
            discussion_context=_load_latest_discussion_context(),
            event_context=serving_store.get_latest_event_context() or {},
            execution_plane=str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
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
        regime_guard = dict(runtime_state.get("latest_regime_transition_guard", {}) or {})
        payload = _advance_execution_chain_via_control_plane(
            trigger="execute_open",
            try_finalize=True,
        )
        record_audit(
            "execution",
            "开盘执行任务已推进真实执行主链",
            {
                "run_mode": settings.run_mode,
                "latest_job_id": latest.get("job_id"),
                "execution_mode": settings.execution_mode,
                "regime_transition_guard": regime_guard,
                **payload,
            },
        )

    def task_execution_window_followthrough():
        payload = _advance_execution_chain_via_control_plane(
            trigger="execution_window_followthrough",
            try_finalize=True,
        )
        record_audit(
            "execution",
            "盘中执行窗口推进完成",
            payload,
        )

    def task_pre_confirm():
        latest = runtime_state.get("latest_runtime_report", {})
        record_audit(
            "risk",
            "完成买入清单预确认",
            {
                "selected_symbols": latest.get("selected_symbols", []),
                "regime_transition_guard": dict(runtime_state.get("latest_regime_transition_guard", {}) or {}),
            },
        )

    def _run_auction_snapshot(label: str) -> dict:
        symbols = _resolve_candidate_symbols(limit=30)
        trade_date = _resolve_trade_date_from_contexts()
        if not symbols:
            payload = {
                "trade_date": trade_date,
                "timestamp": datetime.now().isoformat(),
                "stage": label,
                "signals": [],
                "summary_lines": ["无候选标的，跳过竞价快照抓取。"],
            }
            research_state.set("latest_auction_snapshot", payload)
            return payload
        snapshots = auction_fetcher.fetch_snapshots(symbols, trade_date=trade_date)
        runtime_context = serving_store.get_latest_runtime_context() or runtime_state.get("latest_runtime_context", {}) or {}
        playbook_contexts = list(runtime_context.get("playbook_contexts") or [])
        playbook_map = {
            str(item.get("symbol")): str(item.get("playbook") or "leader_chase")
            for item in playbook_contexts
            if isinstance(item, dict) and item.get("symbol")
        }
        sector_map = {
            str(item.get("symbol")): str(item.get("sector") or "")
            for item in playbook_contexts
            if isinstance(item, dict) and item.get("symbol")
        }
        prev_signal_map = _load_previous_auction_signal_map(trade_date)
        signals = auction_engine.evaluate_all(
            snapshots,
            playbook_map=playbook_map,
            sector_map=sector_map,
            prev_signal_map=prev_signal_map,
        )
        signal_payload = [item.model_dump() for item in signals.values()]
        payload = {
            "trade_date": trade_date,
            "timestamp": datetime.now().isoformat(),
            "stage": label,
            "snapshot_count": len(snapshots),
            "signal_count": len(signals),
            "signals": signal_payload,
            "summary_lines": [
                f"竞价快照完成: stage={label} symbols={len(symbols)} snapshots={len(snapshots)} signals={len(signals)}。"
            ],
        }
        research_state.set("latest_auction_snapshot", payload)
        history = research_state.get("auction_signal_history", [])
        history.append(payload)
        research_state.set("auction_signal_history", history[-20:])
        for item in signals.values():
            event_bus.publish_sync(
                MarketEvent(
                    event_type="AUCTION_SIGNAL",
                    symbol=item.symbol,
                    payload=item.model_dump(),
                    timestamp=datetime.now().isoformat(),
                    priority=1 if item.action == "PROMOTE" else 0,
                    source=f"scheduler.auction.{label}",
                )
            )
        return payload

    def task_auction_0920():
        payload = _run_auction_snapshot("09:20")
        record_audit("auction", "完成 09:20 竞价快照", payload)

    def task_auction_0924():
        payload = _run_auction_snapshot("09:24")
        record_audit("auction", "完成 09:24 竞价快照", payload)

    def task_daily_settlement():
        trade_date = _resolve_trade_date_from_contexts()
        report = trade_attribution_service.build_report(trade_date=trade_date)
        settlement_results: list[dict] = []
        if report.available:
            for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
                settlement_results.append({"agent_id": agent_id})
        updated_states = agent_score_service.run_daily_settlement(
            settlement_results=settlement_results,
            trade_date=trade_date,
        )

        # 触发自治链: 从归因中自动发现 + 存量产物自动升级 (R0.5)
        discovered_ids = []
        promoted_ids = []
        if learned_asset_service:
            try:
                discovered_ids = learned_asset_service.auto_discover_from_attribution(trade_date, report)
                promoted_ids = learned_asset_service.auto_promote_assets()
            except Exception:
                logger.exception("自动学习/升级链执行失败: trade_date=%s", trade_date)

        record_audit(
            "learning",
            "完成学分结算与自治学习",
            {
                "trade_date": trade_date,
                "report_available": report.available,
                "trade_count": report.trade_count,
                "agent_count": len(updated_states),
                "discovered_asset_count": len(discovered_ids),
                "discovered_asset_ids": discovered_ids,
                "promoted_asset_count": len(promoted_ids),
                "promoted_asset_ids": promoted_ids,
            },
        )

    def task_prompt_patch():
        trade_date = _resolve_trade_date_from_contexts()
        report = trade_attribution_service.build_report(trade_date=trade_date)
        score_states = [item.model_dump() for item in agent_score_service.read_states(trade_date)]
        lessons = auto_governance.build_agent_lesson_patches(report=report, score_states=score_states)
        results = prompt_patcher.run_daily(lessons)
        payload = {
            "trade_date": trade_date,
            "lesson_agent_count": len(lessons),
            "patched_agent_count": len(results),
            "results": [item.model_dump() for item in results],
        }
        research_state.set("latest_prompt_patch", payload)
        record_audit("learning", "完成 Prompt 教训注入", payload)

    def task_registry_update():
        trade_date = _resolve_trade_date_from_contexts()
        states = [item.model_dump() for item in agent_score_service.read_states(trade_date)]
        updated = registry_updater.update_from_scores(states)
        payload = {
            "trade_date": trade_date,
            "updated": updated,
            "weights": registry_updater.read_current_weights(),
        }
        research_state.set("latest_registry_update", payload)
        record_audit("learning", "完成注册表权重覆写", payload)

    def task_self_evolve():
        trade_date = _resolve_trade_date_from_contexts()
        report = trade_attribution_service.build_report(trade_date=trade_date)
        strategy_metrics = {}
        for item in list(report.by_playbook or []):
            metrics = type("MetricProxy", (), {"sharpe_ratio": float(item.avg_next_day_close_pct or 0.0)})
            strategy_metrics[str(item.key)] = metrics
        suggestions = self_evolver.suggest_strategy_weights(strategy_metrics) if strategy_metrics else {}
        payload = {
            "trade_date": trade_date,
            "suggestions": suggestions,
            "report_available": report.available,
        }
        research_state.set("latest_self_evolve_suggestions", payload)
        record_audit("learning", "完成策略自进化建议生成", payload)

    def task_continuous_validate():
        trade_date = _resolve_trade_date_from_contexts()
        report = trade_attribution_service.build_report(trade_date=trade_date)
        payload = {
            "trade_date": trade_date,
            "report_available": report.available,
            "trade_count": report.trade_count,
            "history_size": len(continuous_learner._records),
            "summary_lines": list(report.summary_lines or []),
        }
        research_state.set("latest_continuous_validation", payload)
        record_audit("learning", "完成增量学习验证调度", payload)

    def task_nightly_sandbox():
        trade_date = _resolve_trade_date_from_contexts()
        discussion_context = _load_discussion_context_for_trade_date(trade_date) or _load_latest_discussion_context()
        finalize_bundle = dict(discussion_context.get("finalize_packet") or {})
        if not finalize_bundle:
            finalize_bundle = discussion_cycle_service.build_finalize_bundle(trade_date)
        report = trade_attribution_service.build_report(trade_date=trade_date)
        nightly_runner = NightlySandbox(
            settings.storage_root,
            replay_packet_builder=lambda symbol: discussion_cycle_service.build_openclaw_replay_proposal_packet(
                [],
                trade_date=trade_date,
                expected_case_ids=[],
            ),
        )
        result = nightly_runner.run_simulation(
            trade_date=trade_date,
            finalize_bundle=finalize_bundle,
            attribution_report=report.model_dump(),
            parameter_hints=list(report.parameter_hints or []),
        )
        payload = result.model_dump()
        research_state.set("latest_nightly_sandbox", payload)
        record_audit("strategy", "完成夜间沙盘推演", payload)

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

    def task_agent_supervision():
        trade_date = _resolve_trade_date_from_contexts()
        cases = candidate_case_service.list_cases(trade_date=trade_date, limit=500) if trade_date else []
        cycle = None
        discussion_breaker_state = circuit_breaker.check("discussion_service")
        if trade_date and discussion_cycle_service:
            if not discussion_breaker_state.get("available", True):
                logger.warning("discussion_service 熔断中，监督巡检使用缓存上下文: trade_date=%s", trade_date)
            else:
                existing_cycle = discussion_cycle_service.get_cycle(trade_date)
                if existing_cycle is None and cases:
                    try:
                        existing_cycle = discussion_cycle_service.bootstrap_cycle(trade_date)
                    except Exception as exc:
                        circuit_breaker.record_failure(
                            "discussion_service",
                            f"bootstrap:{exc}",
                            cached_result=(discussion_state_store.get("latest_cycle", {}) if discussion_state_store else {}),
                        )
                        logger.exception("监督巡检 bootstrap discussion cycle 失败: trade_date=%s", trade_date)
                if existing_cycle is not None:
                    try:
                        cycle = discussion_cycle_service.refresh_cycle(trade_date)
                        circuit_breaker.record_success(
                            "discussion_service",
                            {"trade_date": trade_date, "available": True},
                        )
                    except Exception as exc:
                        circuit_breaker.record_failure(
                            "discussion_service",
                            f"refresh:{exc}",
                            cached_result=(discussion_state_store.get("latest_cycle", {}) if discussion_state_store else {}),
                        )
                        logger.exception("监督巡检 refresh discussion cycle 失败: trade_date=%s", trade_date)
                        cycle = existing_cycle
        case_map = {item.case_id: item for item in cases}
        polling_status = monitor_state_service.get_polling_status()
        candidate_poll = (polling_status or {}).get("candidate") or {}
        execution_dispatch = meeting_state_store.get(f"execution_dispatch:{trade_date}", {}) if meeting_state_store and trade_date else {}
        overdue_after_seconds = 180
        items: list[dict[str, Any]] = []
        resolved_today = datetime.now().date().isoformat()
        session_open = bool(trade_date and trade_date == resolved_today and is_trading_session(datetime.now()))
        execution_followthrough: dict[str, Any] = {}

        if _should_supervision_followthrough_execution_chain(
            trade_date=trade_date,
            session_open=session_open,
            cycle=cycle,
            execution_dispatch=execution_dispatch if isinstance(execution_dispatch, dict) else {},
        ):
            execution_gate = monitor_state_service.mark_poll_if_due(
                "execution",
                trigger="agent_supervision_followthrough",
            )
            execution_followthrough = dict(execution_gate)
            if execution_gate.get("triggered"):
                try:
                    execution_followthrough = _advance_execution_chain_via_control_plane(
                        trigger="agent_supervision_followthrough",
                        try_finalize=True,
                    )
                    if meeting_state_store and trade_date:
                        latest_dispatch = meeting_state_store.get(f"execution_dispatch:{trade_date}", {})
                        if isinstance(latest_dispatch, dict) and latest_dispatch:
                            execution_dispatch = latest_dispatch
                    logger.info(
                        "监督巡检推进 execution chain: trade_date=%s cycle_state=%s prepared=%s dispatch=%s",
                        trade_date,
                        getattr(cycle, "discussion_state", ""),
                        ((execution_followthrough.get("execution_chain_progress") or {}).get("prepared")),
                        ((execution_followthrough.get("execution_dispatch") or {}).get("status")),
                    )
                except Exception:
                    logger.exception("监督巡检推进 execution chain 失败: trade_date=%s", trade_date)
                    execution_followthrough = {
                        "ok": False,
                        "reason": "agent_supervision_followthrough_failed",
                        "trade_date": trade_date,
                    }

        def _parse_iso_dt_local(value: str | None) -> datetime | None:
            normalized = str(value or "").strip()
            if not normalized:
                return None
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                return None

        def _max_activity_timestamp_local(*timestamps: str | None) -> str | None:
            normalized: list[tuple[datetime, str]] = []
            for timestamp in timestamps:
                parsed = _parse_iso_dt_local(timestamp)
                if parsed is None:
                    continue
                normalized.append((parsed, str(timestamp)))
            if not normalized:
                return None
            normalized.sort(key=lambda item: item[0])
            return normalized[-1][1]

        def _extract_activity_timestamp_local(
            payload: dict[str, Any] | None,
            *,
            current_trade_date: str | None = None,
            candidate_keys: tuple[str, ...] = ("generated_at", "updated_at", "captured_at", "recorded_at"),
        ) -> str | None:
            if not isinstance(payload, dict) or not payload:
                return None
            if current_trade_date:
                payload_trade_date = str(payload.get("trade_date") or "")[:10]
                generated_at = str(payload.get("generated_at") or "")[:10]
                if payload_trade_date and payload_trade_date != current_trade_date and generated_at and generated_at != current_trade_date:
                    return None
            for key in candidate_keys:
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
            return None

        def _seconds_since_local(timestamp: str | None) -> int | None:
            parsed = _parse_iso_dt_local(timestamp)
            if parsed is None:
                return None
            now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            return max(int((now - parsed).total_seconds()), 0)

        def _latest_state_store_activity_local(
            state_store: StateStore | None,
            keys: tuple[str, ...],
            *,
            current_trade_date: str | None = None,
        ) -> str | None:
            if not state_store:
                return None
            timestamps: list[str] = []
            for key in keys:
                payload = state_store.get(key)
                if isinstance(payload, dict):
                    ts = _extract_activity_timestamp_local(payload, current_trade_date=current_trade_date)
                    if ts:
                        timestamps.append(ts)
                elif isinstance(payload, list):
                    for item in reversed(payload[-5:]):
                        if not isinstance(item, dict):
                            continue
                        ts = _extract_activity_timestamp_local(item, current_trade_date=current_trade_date)
                        if ts:
                            timestamps.append(ts)
                            break
            return _max_activity_timestamp_local(*timestamps)

        def _latest_param_proposal_activity_local(
            *,
            current_trade_date: str | None = None,
            scopes: tuple[str, ...] = (),
        ) -> str | None:
            if not parameter_service:
                return None
            try:
                events = parameter_service.list_proposals()
            except Exception:
                return None
            timestamps: list[str] = []
            for event in events:
                created_at = str(getattr(event, "created_at", "") or "").strip()
                if not created_at:
                    continue
                if current_trade_date and created_at[:10] != current_trade_date:
                    continue
                scope = str(getattr(event, "scope", "") or "").strip()
                if scopes and scope not in scopes:
                    continue
                timestamps.append(created_at)
            return _max_activity_timestamp_local(*timestamps)

        def _build_activity_signal_bundle_local(label: str, entries: list[tuple[str, str | None]]) -> dict[str, Any]:
            signals: list[dict[str, Any]] = []
            latest_at: str | None = None
            for source, timestamp in entries:
                normalized_source = str(source or "").strip()
                if not normalized_source or not timestamp:
                    continue
                signals.append({"source": normalized_source, "last_active_at": timestamp})
                latest_at = _max_activity_timestamp_local(latest_at, timestamp)
            signals.sort(key=lambda item: _parse_iso_dt_local(item.get("last_active_at")) or datetime.min, reverse=True)
            return {
                "label": label,
                "last_active_at": latest_at,
                "signals": signals,
            }

        def _compose_evaluation_metrics_local(current_trade_date: str | None) -> dict[str, Any]:
            if not runtime_state or not current_trade_date:
                return {"count": 0, "latest_generated_at": None, "latest_trace_id": None, "latest_record": {}}
            records = list(runtime_state.get("compose_evaluations", []) or [])
            matched: list[dict[str, Any]] = []
            for item in records:
                generated_at = str(item.get("generated_at") or "").strip()
                adoption = dict(item.get("adoption") or {})
                if generated_at.startswith(current_trade_date) or str(adoption.get("trade_date") or "").strip() == current_trade_date:
                    matched.append(item)
            latest = matched[-1] if matched else {}
            return {
                "count": len(matched),
                "latest_generated_at": str(latest.get("generated_at") or "").strip() or None,
                "latest_trace_id": str(latest.get("trace_id") or "").strip() or None,
                "latest_record": dict(latest or {}),
            }

        latest_runtime_report = runtime_state.get("latest_runtime_report", {}) if runtime_state else {}
        latest_runtime_context = serving_store.get_latest_runtime_context() or {}
        latest_monitor_context = serving_store.get_latest_monitor_context() or {}
        compose_metrics = _compose_evaluation_metrics_local(trade_date)

        if cycle and discussion_cycle_service and trade_date:
            current_round = max(int(cycle.current_round or 0), 1)
            auto_writeback_states = {"round_1_running", "round_2_running", "round_running"}
            expected_case_ids = (
                list(cycle.focus_pool_case_ids or cycle.base_pool_case_ids or [])
                if current_round <= 1
                else list(cycle.round_2_target_case_ids or cycle.focus_pool_case_ids or [])
            )
            strategy_covered_case_ids: list[str] = []
            for case_id in expected_case_ids:
                case = case_map.get(case_id)
                if not case:
                    continue
                if any(
                    opinion.agent_id == "ashare-strategy" and int(opinion.round or 0) == current_round
                    for opinion in list(case.opinions or [])
                ):
                    strategy_covered_case_ids.append(case_id)
            latest_compose_record = dict(compose_metrics.get("latest_record") or {})
            if (
                cycle.discussion_state in auto_writeback_states
                and expected_case_ids
                and not strategy_covered_case_ids
                and latest_compose_record
            ):
                try:
                    auto_writeback = discussion_cycle_service.write_compose_strategy_opinions(
                        latest_compose_record,
                        trade_date=trade_date,
                        expected_round=current_round,
                        expected_agent_id="ashare-strategy",
                        auto_rebuild=True,
                    )
                    if auto_writeback.get("ok") and int(auto_writeback.get("written_count", 0) or 0) > 0:
                        cases = candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                        case_map = {item.case_id: item for item in cases}
                        cycle = discussion_cycle_service.refresh_cycle(trade_date)
                        logger.info(
                            "监督巡检自动写回 compose->strategy opinions: trade_date=%s round=%s written=%s trace_id=%s",
                            trade_date,
                            current_round,
                            auto_writeback.get("written_count", 0),
                            auto_writeback.get("trace_id"),
                        )
                except Exception:
                    logger.exception("监督巡检自动写回 compose strategy opinions 失败: trade_date=%s", trade_date)

        runtime_activity_bundle = _build_activity_signal_bundle_local(
            "运行事实产出",
            [
                ("runtime_report", _extract_activity_timestamp_local(latest_runtime_report, current_trade_date=trade_date)),
                ("runtime_context", _extract_activity_timestamp_local(latest_runtime_context, current_trade_date=trade_date)),
                ("monitor_context", _extract_activity_timestamp_local(latest_monitor_context, current_trade_date=trade_date)),
                ("candidate_poll", str(candidate_poll.get("last_polled_at") or "").strip() or None),
            ],
        )
        runtime_last_active_at = runtime_activity_bundle.get("last_active_at")
        runtime_activity_age = _seconds_since_local(runtime_last_active_at)
        runtime_status = "standby"
        runtime_reasons: list[str] = []
        if not runtime_last_active_at:
            if session_open:
                runtime_status = "needs_work"
                runtime_reasons.append("交易时段内尚未观察到 runtime/monitor 事实产出")
            else:
                runtime_reasons.append("当前无 runtime 活动记录，但也不在交易时段")
        elif session_open and runtime_activity_age is not None and runtime_activity_age > max(overdue_after_seconds, 300):
            runtime_status = "overdue"
            runtime_reasons.append(f"最近 runtime 活动距今 {int(runtime_activity_age)} 秒")
            runtime_reasons.append("当前监督的是运行事实产出是否迟滞，而不是 candidate_poll 调用次数")
        else:
            runtime_status = "working"
            runtime_reasons.append(f"最近 runtime 活动={runtime_last_active_at}")
            if session_open:
                runtime_reasons.append("当前监督基于活动痕迹，不按 runtime 调用频率催办")
        items.append(
            {
                "agent_id": "ashare-runtime",
                "status": runtime_status,
                "reasons": runtime_reasons,
                "last_active_at": runtime_last_active_at,
                "activity_label": runtime_activity_bundle.get("label"),
                "activity_signals": runtime_activity_bundle.get("signals"),
                "activity_signal_count": len(runtime_activity_bundle.get("signals") or []),
            }
        )

        coordinator_reasons: list[str] = []
        coordinator_status = "standby"
        if cycle:
            cycle_age = _seconds_since_local(cycle.updated_at)
            active_states = {"round_1_running", "round_2_running", "round_running", "final_review_ready"}
            if cycle.discussion_state == "idle" and (cycle.focus_pool_case_ids or cycle.execution_pool_case_ids):
                coordinator_status = "needs_work"
                coordinator_reasons.append(
                    f"候选池已就绪 focus={len(cycle.focus_pool_case_ids or [])} execution={len(cycle.execution_pool_case_ids or [])}，但 discussion 仍未启动"
                )
            elif cycle.discussion_state in active_states:
                if cycle_age is not None and cycle_age > overdue_after_seconds:
                    coordinator_status = "overdue"
                    coordinator_reasons.append(
                        f"讨论处于 {cycle.discussion_state}，但 cycle.updated_at 距今 {int(cycle_age)} 秒"
                    )
                else:
                    coordinator_status = "working"
                    coordinator_reasons.append(f"当前讨论态={cycle.discussion_state}")
            else:
                coordinator_status = "working"
                coordinator_reasons.append(f"当前讨论态={cycle.discussion_state}")
        else:
            coordinator_reasons.append("当前无 active cycle")
        items.append(
            {
                "agent_id": "ashare",
                "status": coordinator_status,
                "reasons": coordinator_reasons,
                "last_active_at": (cycle.updated_at if cycle else None),
            }
        )

        if cycle:
            current_round = int(cycle.current_round or 0)
            expected_case_ids = (
                list(cycle.focus_pool_case_ids or cycle.base_pool_case_ids or [])
                if current_round <= 1
                else list(cycle.round_2_target_case_ids or cycle.focus_pool_case_ids or [])
            )
            discussion_agent_activity = {
                "ashare-research": _build_activity_signal_bundle_local(
                    "研究/事件产出",
                    [
                        ("research_summary", _extract_activity_timestamp_local(research_state.get("summary", {}), current_trade_date=trade_date)),
                        ("event_fetch_result", _latest_state_store_activity_local(research_state, ("latest_event_fetch_result",), current_trade_date=trade_date)),
                        ("dossier_or_behavior", _latest_state_store_activity_local(research_state, ("latest_dossier_pack", "latest_stock_behavior_profiles"), current_trade_date=trade_date)),
                        ("intraday_or_tail_scan", _latest_state_store_activity_local(meeting_state_store, ("latest_tail_market_scan", "latest_intraday_rank_result"), current_trade_date=trade_date)),
                    ],
                ),
                "ashare-strategy": _build_activity_signal_bundle_local(
                    "策略/调参提案",
                    [
                        ("compose_evaluation", compose_metrics.get("latest_generated_at")),
                        ("param_proposals", _latest_param_proposal_activity_local(current_trade_date=trade_date, scopes=("strategy", "runtime", "execution"))),
                        ("playbook_override", _latest_state_store_activity_local(meeting_state_store, ("latest_playbook_override_snapshot",), current_trade_date=trade_date)),
                        ("proposal_packet", _latest_state_store_activity_local(meeting_state_store, ("latest_openclaw_proposal_packet", "latest_offline_self_improvement_export"), current_trade_date=trade_date)),
                        ("nightly_sandbox", _latest_state_store_activity_local(research_state, ("latest_nightly_sandbox",), current_trade_date=trade_date)),
                    ],
                ),
                "ashare-risk": _build_activity_signal_bundle_local(
                    "风控/执行预检",
                    [
                        ("execution_precheck", _latest_state_store_activity_local(meeting_state_store, ("latest_execution_precheck",), current_trade_date=trade_date)),
                        ("execution_reconciliation", _latest_state_store_activity_local(meeting_state_store, ("latest_execution_reconciliation", "latest_pending_order_remediation"), current_trade_date=trade_date)),
                        ("tail_market_scan", _latest_state_store_activity_local(meeting_state_store, ("latest_tail_market_scan",), current_trade_date=trade_date)),
                    ],
                ),
                "ashare-audit": _build_activity_signal_bundle_local(
                    "审计/纪要复核",
                    [
                        ("audit_records", _latest_state_store_activity_local(meeting_state_store, ("latest", "latest_review_board", "latest_openclaw_replay_packet"), current_trade_date=trade_date)),
                        ("execution_dispatch", _latest_state_store_activity_local(meeting_state_store, ("latest_execution_dispatch",), current_trade_date=trade_date)),
                        ("governance_proposals", _latest_param_proposal_activity_local(current_trade_date=trade_date, scopes=("risk", "governance"))),
                    ],
                ),
            }
            activity_window_seconds = max(overdue_after_seconds, 300)
            for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
                covered = 0
                for case_id in expected_case_ids:
                    case = case_map.get(case_id)
                    if not case:
                        continue
                    matched = [
                        op for op in list(case.opinions or [])
                        if op.agent_id == agent_id and op.round == max(current_round, 1)
                    ]
                    if matched:
                        covered += 1
                reasons: list[str] = []
                status = "standby"
                if cycle.discussion_state in {"round_1_running", "round_2_running", "round_running"} and expected_case_ids:
                    age = _seconds_since(
                        (cycle.round_2_started_at if current_round >= 2 else cycle.round_1_started_at) or cycle.updated_at
                    )
                    if covered >= len(expected_case_ids):
                        status = "working"
                        reasons.append(f"Round {max(current_round, 1)} 已覆盖 {covered}/{len(expected_case_ids)}")
                    else:
                        status = "overdue" if age is not None and age > overdue_after_seconds else "needs_work"
                        reasons.append(f"Round {max(current_round, 1)} 仅覆盖 {covered}/{len(expected_case_ids)}")
                        if age is not None:
                            reasons.append(f"本轮开始后已过去 {int(age)} 秒")
                else:
                    activity_meta = discussion_agent_activity.get(agent_id, {})
                    activity_last_active_at = str(activity_meta.get("last_active_at") or "").strip() or None
                    activity_label = str(activity_meta.get("label") or "活动痕迹")
                    activity_age = _seconds_since_local(activity_last_active_at)
                    if activity_last_active_at:
                        if session_open and activity_age is not None and activity_age > activity_window_seconds:
                            status = "overdue"
                            reasons.append(f"{activity_label}距今 {int(activity_age)} 秒，盘中响应偏慢")
                        else:
                            status = "working"
                            reasons.append(f"最近{activity_label}={activity_last_active_at}")
                    elif session_open:
                        status = "needs_work"
                        reasons.append(f"交易时段内尚未观察到{activity_label}")
                    else:
                        reasons.append("当前无进行中的讨论轮次")
                activity_meta = discussion_agent_activity.get(agent_id, {})
                items.append(
                    {
                        "agent_id": agent_id,
                        "status": status,
                        "reasons": reasons,
                        "last_active_at": activity_meta.get("last_active_at"),
                        "activity_label": activity_meta.get("label"),
                        "activity_signals": activity_meta.get("signals"),
                        "activity_signal_count": len(activity_meta.get("signals") or []),
                    }
                )

            executor_status = "standby"
            executor_reasons: list[str] = []
            if cycle.execution_pool_case_ids and not execution_dispatch:
                executor_status = "needs_work"
                executor_reasons.append(f"execution_pool={len(cycle.execution_pool_case_ids)}，尚无最新 dispatch 回执")
            elif execution_dispatch:
                executor_status = "working"
                executor_reasons.append(f"最新 dispatch 状态={execution_dispatch.get('status')}")
            items.append({"agent_id": "ashare-executor", "status": executor_status, "reasons": executor_reasons})
        else:
            items.extend(
                {"agent_id": agent_id, "status": "standby", "reasons": ["当前无 active cycle"]}
                for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit", "ashare-executor")
            )

        attention_items = [item for item in items if item.get("status") in {"needs_work", "overdue"}]
        
        # S1.3: 提取最新的重大市场变化
        latest_event_context = serving_store.get_latest_event_context() or {}
        latest_market_change_at = None
        highlights = list(latest_event_context.get("highlights") or [])
        for ev in highlights:
            severity = str(ev.get("severity") or "").lower()
            if severity in {"high", "critical", "block"}:
                ev_time = ev.get("event_at") or ev.get("recorded_at")
                if ev_time and (not latest_market_change_at or str(ev_time) > str(latest_market_change_at)):
                    latest_market_change_at = ev_time

        # 重新评估 overdue 状态，加入市场变化响应检查
        for item in items:
            agent_id = item["agent_id"]
            if session_open and latest_market_change_at and agent_id in {"ashare-research", "ashare-strategy"}:
                latest_activity_at = item.get("last_active_at")
                if not latest_activity_at or str(latest_activity_at) < str(latest_market_change_at):
                    change_age = _seconds_since_local(latest_market_change_at)
                    if change_age is not None and change_age > 600: # 10分钟无响应
                        if item["status"] != "overdue":
                            item["status"] = "overdue"
                            item["reasons"].append(f"重大市场变化({latest_market_change_at})后 10 分钟无新提案/回应")
                            if item not in attention_items:
                                attention_items.append(item)

        payload = annotate_supervision_payload(
            {
                "trade_date": trade_date,
                "cycle_state": (cycle.discussion_state if cycle else None),
                "round": (int(cycle.current_round or 0) if cycle else 0),
                "items": items,
                "attention_items": attention_items,
                "summary_lines": [
                    f"trade_date={trade_date} supervision_items={len(items)} attention={len(attention_items)}",
                    "人工默认只接重要业务消息；催办和升级由机器人自动完成。",
                    "监督重点是 agent 的主线产物、真实活动痕迹与响应迟滞，不是机械催工具调用次数。",
                    *(
                        ["今日 compose 评估账本=0；若策略侧只有调参痕迹而无编排产物，按未完成主线处理。"]
                        if int(compose_metrics.get("count", 0) or 0) <= 0
                        else []
                    ),
                    *[f"{item['agent_id']}={item['status']}" for item in attention_items[:6]],
                ],
                "important_message_policy": {
                    "human_push_only": ["最终推荐/阻断简报", "真实买卖回执", "实盘执行告警", "盘后战果与学习摘要"],
                    "robot_messages": ["值班催办", "超时升级", "流程阻塞提醒"],
                },
                "execution_followthrough": execution_followthrough,
            },
            meeting_state_store,
        )
        latest_execution_precheck = (
            meeting_state_store.get("latest_execution_precheck", {})
            if meeting_state_store
            else {}
        ) or {}
        position_context = {}
        if isinstance(latest_execution_precheck, dict) and latest_execution_precheck:
            precheck_trade_date = str(latest_execution_precheck.get("trade_date") or "").strip()
            if not trade_date or not precheck_trade_date or precheck_trade_date == trade_date:
                position_context = {
                    "trade_date": precheck_trade_date,
                    "position_count": int(latest_execution_precheck.get("current_equity_position_count", 0) or 0),
                    "current_total_ratio": float(latest_execution_precheck.get("current_total_ratio", 0.0) or 0.0),
                    "equity_position_limit": float(
                        latest_execution_precheck.get("effective_total_position_limit", 0.0) or 0.0
                    ),
                    "available_test_trade_value": float(
                        latest_execution_precheck.get("stock_test_budget_remaining", 0.0) or 0.0
                    ),
                    "stock_test_budget_amount": float(
                        latest_execution_precheck.get("stock_test_budget_amount", 0.0) or 0.0
                    ),
                }
        task_plan = build_agent_task_plan(
            payload,
            execution_summary={
                "intent_count": (
                    len(list(cycle.execution_pool_case_ids or []))
                    if cycle and cycle.execution_pool_case_ids
                    else 0
                ),
                "dispatch_status": str((execution_dispatch or {}).get("status") or ""),
                "submitted_count": int((execution_dispatch or {}).get("submitted_count", 0) or 0),
                "preview_count": int((execution_dispatch or {}).get("preview_count", 0) or 0),
                "blocked_count": int((execution_dispatch or {}).get("blocked_count", 0) or 0),
                "latest_execution_reconciliation": (
                    meeting_state_store.get("latest_execution_reconciliation", {})
                    if meeting_state_store
                    else {}
                ) or {},
                "latest_review_board": (
                    meeting_state_store.get("latest_review_board", {})
                    if meeting_state_store
                    else {}
                ) or {},
                "latest_review_board_summary_lines": list(
                    (
                        (
                            meeting_state_store.get("latest_review_board", {})
                            if meeting_state_store
                            else {}
                        )
                        or {}
                    ).get("summary_lines")
                    or []
                ),
                "latest_nightly_sandbox": (
                    research_state.get("latest_nightly_sandbox", {})
                    if research_state
                    else {}
                ) or {},
            },
            position_context=position_context,
            latest_market_change_at=latest_market_change_at,
            meeting_state_store=meeting_state_store,
        )
        payload["items"] = task_plan.get("items", payload.get("items", []))
        payload["attention_items"] = task_plan.get("attention_items", payload.get("attention_items", []))
        payload["notify_items"] = task_plan.get("notify_items", payload.get("notify_items", []))
        payload["task_dispatch_plan"] = {
            "phase": task_plan.get("phase"),
            "position_context": position_context,
            "summary_lines": task_plan.get("summary_lines", []),
            "recommended_count": len(task_plan.get("recommended_tasks", [])),
            "recommended_tasks": task_plan.get("recommended_tasks", []),
            "tasks": task_plan.get("tasks", []),
        }
        payload["summary_lines"] = list(payload.get("summary_lines") or []) + [
            line for line in list(task_plan.get("summary_lines") or []) if line
        ]
        followthrough_reason = str(execution_followthrough.get("reason") or "").strip()
        if execution_followthrough.get("ok") or followthrough_reason:
            payload["summary_lines"].append(
                "监督兜底执行链推进="
                + (
                    followthrough_reason
                    or str(((execution_followthrough.get("execution_dispatch") or {}).get("status")) or "ok")
                )
            )
        if meeting_state_store:
            history = meeting_state_store.get("agent_supervision_history", [])
            history.append(payload)
            meeting_state_store.set("latest_agent_supervision", payload)
            meeting_state_store.set("agent_supervision_history", history[-200:])
        record_audit(
            "supervision",
            "完成 Agent 监督巡检",
            {
                "trade_date": trade_date,
                "attention_count": len(attention_items),
                "attention_agent_ids": [item.get("agent_id") for item in attention_items],
            },
        )
        notify_items = list(payload.get("notify_items") or [])
        recommended_tasks = [dict(item) for item in list((payload.get("task_dispatch_plan") or {}).get("recommended_tasks") or [])]
        if recommended_tasks:
            notify_map = {str(item.get("agent_id") or ""): dict(item) for item in notify_items}
            for task in recommended_tasks:
                agent_id = str(task.get("agent_id") or "").strip()
                if agent_id and agent_id in notify_map:
                    notify_map[agent_id].update(task)
                elif agent_id:
                    notify_map[agent_id] = dict(task)
            notify_items = list(notify_map.values())
        if notify_items:
            dispatch_title = str(payload.get("notification_title") or "Agent 自动催办")
            level = str(payload.get("notification_level") or "info")
            content = agent_supervision_template(dispatch_title, payload["summary_lines"], notify_items)
            dispatched = dispatcher.dispatch_monitor_changes(dispatch_title, content, level=level, force=level in {"warning", "critical"})
            if dispatched:
                record_supervision_notification(
                    meeting_state_store,
                    payload.get("trade_date"),
                    signature=str(payload.get("attention_signature") or ""),
                    level=level,
                    item_count=len(notify_items),
                )
                for item in notify_items:
                    agent_id = str(item.get("agent_id") or "").strip()
                    dispatch_key = str(item.get("dispatch_key") or "").strip()
                    if agent_id and dispatch_key:
                        record_agent_task_dispatch(
                            meeting_state_store,
                            payload.get("trade_date"),
                            agent_id=agent_id,
                            dispatch_key=dispatch_key,
                            task_payload=item,
                        )

    def task_agent_runtime_autonomy():
        trade_date = _resolve_trade_date_from_contexts()
        if not trade_date or not meeting_state_store:
            return
        control_key = f"{AGENT_AUTONOMY_CONTROL_KEY_PREFIX}{trade_date}"
        control_state = dict(meeting_state_store.get(control_key, {}) or {})
        now = datetime.now()
        control_plane_base_url = _resolve_internal_control_plane_base_url()
        autonomy_timeout_sec = _resolve_agent_autonomy_timeout_sec()
        endpoint_map = {
            "ashare-research": "/system/discussions/opinions/research-writeback",
            "ashare-risk": "/system/discussions/opinions/risk-writeback",
            "ashare-audit": "/system/discussions/opinions/audit-writeback",
        }
        with httpx.Client(
            timeout=httpx.Timeout(
                timeout=autonomy_timeout_sec,
                connect=min(10.0, autonomy_timeout_sec),
            )
        ) as client:
            for agent_id in ("ashare-strategy", "ashare-research", "ashare-risk", "ashare-audit"):
                agent_state = dict(control_state.get(agent_id) or {})
                last_success_at = str(agent_state.get("last_success_at") or "").strip() or None
                last_success_dt = None
                if last_success_at:
                    try:
                        last_success_dt = datetime.fromisoformat(last_success_at)
                    except ValueError:
                        last_success_dt = None
                last_error_at = str(agent_state.get("last_error_at") or "").strip() or None
                last_error_dt = None
                if last_error_at:
                    try:
                        last_error_dt = datetime.fromisoformat(last_error_at)
                    except ValueError:
                        last_error_dt = None
                if last_success_dt is not None and (now - last_success_dt).total_seconds() < 600:
                    continue
                if last_error_dt is not None and (now - last_error_dt).total_seconds() < 60:
                    continue
                try:
                    packet_response = client.get(
                        f"{control_plane_base_url}/system/agents/runtime-work-packets",
                        params={
                            "trade_date": trade_date,
                            "agent_id": agent_id,
                            "recommended_only": "true",
                        },
                    )
                    packet_response.raise_for_status()
                    packet_payload = packet_response.json()
                    packets = list(packet_payload.get("packets") or [])
                    if not packets:
                        continue
                    packet = dict(packets[0] or {})
                    if str(packet.get("status") or "").strip() not in {"needs_work", "overdue"}:
                        continue

                    agent_state["last_attempt_at"] = now.isoformat()
                    agent_state["last_phase_label"] = str(packet.get("phase_label") or "")
                    control_state[agent_id] = agent_state
                    meeting_state_store.set(control_key, control_state)

                    trace_id = ""
                    selected_profile: dict[str, Any] = {}
                    if agent_id == "ashare-strategy":
                        compose_response = client.get(
                            f"{control_plane_base_url}/runtime/evaluations/panel",
                            params={"limit": 5},
                        )
                        compose_response.raise_for_status()
                        compose_panel = compose_response.json()
                        compose_records = [
                            dict(item)
                            for item in list(compose_panel.get("items") or [])
                            if isinstance(item, dict)
                        ]
                        latest_record = next(
                            (
                                item
                                for item in compose_records
                                if _compose_record_matches_trade_date(item, trade_date)
                            ),
                            {},
                        )
                        trace_id = str(latest_record.get("trace_id") or "").strip()
                        if trace_id:
                            writeback_response = client.post(
                                f"{control_plane_base_url}/system/discussions/opinions/compose-writeback",
                                json={
                                    "trade_date": trade_date,
                                    "trace_id": trace_id,
                                    "expected_agent_id": "ashare-strategy",
                                },
                            )
                        else:
                            compose_hint = dict(packet.get("compose_brief_hint") or {})
                            if not compose_hint.get("available"):
                                continue
                            compose_profiles = [
                                dict(item)
                                for item in list(compose_hint.get("reference_templates") or compose_hint.get("profiles") or [])
                                if isinstance(item, dict)
                            ]
                            selected_profile = next(
                                (item for item in compose_profiles if bool(item.get("recommended")) or bool(item.get("selected"))),
                                compose_profiles[0] if compose_profiles else {},
                            )
                            selected_payload = dict(selected_profile.get("payload") or {})
                            if not selected_payload:
                                selected_payload = dict(compose_hint.get("sample_payload") or {})
                            if not selected_payload:
                                selected_payload = dict(compose_hint.get("custom_payload_template") or {})
                            if not selected_payload:
                                continue
                            compose_run = client.post(
                                f"{control_plane_base_url}{compose_hint.get('endpoint')}",
                                json=selected_payload,
                            )
                            compose_run.raise_for_status()
                            compose_payload = compose_run.json()
                            if not compose_payload.get("ok", True):
                                raise RuntimeError(str(compose_payload.get("error") or "compose failed"))
                            trace_id = str(((compose_payload.get("evaluation_trace") or {}).get("trace_id") or "")).strip()
                            if not trace_id:
                                raise RuntimeError("compose returned without trace_id")
                            writeback_response = client.post(
                                f"{control_plane_base_url}/system/discussions/opinions/compose-writeback",
                                json={
                                    "trade_date": trade_date,
                                    "trace_id": trace_id,
                                    "expected_agent_id": "ashare-strategy",
                                },
                            )
                    else:
                        writeback_response = client.post(
                            f"{control_plane_base_url}{endpoint_map[agent_id]}",
                            json={
                                "trade_date": trade_date,
                                "expected_agent_id": agent_id,
                            },
                        )
                    writeback_response.raise_for_status()
                    writeback_payload = writeback_response.json()
                    if not writeback_payload.get("ok"):
                        raise RuntimeError(str(writeback_payload.get("error") or f"{agent_id} writeback failed"))
                    agent_state.update(
                        {
                            "last_success_at": datetime.now().isoformat(),
                            "last_trace_id": trace_id,
                            "last_written_count": int(writeback_payload.get("written_count", 0) or 0),
                            "last_compose_profile_id": (
                                str(selected_profile.get("id") or "").strip()
                                if agent_id == "ashare-strategy"
                                else str(agent_state.get("last_compose_profile_id") or "")
                            ),
                            "last_error": "",
                        }
                    )
                    control_state[agent_id] = agent_state
                    meeting_state_store.set(control_key, control_state)
                    record_audit(
                        "autonomy",
                        "完成 agent 自主起手写回",
                        {
                            "trade_date": trade_date,
                            "agent_id": agent_id,
                            "trace_id": trace_id,
                            "compose_profile_id": str(selected_profile.get("id") or "").strip(),
                            "written_count": int(writeback_payload.get("written_count", 0) or 0),
                        },
                    )
                except Exception as exc:
                    agent_state.update(
                        {
                            "last_error": str(exc),
                            "last_error_at": datetime.now().isoformat(),
                        }
                    )
                    control_state[agent_id] = agent_state
                    meeting_state_store.set(control_key, control_state)
                    logger.warning(
                        "agent runtime autonomy failed: trade_date=%s agent_id=%s error=%s",
                        trade_date,
                        agent_id,
                        exc,
                    )

    def task_reconcile_backtest():
        try:
            records = evaluation_ledger_service.list_records(limit=20)
            pending_records = [r for r in records if r.get("outcome", {}).get("status") == "pending"]
            if not pending_records:
                logger.info("账本回测验证: 无待验证记录")
                return

            # 批量获取行情
            all_symbols = set()
            for r in pending_records:
                all_symbols.update(evaluation_ledger_service._resolve_target_symbols(r))
            
            if not all_symbols:
                return

            price_data = {}
            for symbol in all_symbols:
                bars = market.get_bars([symbol], period="1d", count=60)
                if bars:
                    df = pd.DataFrame([{"open": b.open, "close": b.close, "high": b.high, "low": b.low, "volume": b.volume} for b in bars])
                    df.index = [b.trade_time[:10] for b in bars]
                    price_data[symbol] = df

            results = []
            for r in pending_records:
                trace_id = r["trace_id"]
                try:
                    # 1. 运行 T+1 收益回测
                    updated = evaluation_ledger_service.reconcile_backtest(trace_id=trace_id, price_data=price_data)
                    
                    # 2. 评估因子有效性 (Rank IC)
                    evaluation_ledger_service.reconcile_factor_performance(trace_id=trace_id, price_data=price_data)
                    
                    results.append(f"{trace_id}:{updated.get('outcome', {}).get('status')}")
                except Exception as ex:
                    logger.warning("回测验证失败 trace_id=%s: %s", trace_id, ex)

            record_audit(
                "runtime",
                "完成账本回测验证",
                {
                    "pending_count": len(pending_records),
                    "processed_count": len(results),
                    "results": results,
                },
            )
        except Exception as e:
            logger.warning("账本回测验证任务失败: %s", e)

    def task_factor_effectiveness_refresh():
        breaker_state = circuit_breaker.check("factor_monitor")
        if not breaker_state.get("available", True):
            fallback = dict(breaker_state.get("cached_result") or runtime_state.get("latest_factor_effectiveness", {}) or {})
            runtime_state.set("latest_factor_effectiveness", fallback)
            record_audit(
                "runtime",
                "因子有效性巡检走熔断降级",
                {
                    "breaker": {"subsystem": "factor_monitor", "status": breaker_state.get("status")},
                    "fallback_generated_at": fallback.get("generated_at"),
                },
            )
            return
        try:
            trade_date = _resolve_trade_date_from_contexts()
            payload = factor_monitor.build_effectiveness_snapshot(trade_date=trade_date, force=True)
            runtime_state.set("latest_factor_effectiveness", payload)
            circuit_breaker.record_success("factor_monitor", payload)
            effective_count = sum(
                1 for item in list(payload.get("items") or []) if str(item.get("status") or "") == "effective"
            )
            record_audit(
                "runtime",
                "完成因子有效性巡检",
                {
                    "trade_date": trade_date,
                    "factor_count": len(list(payload.get("items") or [])),
                    "effective_count": effective_count,
                    "generated_at": payload.get("generated_at"),
                },
            )
        except Exception as exc:
            circuit_breaker.record_failure(
                "factor_monitor",
                str(exc),
                cached_result=(runtime_state.get("latest_factor_effectiveness", {}) or {}),
            )
            logger.warning("因子有效性巡检任务失败: %s", exc)

    def task_position_stress_test():
        try:
            resolved_account_id = _resolve_scheduler_account_id(settings, execution_adapter)
            positions = list(execution_adapter.get_positions(resolved_account_id) or [])
            balance = execution_adapter.get_balance(resolved_account_id)
            equity_positions = [
                item for item in summarize_position_buckets(positions).equity_positions
                if int(getattr(item, "quantity", 0) or 0) > 0
            ]
            payload = stress_test_service.run(
                equity_positions,
                total_asset=float(getattr(balance, "total_asset", 0.0) or 0.0),
            )
            runtime_state.set("latest_stress_test", payload)
            record_audit(
                "risk",
                "完成持仓压力测试",
                {
                    "account_id": resolved_account_id,
                    "position_count": payload.get("position_count", 0),
                    "worst_loss_pct": payload.get("worst_loss_pct", 0.0),
                    "worst_scenario": payload.get("worst_scenario", ""),
                },
            )
        except Exception as exc:
            logger.warning("持仓压力测试任务失败: %s", exc)

    def task_data_freshness_check():
        try:
            sample_symbols = list(market.get_main_board_universe()[:5])
            payload = {
                "gateway_health": freshness_monitor.check_gateway_health(),
                "kline_freshness": freshness_monitor.check_kline_freshness(sample_symbols),
                "universe_coverage": freshness_monitor.check_universe_coverage(),
            }
            payload["status"] = (
                "degraded"
                if payload["gateway_health"].get("status") in {"unreachable", "degraded"}
                or payload["kline_freshness"].get("status") == "stale"
                or payload["universe_coverage"].get("status") == "degraded"
                else "healthy"
            )
            runtime_state.set("latest_data_health", payload)
            record_audit("data", "完成数据新鲜度巡检", payload)
            if settings.notify.alerts_enabled and payload["status"] == "degraded":
                dispatcher.dispatch_alert(
                    "\n".join(
                        [
                            "数据新鲜度巡检降级",
                            f"gateway={payload['gateway_health'].get('status')}",
                            f"kline={payload['kline_freshness'].get('status')}",
                            f"coverage={payload['universe_coverage'].get('count')}",
                        ]
                    )
                )
        except Exception as exc:
            logger.warning("数据新鲜度巡检失败: %s", exc)

    def task_bridge_guardian():
        try:
            payload = run_bridge_guardian_check(
                settings,
                runtime_state,
                monitor_state_service,
                dispatcher,
            )
            record_audit(
                "execution",
                "完成执行桥健康守护",
                {
                    "status": payload.get("status"),
                    "blocked": payload.get("blocked"),
                    "age_seconds": payload.get("age_seconds"),
                },
            )
        except Exception as exc:
            logger.warning("执行桥健康守护失败: %s", exc)

    def task_stale_order_cleanup():
        try:
            resolved_account_id = _resolve_scheduler_account_id(settings, execution_adapter)
            payload = run_stale_order_cleanup(
                execution_gateway_state_store,
                execution_adapter,
                account_id=resolved_account_id,
                audit_store=audit_store,
            )
            meeting_state_store.set("latest_pending_order_remediation", payload)
            record_audit(
                "execution",
                "完成挂单治理",
                {
                    "account_id": resolved_account_id,
                    "checked_count": payload.get("checked_count", 0),
                    "stale_count": payload.get("stale_count", 0),
                    "orphaned_count": payload.get("orphaned_count", 0),
                    "claim_retry_count": payload.get("claim_retry_count", 0),
                },
            )
        except Exception as exc:
            logger.warning("挂单治理任务失败: %s", exc)

    def task_execution_bridge_reconciliation():
        breaker_state = circuit_breaker.check("execution_reconciliation")
        if not breaker_state.get("available", True):
            cached = dict(
                breaker_state.get("cached_result")
                or meeting_state_store.get("latest_execution_reconciliation", {})
                or {}
            )
            if cached:
                meeting_state_store.set("latest_execution_reconciliation", cached)
            record_audit(
                "execution",
                "执行对账走熔断降级",
                {
                    "breaker": {"subsystem": "execution_reconciliation", "status": breaker_state.get("status")},
                    "fallback_generated_at": cached.get("generated_at"),
                },
            )
            return
        try:
            resolved_account_id = _resolve_scheduler_account_id(settings, execution_adapter)
            payload = run_execution_bridge_reconciliation(
                execution_adapter,
                meeting_state_store,
                execution_gateway_state_store,
                account_id=resolved_account_id,
            )
            trade_date = _resolve_trade_date_from_contexts()
            execution_quality_report = quality_tracker.summarize_day(trade_date, persist=True)
            payload["execution_quality_report"] = execution_quality_report
            latest_report = trade_attribution_service.latest_report()
            playbook_updates = trade_attribution_service.build_playbook_priority_updates(latest_report)
            applied_updates = playbook_registry.apply_priority_updates(playbook_updates)
            payload["playbook_priority_updates"] = applied_updates
            recent_records = evaluation_ledger_service.list_records(limit=200)
            backtest_returns: list[float] = []
            replay_candidate_records: list[dict[str, Any]] = []
            replay_symbols: set[str] = set()
            for record in recent_records:
                generated_at = str(record.get("generated_at") or "")[:10]
                adoption_trade_date = str((record.get("adoption") or {}).get("trade_date") or "")[:10]
                if trade_date not in {generated_at, adoption_trade_date}:
                    try:
                        record_date = pd.Timestamp(adoption_trade_date or generated_at).date()
                        if (pd.Timestamp(trade_date).date() - record_date).days not in range(0, 6):
                            continue
                    except Exception:
                        continue
                posterior_metrics = dict((record.get("outcome") or {}).get("posterior_metrics") or {})
                backtest_metrics = dict(posterior_metrics.get("backtest_metrics") or {})
                if "total_return" in backtest_metrics:
                    backtest_returns.append(float(backtest_metrics.get("total_return", 0.0) or 0.0))
                replay_candidate_records.append(record)
                replay_symbols.update(evaluation_ledger_service._resolve_target_symbols(record))
            replay_price_data: dict[str, pd.DataFrame] = {}
            if replay_symbols:
                bars = market.get_bars(sorted(replay_symbols), period="1d", count=90, end_time=trade_date)
                grouped_rows: dict[str, list[dict[str, Any]]] = {}
                for bar in bars:
                    grouped_rows.setdefault(str(bar.symbol), []).append(
                        {
                            "open": float(bar.open),
                            "high": float(bar.high),
                            "low": float(bar.low),
                            "close": float(bar.close),
                            "volume": float(bar.volume),
                            "amount": float(getattr(bar, "amount", 0.0) or 0.0),
                            "pre_close": float(getattr(bar, "pre_close", 0.0) or 0.0),
                            "trade_time": str(bar.trade_time)[:10],
                        }
                    )
                for symbol, rows in grouped_rows.items():
                    frame = pd.DataFrame(rows).drop_duplicates(subset=["trade_time"]).sort_values("trade_time")
                    if frame.empty:
                        continue
                    frame.index = frame["trade_time"]
                    replay_price_data[symbol] = frame
            drift_report = live_drift_tracker.record_from_replay(
                trade_date=trade_date,
                live_pnl_pct=float(latest_report.avg_next_day_close_pct or 0.0),
                evaluation_records=replay_candidate_records,
                price_data=replay_price_data,
                execution_quality_report=execution_quality_report,
                report_trade_date=latest_report.trade_date,
                report_score_date=latest_report.score_date,
                config_overrides={
                    "execution_quality_report_path": str(settings.storage_root / "execution" / "quality_tracker.json"),
                },
                fallback_backtest_pnl_pct=(sum(backtest_returns) / len(backtest_returns) if backtest_returns else 0.0),
            )
            drift_cause = dict(drift_report.get("cause_breakdown") or {})
            if str(drift_cause.get("mode") or "") == "proxy_fallback":
                drift_cause["mode"] = (
                    "proxy_from_attribution_and_evaluation_ledger" if backtest_returns else "proxy_without_backtest_sample"
                )
                drift_cause.setdefault("fallback_reason", "no_replayable_records")
                drift_cause["backtest_sample_count"] = len(backtest_returns)
                drift_cause["matched_reconciliation_count"] = int(payload.get("matched", 0) or 0)
                drift_report["cause_breakdown"] = drift_cause
            payload["live_backtest_drift_report"] = drift_report
            meeting_state_store.set("latest_execution_reconciliation", payload)
            meeting_state_store.set("latest_live_backtest_drift_report", drift_report)
            circuit_breaker.record_success("execution_reconciliation", payload)
            record_audit(
                "execution",
                "完成执行对账",
                {
                    "account_id": resolved_account_id,
                    "matched": payload.get("matched", 0),
                    "unmatched_linux": len(list(payload.get("unmatched_linux") or [])),
                    "unmatched_qmt": len(list(payload.get("unmatched_qmt") or [])),
                    "status_mismatch": len(list(payload.get("status_mismatch") or [])),
                    "playbook_priority_updates": len(applied_updates),
                    "live_backtest_drift_report": drift_report,
                },
            )
        except Exception as exc:
            circuit_breaker.record_failure(
                "execution_reconciliation",
                str(exc),
                cached_result=(meeting_state_store.get("latest_execution_reconciliation", {}) or {}),
            )
            logger.warning("执行对账任务失败: %s", exc)

    runner.register("data.freshness:check", task_data_freshness_check)
    runner.register("execution.bridge_guardian:check", task_bridge_guardian)
    runner.register("execution.stale_order:cleanup", task_stale_order_cleanup)
    runner.register("execution.reconciliation:run", task_execution_bridge_reconciliation)
    runner.register("sentiment.calculator:compute_daily", task_compute_daily)
    runner.register("sentiment.calculator:pre_market", task_compute_daily)
    runner.register("sentiment.calculator:midday_snapshot", task_compute_daily)
    runner.register("position.watch:fast_realtime", task_fast_position_watch)
    runner.register("position.watch:check_realtime", task_position_watch)
    runner.register("monitor.market_watcher:check_once", task_check_once)
    runner.register("data.fetcher:fetch_news", task_fetch_news)
    runner.register("data.fetcher:fetch_daily", task_fetch_daily)
    runner.register("history.ingest:daily", task_history_ingest_daily)
    runner.register("history.ingest:minute", task_history_ingest_minute)
    runner.register("history.ingest:behavior_profiles", task_history_ingest_behavior_profiles)
    runner.register("strategy.stock_profile:refresh", task_refresh_behavior_profiles)
    runner.register("governance.parameter_hints:inspection", task_parameter_hint_inspection)
    runner.register("risk.stress_test:run", task_position_stress_test)
    runner.register("strategy.evaluation_ledger:reconcile_backtest", task_reconcile_backtest)
    runner.register("strategy.factor_monitor:refresh", task_factor_effectiveness_refresh)
    runner.register("report.daily:generate", task_daily_report)
    runner.register("strategy.screener:run_pipeline", task_run_pipeline)
    runner.register("strategy.screener:refresh", task_run_pipeline)
    runner.register("strategy.buy_decision:generate_buy_list", task_run_pipeline)
    runner.register("strategy.buy_decision:execute_open", task_execute_open)
    runner.register("execution.window:advance", task_execution_window_followthrough)
    runner.register("strategy.buy_decision:pre_confirm", task_pre_confirm)
    runner.register("strategy.auction:scan_0920", task_auction_0920)
    runner.register("strategy.auction:scan_0924", task_auction_0924)
    runner.register("execution.reverse_repo:auto_repurchase", task_reverse_repo_repurchase)
    runner.register("strategy.sell_decision:tail_market", task_tail_market)
    runner.register("monitor.market_watcher:check_micro", task_check_micro)
    runner.register("supervision.agent:check", task_agent_supervision)
    runner.register("autonomy.agent:runtime_bootstrap", task_agent_runtime_autonomy)
    runner.register("learning.score_state:daily_settlement", task_daily_settlement)
    runner.register("learning.prompt_patcher:daily_patch", task_prompt_patch)
    runner.register("learning.registry_updater:update_weights", task_registry_update)
    runner.register("learning.self_evolve:suggest", task_self_evolve)
    runner.register("learning.continuous:validate", task_continuous_validate)
    runner.register("strategy.nightly_sandbox:run", task_nightly_sandbox)
    runner.register("factors.engine:compute_all", task_factor_compute)
    runner.register("monitor.dragon_tiger:analyze", task_dragon_tiger)

    try:
        runner.start()
    except (KeyboardInterrupt, SystemExit):
        runner.shutdown()
