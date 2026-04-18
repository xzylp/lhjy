"""调度器 — APScheduler 真实调度，盘前/盘中/盘后时间表"""

from __future__ import annotations

import importlib
import json
import httpx
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .contracts import (
    EventFetchResult,
    ExitContext,
    MarketEvent,
    PlaceOrderRequest,
    PlaybookOverrideSnapshot,
    PositionSnapshot,
    QuoteSnapshot,
    SectorProfile,
    StockBehaviorProfile,
)
from .data.archive import DataArchiveStore
from .data.contracts import EventRecord
from .data.freshness import build_freshness_meta
from .data.serving import ServingStore
from .execution_gateway import (
    EXECUTION_GATEWAY_PENDING_PATH,
    enqueue_execution_gateway_intent,
    resolve_execution_gateway_state_store,
)
from .execution_safety import is_limit_up, is_trading_session
from .governance.inspection import collect_parameter_hint_inspection
from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore
from .infra.market_adapter import MarketDataAdapter
from .learning.attribution import TradeAttributionService
from .logging_config import get_logger
from .notify.monitor_changes import MonitorChangeNotifier
from .notify.templates import agent_supervision_template, execution_order_event_template
from .precompute import DossierPrecomputeService
from .reverse_repo import ReverseRepoService
from .settings import AppSettings
from .strategy.sell_decision import PositionState, SellDecisionEngine
from .supervision_state import annotate_supervision_payload, record_supervision_notification
from .supervision_tasks import build_agent_task_plan, record_agent_task_dispatch

logger = get_logger("scheduler")
PLAYBOOK_OVERRIDE_STORAGE_FILE = Path("learning") / "playbook_overrides.json"
AGENT_AUTONOMY_CONTROL_KEY_PREFIX = "agent_autonomy_control:"


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
    ScheduledTask(name="竞价快照09:20", cron="20 9 * * 1-5", handler="strategy.auction:scan_0920"),
    ScheduledTask(name="竞价快照09:24", cron="24 9 * * 1-5", handler="strategy.auction:scan_0924"),
    ScheduledTask(name="逆回购开盘回补", cron="35 9 * * 1-5", handler="execution.reverse_repo:auto_repurchase"),
]

# 盘中任务 (09:30 - 15:00)
INTRADAY_TASKS = [
    ScheduledTask(name="开盘执行",    cron="30 9 * * 1-5",  handler="strategy.buy_decision:execute_open"),
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
    ScheduledTask(name="因子计算",     cron="45 15 * * 1-5", handler="factors.engine:compute_all"),
    ScheduledTask(name="日终复盘",     cron="0 16 * * 1-5",  handler="report.daily:generate"),
    ScheduledTask(name="学分结算",     cron="30 16 * * *",   handler="learning.score_state:daily_settlement"),
    ScheduledTask(name="Prompt进化",   cron="45 16 * * *",   handler="learning.prompt_patcher:daily_patch"),
    ScheduledTask(name="龙虎榜分析",   cron="0 17 * * *",    handler="monitor.dragon_tiger:analyze"),
    ScheduledTask(name="注册表权重覆写", cron="0 17 * * *",  handler="learning.registry_updater:update_weights"),
    ScheduledTask(name="策略自进化",   cron="15 17 * * *",   handler="learning.self_evolve:suggest"),
    ScheduledTask(name="股性画像刷新", cron="30 17 * * *",   handler="strategy.stock_profile:refresh"),
    ScheduledTask(name="增量学习回放", cron="30 17 * * *",   handler="learning.continuous:validate"),
    ScheduledTask(name="参数治理巡检", cron="0 18 * * *",    handler="governance.parameter_hints:inspection"),
    ScheduledTask(name="次日新闻扫描", cron="0 20 * * *",    handler="data.fetcher:fetch_news"),
    ScheduledTask(name="账本回测验证", cron="30 20 * * 1-5", handler="strategy.evaluation_ledger:reconcile_backtest"),
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
    event_context: dict | None = None,
    execution_plane: str | None = None,
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
    event_context = _build_intraday_event_context(settings, event_context)
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
        "intraday_rank_result": intraday_rank_result,
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
                day=parts[2],
                month=parts[3],
                day_of_week=_normalize_crontab_day_of_week(parts[4]),
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
        get_agent_score_service,
        get_candidate_case_service,
        get_discussion_cycle_service,
        get_execution_adapter,
        get_learned_asset_service,
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
    from .data.auction_fetcher import AuctionFetcher
    from .data.event_bus import EventBus
    from .data.serving import ServingStore
    from .data.fetcher import DataFetcher, DataPipeline
    from .data.special import SpecialDataFetcher
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
    from .strategy.auction_engine import AuctionEngine
    from .strategy.nightly_sandbox import NightlySandbox

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
    archive_store = DataArchiveStore(settings.storage_root)
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

    def _resolve_trade_date_from_contexts() -> str:
        return (
            str((serving_store.get_latest_runtime_context() or {}).get("trade_date") or "")
            or str((serving_store.get_latest_discussion_context() or {}).get("trade_date") or "")
            or date.today().isoformat()
        )

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
        record_audit(
            "monitor",
            "完成微观节奏巡检",
            {
                "trade_date": _resolve_trade_date_from_contexts(),
                "event_bus_price_alert_count": len(meeting_state_store.get("event_bus_price_alerts", []) if meeting_state_store else []),
            },
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
            discussion_context=(
                meeting_state_store.get("latest_discussion_context", {})
                if meeting_state_store
                else {}
            ),
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
        try:
            universe = fetcher.fetch_universe("main-board")[:200]
            from .strategy.screener import StockScreener
            screener = StockScreener()
            blocked_symbols = _build_blocked_symbols_from_events(serving_store.get_latest_event_context() or {})
            
            # 降级为基础样本生成，不再执行因子/AI/环境打分
            result = screener.run_base_sample(
                universe,
                runtime_config=config_mgr.get(),
                blocked_symbols=blocked_symbols,
                top_n=60,
            )
            
            trade_date = date.today().isoformat()
            nightly_priority_boosts = _load_nightly_priority_boosts(trade_date)
            
            # 基础打分：仅根据原始顺序（如成交额/热度）+ 夜间推演加分，不代入策略因子
            snapshots = fetcher.fetch_snapshots(result.passed[:30])
            snapshot_map = {item.symbol: item for item in snapshots}
            score_map = {
                symbol: float(max(len(result.passed) - index, 1)) + float(nightly_priority_boosts.get(symbol, 0.0) or 0.0)
                for index, symbol in enumerate(result.passed)
            }
            name_map = {
                symbol: (snapshot_map[symbol].name if symbol in snapshot_map and snapshot_map[symbol].name else market.get_symbol_name(symbol))
                for symbol in result.passed
            }
            
            pool_mgr.update(result.passed, score_map, names=name_map)
            ranked_symbols = sorted(result.passed, key=lambda item: score_map.get(item, 0.0), reverse=True)
            
            runtime_snapshot = {
                "job_id": f"scheduler-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "job_type": "scheduler_base_sample",
                "generated_at": datetime.now().isoformat(),
                "selected_symbols": ranked_symbols,
                "decision_count": len(ranked_symbols),
                "nightly_priority_boosts": nightly_priority_boosts,
                "summary_lines": [
                    f"调度器已生成基础样本池 (n={len(ranked_symbols)})。",
                    "注意：当前结果未经过策略打分，仅供讨论素材使用。",
                    "提示：Agent 应组织参数并发起 /runtime/jobs/compose 以获取经过策略编排的候选排名。",
                ],
                "top_picks": [
                    {
                        "symbol": symbol,
                        "name": name_map[symbol],
                        "rank": index + 1,
                        "selection_score": score_map[symbol],
                        "action": "BUY",
                        "summary": f"{name_map[symbol] or symbol} 进入基础样本池，等待 Agent 组织 compose 参数发起深度扫描。",
                        "score_breakdown": {
                            "base_rank_score": score_map[symbol],
                            "nightly_priority_boost": float(nightly_priority_boosts.get(symbol, 0.0) or 0.0),
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
                    for index, symbol in enumerate(ranked_symbols)
                ],
            }
            sync_payload = persist_scheduler_runtime_snapshot(runtime_snapshot, source="scheduler_base_sample")
            record_audit(
                "runtime",
                "调度器完成基础样本生成",
                {
                    **runtime_snapshot,
                    **sync_payload,
                },
            )
        except Exception as e:
            logger.warning("基础样本生成失败: %s", e)

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
        record_audit(
            "execution",
            "开盘执行任务进入纸面阻断",
            {"run_mode": settings.run_mode, "latest_job_id": latest.get("job_id"), "execution_mode": settings.execution_mode},
        )

    def task_pre_confirm():
        latest = runtime_state.get("latest_runtime_report", {})
        record_audit("risk", "完成买入清单预确认", {"selected_symbols": latest.get("selected_symbols", [])})

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
        discussion_context = (
            serving_store.get_latest_discussion_context()
            or meeting_state_store.get("latest_discussion_context", {})
            or {}
        )
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
        if trade_date and discussion_cycle_service:
            existing_cycle = discussion_cycle_service.get_cycle(trade_date)
            if existing_cycle is None and cases:
                try:
                    existing_cycle = discussion_cycle_service.bootstrap_cycle(trade_date)
                except Exception:
                    logger.exception("监督巡检 bootstrap discussion cycle 失败: trade_date=%s", trade_date)
            if existing_cycle is not None:
                try:
                    cycle = discussion_cycle_service.refresh_cycle(trade_date)
                except Exception:
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
        if meeting_state_store:
            history = meeting_state_store.get("agent_supervision_history", [])
            history.append(payload)
            meeting_state_store.set("latest_agent_supervision", payload)
            meeting_state_store.set("agent_supervision_history", history[-100:])
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
        control_plane_base_url = (
            str(getattr(settings.service, "public_base_url", "") or "").strip()
            or f"http://{settings.service.host}:{settings.service.port}"
        ).rstrip("/")
        endpoint_map = {
            "ashare-research": "/system/discussions/opinions/research-writeback",
            "ashare-risk": "/system/discussions/opinions/risk-writeback",
            "ashare-audit": "/system/discussions/opinions/audit-writeback",
        }
        with httpx.Client(timeout=20.0) as client:
            for agent_id in ("ashare-strategy", "ashare-research", "ashare-risk", "ashare-audit"):
                agent_state = dict(control_state.get(agent_id) or {})
                last_attempt_at = str(agent_state.get("last_attempt_at") or "").strip() or None
                last_attempt_dt = None
                if last_attempt_at:
                    try:
                        last_attempt_dt = datetime.fromisoformat(last_attempt_at)
                    except ValueError:
                        last_attempt_dt = None
                if last_attempt_dt is not None and (now - last_attempt_dt).total_seconds() < 600:
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
                        latest_record = dict((compose_panel.get("items") or [{}])[0] or {})
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

    runner.register("sentiment.calculator:compute_daily", task_compute_daily)
    runner.register("sentiment.calculator:pre_market", task_compute_daily)
    runner.register("sentiment.calculator:midday_snapshot", task_compute_daily)
    runner.register("monitor.market_watcher:check_once", task_check_once)
    runner.register("data.fetcher:fetch_news", task_fetch_news)
    runner.register("data.fetcher:fetch_daily", task_fetch_daily)
    runner.register("strategy.stock_profile:refresh", task_refresh_behavior_profiles)
    runner.register("governance.parameter_hints:inspection", task_parameter_hint_inspection)
    runner.register("strategy.evaluation_ledger:reconcile_backtest", task_reconcile_backtest)
    runner.register("report.daily:generate", task_daily_report)
    runner.register("strategy.screener:run_pipeline", task_run_pipeline)
    runner.register("strategy.screener:refresh", task_run_pipeline)
    runner.register("strategy.buy_decision:generate_buy_list", task_run_pipeline)
    runner.register("strategy.buy_decision:execute_open", task_execute_open)
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
