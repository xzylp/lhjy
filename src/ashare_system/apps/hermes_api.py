"""Hermes 控制平台通用能力接口。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..data.catalog_service import CatalogService
from ..data.control_db import ControlPlaneDB
from ..data.history_store import HistoryStore
from ..data.serving import ServingStore
from ..hermes.model_router import HermesModelRouter
from ..infra.audit_store import AuditStore, StateStore
from ..learning.score_state import AgentScoreService
from ..monitor.persistence import MonitorStateService
from ..settings import AppSettings


PROMPT_ROLE_META: dict[str, dict[str, str]] = {
    "runtime_scout": {"title": "运行侦察", "category": "profile", "group": "monitoring", "desc": "盯运行事实、runtime 口味和候选刷新。"},
    "event_researcher": {"title": "事件研究", "category": "profile", "group": "research", "desc": "追踪新闻、公告、政策与事件催化。"},
    "strategy_analyst": {"title": "策略分析", "category": "profile", "group": "strategy", "desc": "组织因子、战法、约束与组合口味。"},
    "risk_gate": {"title": "风控闸门", "category": "profile", "group": "risk", "desc": "负责 allow / limit / reject 的结构化风控判断。"},
    "audit_recorder": {"title": "审计记录", "category": "profile", "group": "governance", "desc": "记录归因、复盘与治理线索。"},
    "execution_operator": {"title": "执行操作", "category": "profile", "group": "execution", "desc": "负责执行预检、派发与回执跟踪。"},
    "cron_preopen_readiness": {"title": "盘前就绪", "category": "schedule", "group": "cron", "desc": "盘前准备与待办确认。"},
    "cron_intraday_watch": {"title": "盘中巡检", "category": "schedule", "group": "cron", "desc": "盘中异动、候选和市场变化巡检。"},
    "cron_position_watch": {"title": "持仓巡视", "category": "schedule", "group": "cron", "desc": "持仓、替换与做T窗口监视。"},
    "cron_postclose_learning": {"title": "盘后学习", "category": "schedule", "group": "cron", "desc": "盘后归因、学习与治理。"},
    "cron_nightly_sandbox": {"title": "夜间沙盘", "category": "schedule", "group": "cron", "desc": "夜间沙盘推演与次日预案。"},
}


class HermesCreateSessionInput(BaseModel):
    title: str = Field(default="新建 Hermes 会话")
    profile_id: str = Field(default="runtime_scout")
    model_id: str = Field(default="workspace-default")


class HermesAppendMessageInput(BaseModel):
    session_id: str
    role: str = Field(default="user")
    content: str


class HermesCommandInput(BaseModel):
    command: str
    session_id: str = Field(default="")


def build_router(
    *,
    settings: AppSettings,
    audit_store: AuditStore | None,
    runtime_state_store: StateStore | None,
    research_state_store: StateStore | None,
    meeting_state_store: StateStore | None,
    agent_score_service: AgentScoreService | None,
    monitor_state_service: MonitorStateService | None,
) -> APIRouter:
    router = APIRouter(prefix="/system/hermes", tags=["hermes"])
    serving_store = ServingStore(settings.storage_root)
    control_plane_db = ControlPlaneDB(settings.control_plane_db_path)
    history_catalog = CatalogService(control_plane_db)
    history_store = HistoryStore(settings.storage_root, control_plane_db, history_catalog)
    model_router = HermesModelRouter(settings.hermes)
    prompts_dir = settings.workspace / "hermes" / "prompts"
    scheduler_path = settings.workspace / "src" / "ashare_system" / "scheduler.py"

    def _now() -> str:
        return datetime.now().isoformat()

    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip())

    def _display_label(value: Any) -> str:
        text = str(value or "").strip()
        mapping = {
            "active": "运行中",
            "ready": "就绪",
            "warning": "告警",
            "unknown": "未知",
            "on": "开启",
            "off": "关闭",
            "live": "实盘",
            "dry-run": "演练",
            "dry_run": "演练",
            "sim": "模拟",
            "mock": "模拟",
        }
        return mapping.get(text, text or "--")

    def _read_markdown_summary(path: Path) -> tuple[str, str]:
        if not path.exists():
            return path.stem, ""
        lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
        title = path.stem
        summary = ""
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip() or title
                continue
            summary = stripped
            break
        return title, summary

    def _load_prompt_items() -> list[dict[str, Any]]:
        if not prompts_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(prompts_dir.glob("*.md")):
            stem = path.stem
            if stem == "README":
                continue
            title, summary = _read_markdown_summary(path)
            meta = PROMPT_ROLE_META.get(stem, {})
            items.append(
                {
                    "id": stem,
                    "name": meta.get("title") or title,
                    "category": meta.get("category") or "profile",
                    "group": meta.get("group") or "general",
                    "summary": meta.get("desc") or summary,
                    "path": str(path.relative_to(settings.workspace)),
                    "status": "active",
                    "source": "markdown_prompt",
                }
            )
        return items

    def _list_profiles() -> list[dict[str, Any]]:
        return [item for item in _load_prompt_items() if item.get("category") == "profile"]

    def _list_skills() -> list[dict[str, Any]]:
        skills: list[dict[str, Any]] = []
        for item in _load_prompt_items():
            skills.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "group": item["group"],
                    "status": item["status"],
                    "summary": item["summary"],
                    "path": item["path"],
                    "capability_type": "prompt_contract" if item["category"] == "profile" else "schedule_prompt",
                }
            )
        return skills

    def _parse_scheduler_tasks() -> list[dict[str, Any]]:
        if not scheduler_path.exists():
            return []
        pattern = re.compile(r'ScheduledTask\(name="([^"]+)",\s*cron="([^"]+)",\s*handler="([^"]+)"')
        text = scheduler_path.read_text(encoding="utf-8")
        items: list[dict[str, Any]] = []
        for name, cron, handler in pattern.findall(text):
            items.append(
                {
                    "id": handler.replace(":", "."),
                    "name": name,
                    "cron": cron,
                    "handler": handler,
                    "status": "scheduled",
                    "source": "scheduler_registry",
                }
            )
        return items

    def _list_schedules() -> list[dict[str, Any]]:
        prompt_map = {item["id"]: item for item in _load_prompt_items() if item.get("category") == "schedule"}
        schedules = _parse_scheduler_tasks()
        cron_overlays = [
            {
                "id": item_id,
                "name": item["name"],
                "cron": "see_prompt_contract",
                "handler": f"hermes.prompt:{item_id}",
                "status": "contract_ready",
                "source": item["path"],
                "summary": item["summary"],
            }
            for item_id, item in prompt_map.items()
        ]
        return schedules + cron_overlays

    def _list_tools() -> list[dict[str, Any]]:
        return [
            {"id": "workspace", "name": "工作台", "category": "core", "method": "GET", "endpoint": "/system/hermes/workspace", "summary": "Hermes 工作台总览。"},
            {"id": "sessions", "name": "会话", "category": "core", "method": "GET/POST", "endpoint": "/system/hermes/sessions", "summary": "会话创建、恢复和状态读取。"},
            {"id": "profiles", "name": "角色", "category": "registry", "method": "GET", "endpoint": "/system/hermes/profiles", "summary": "角色配置与提示词合同。"},
            {"id": "models", "name": "模型", "category": "registry", "method": "GET", "endpoint": "/system/hermes/models", "summary": "模型槽位与别名配置。"},
            {"id": "model_resolve", "name": "模型路由", "category": "registry", "method": "GET", "endpoint": "/system/hermes/models/resolve", "summary": "按岗位、任务和风险级别解析实际模型槽位。"},
            {"id": "skills", "name": "技能", "category": "registry", "method": "GET", "endpoint": "/system/hermes/skills", "summary": "可复用能力模块与调度提示词。"},
            {"id": "memory", "name": "记忆", "category": "state", "method": "GET", "endpoint": "/system/hermes/memory", "summary": "会话记忆、市场假设和系统书签。"},
            {"id": "schedules", "name": "调度", "category": "automation", "method": "GET", "endpoint": "/system/hermes/schedules", "summary": "计划任务与 cron 编排入口。"},
            {"id": "activity", "name": "事件", "category": "state", "method": "GET", "endpoint": "/system/hermes/activity", "summary": "事件流、tool read / exec 与审计回放。"},
            {"id": "command_preview", "name": "指令预判", "category": "control", "method": "POST", "endpoint": "/system/hermes/command/preview", "summary": "在执行前解释指令会命中什么能力。"},
            {"id": "command_execute", "name": "指令执行", "category": "control", "method": "POST", "endpoint": "/system/hermes/command/execute", "summary": "执行只读控制指令并生成结构化回复。"},
            {"id": "ashare_overview", "name": "A股集成", "category": "integration", "method": "GET", "endpoint": "/system/hermes/integrations/ashare/overview", "summary": "挂载 A 股 runtime / supervision / execution / governance。"},
        ]

    def _read_sessions_payload() -> dict[str, Any]:
        if not meeting_state_store:
            return {"active_session_id": "", "items": []}
        payload = meeting_state_store.get("hermes_sessions", {}) or {}
        if not isinstance(payload, dict):
            return {"active_session_id": "", "items": []}
        return {
            "active_session_id": str(payload.get("active_session_id") or ""),
            "items": list(payload.get("items") or []),
        }

    def _write_sessions_payload(payload: dict[str, Any]) -> None:
        if meeting_state_store:
            meeting_state_store.set("hermes_sessions", payload)

    def _append_activity(kind: str, title: str, detail: str, payload: dict[str, Any] | None = None) -> None:
        if not meeting_state_store:
            return
        events = list(meeting_state_store.get("hermes_activity_events", []) or [])
        events.append(
            {
                "event_id": f"hermes-{uuid4().hex[:10]}",
                "kind": kind,
                "title": title,
                "detail": detail,
                "payload": payload or {},
                "created_at": _now(),
            }
        )
        meeting_state_store.set("hermes_activity_events", events[-300:])

    def _ensure_default_session() -> dict[str, Any]:
        payload = _read_sessions_payload()
        items = list(payload.get("items") or [])
        if items:
            return payload
        now = _now()
        default_session = {
            "session_id": "workspace-default",
            "title": "新对话",
            "profile_id": "runtime_scout",
            "model_id": "workspace-default",
            "kind": "workspace",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "messages": [
                {
                    "message_id": f"msg-{uuid4().hex[:8]}",
                    "role": "assistant",
                    "content": "Hermes 已就绪。你可以查询状态、角色、技能、调度、网关，或直接查看 A 股控制面集成状态。",
                    "created_at": now,
                }
            ],
        }
        payload = {"active_session_id": default_session["session_id"], "items": [default_session]}
        _write_sessions_payload(payload)
        return payload

    def _list_sessions() -> list[dict[str, Any]]:
        payload = _ensure_default_session()
        sessions: list[dict[str, Any]] = []
        for item in list(payload.get("items") or []):
            messages = list(item.get("messages") or [])
            sessions.append(
                {
                    "session_id": item.get("session_id"),
                    "title": item.get("title"),
                    "profile_id": item.get("profile_id"),
                    "model_id": item.get("model_id"),
                    "status": item.get("status"),
                    "kind": item.get("kind"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "message_count": len(messages),
                    "last_message_preview": _normalize_text(messages[-1]["content"])[:120] if messages else "",
                }
            )
        return sessions

    def _get_session(session_id: str) -> dict[str, Any] | None:
        payload = _ensure_default_session()
        for item in list(payload.get("items") or []):
            if str(item.get("session_id") or "") == session_id:
                return item
        return None

    def _save_session(updated_session: dict[str, Any], *, active: bool = False) -> dict[str, Any]:
        payload = _ensure_default_session()
        items = list(payload.get("items") or [])
        replaced = False
        for index, item in enumerate(items):
            if str(item.get("session_id") or "") == str(updated_session.get("session_id") or ""):
                items[index] = updated_session
                replaced = True
                break
        if not replaced:
            items.insert(0, updated_session)
        payload["items"] = items[:50]
        if active:
            payload["active_session_id"] = str(updated_session.get("session_id") or "")
        _write_sessions_payload(payload)
        return updated_session

    def _append_message(session_id: str, role: str, content: str) -> dict[str, Any]:
        session = _get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        messages = list(session.get("messages") or [])
        messages.append(
            {
                "message_id": f"msg-{uuid4().hex[:8]}",
                "role": role,
                "content": content,
                "created_at": _now(),
            }
        )
        session["messages"] = messages[-200:]
        session["updated_at"] = _now()
        _save_session(session, active=True)
        return session

    def _latest_runtime_context() -> dict[str, Any]:
        return serving_store.get_latest_runtime_context() or (runtime_state_store.get("latest_runtime_context", {}) if runtime_state_store else {}) or {}

    def _latest_workspace_context() -> dict[str, Any]:
        return serving_store.get_latest_workspace_context() or {}

    def _latest_discussion_context() -> dict[str, Any]:
        return serving_store.get_latest_discussion_context() or {}

    def _latest_monitor_context() -> dict[str, Any]:
        return serving_store.get_latest_monitor_context() or {}

    def _latest_bridge_health() -> dict[str, Any]:
        if not monitor_state_service:
            return {}
        return dict((monitor_state_service.get_latest_execution_bridge_health() or {}).get("health") or {})

    def _build_history_overview() -> dict[str, Any]:
        latest_daily = dict(runtime_state_store.get("latest_history_daily_ingest", {}) or {}) if runtime_state_store else {}
        latest_minute = dict(runtime_state_store.get("latest_history_minute_ingest", {}) or {}) if runtime_state_store else {}
        latest_behavior = dict(runtime_state_store.get("latest_history_behavior_profile_ingest", {}) or {}) if runtime_state_store else {}
        capabilities = history_store.capabilities()
        catalog_snapshot = history_catalog.build_health_snapshot()
        summary_lines = [
            (
                f"日线入湖最近作业 {latest_daily.get('row_count', 0)} 行 / "
                f"{latest_daily.get('ingested_symbol_count', 0) or latest_daily.get('symbol_count', 0)} 只。"
            ),
            (
                f"分钟线入湖最近作业 {latest_minute.get('row_count', 0)} 行 / {latest_minute.get('symbol_count', 0)} 只，"
                f"样本池 {len(list(latest_minute.get('symbols') or []))} 只。"
            ),
            (
                f"股性画像最近作业 {latest_behavior.get('row_count', 0)} 条 / {latest_behavior.get('symbol_count', 0)} 只，"
                f"Parquet={'on' if capabilities.get('parquet_enabled') else 'off'} DuckDB={'on' if capabilities.get('duckdb_enabled') else 'off'}。"
            ),
        ]
        return {
            "capabilities": capabilities,
            "catalog": catalog_snapshot,
            "latest_daily": latest_daily,
            "latest_minute": latest_minute,
            "latest_behavior_profiles": latest_behavior,
            "summary_lines": summary_lines,
        }

    def _build_ashare_overview() -> dict[str, Any]:
        runtime_context = _latest_runtime_context()
        workspace_context = _latest_workspace_context()
        discussion_context = _latest_discussion_context()
        latest_dispatch = dict(meeting_state_store.get("latest_execution_dispatch", {}) or {}) if meeting_state_store else {}
        latest_precheck = dict(meeting_state_store.get("latest_execution_precheck", {}) or {}) if meeting_state_store else {}
        latest_reconciliation = dict(meeting_state_store.get("latest_execution_reconciliation", {}) or {}) if meeting_state_store else {}
        latest_review_board = dict(meeting_state_store.get("latest_review_board", {}) or {}) if meeting_state_store else {}
        bridge_health = _latest_bridge_health()
        history_overview = _build_history_overview()
        score_states = agent_score_service.list_scores() if agent_score_service else []
        return {
            "trade_date": str(
                runtime_context.get("trade_date")
                or workspace_context.get("trade_date")
                or discussion_context.get("trade_date")
                or datetime.now().date().isoformat()
            ),
            "runtime": {
                "available": bool(runtime_context),
                "summary_lines": list(runtime_context.get("summary_lines") or []),
                "endpoint": "/runtime/capabilities",
            },
            "supervision": {
                "available": bool(_latest_monitor_context() or latest_review_board),
                "summary_lines": list((latest_review_board or {}).get("summary_lines") or []),
                "endpoint": "/system/agents/supervision-board",
            },
            "discussion": {
                "available": bool(discussion_context),
                "summary_lines": list(discussion_context.get("summary_lines") or []),
                "endpoint": "/system/discussions/meeting-context",
            },
            "execution": {
                "dispatch_status": str(latest_dispatch.get("status") or "unknown"),
                "precheck_status": str(latest_precheck.get("status") or "unknown"),
                "reconciliation_status": str(latest_reconciliation.get("status") or "unknown"),
                "bridge_status": str(bridge_health.get("overall_status") or "unknown"),
                "bridge_path": str(bridge_health.get("bridge_path") or ""),
                "dispatch_endpoint": "/system/discussions/execution-dispatch/latest",
                "precheck_endpoint": "/system/discussions/execution-precheck",
            },
            "governance": {
                "score_count": len(score_states),
                "top_agents": [
                    {
                        "agent_id": item.agent_id,
                        "new_score": item.new_score,
                        "elo_rating": item.elo_rating,
                        "rating_tier": item.rating_tier,
                    }
                    for item in score_states[:4]
                ],
                "endpoint": "/system/agent-scores",
            },
            "history": {
                "available": True,
                "summary_lines": list(history_overview.get("summary_lines") or []),
                "latest_daily": history_overview.get("latest_daily"),
                "latest_minute": history_overview.get("latest_minute"),
                "latest_behavior_profiles": history_overview.get("latest_behavior_profiles"),
                "capabilities": history_overview.get("capabilities"),
                "endpoint": "/system/search/catalog",
            },
        }

    def _build_workspace() -> dict[str, Any]:
        sessions = _list_sessions()
        active_session_id = _read_sessions_payload().get("active_session_id") or (sessions[0]["session_id"] if sessions else "")
        ashare_overview = _build_ashare_overview()
        workspace_context = _latest_workspace_context()
        runtime_context = _latest_runtime_context()
        bridge_health = _latest_bridge_health()
        summary_lines = [
            "Hermes 是通用控制平台；A 股系统作为集成能力挂载在平台内。",
            f"当前运行模式 {_display_label(settings.run_mode)}，实盘开关 {_display_label('on' if settings.live_trade_enabled else 'off')}。",
        ]
        if bridge_health:
            summary_lines.append(f"执行桥状态 {_display_label(bridge_health.get('overall_status', 'unknown'))}。")
        if runtime_context:
            summary_lines.append("runtime 最新上下文已可用，可直接进入 A 股集成面查看。")
        if workspace_context.get("summary_lines"):
            summary_lines.extend([str(item) for item in list(workspace_context.get("summary_lines") or [])[:2]])
        return {
            "title": "Hermes 控制平台",
            "subtitle": "统一会话、角色、工具、记忆、调度与业务集成控制面",
            "generated_at": _now(),
            "run_mode": settings.run_mode,
            "live_enabled": settings.live_trade_enabled,
            "service_port": settings.service.port,
            "active_session_id": active_session_id,
            "summary_lines": summary_lines,
            "counts": {
                "sessions": len(sessions),
                "profiles": len(_list_profiles()),
                "skills": len(_list_skills()),
                "tools": len(_list_tools()),
                "schedules": len(_list_schedules()),
            },
            "entrypoints": {
                "chat": "/dashboard/hermes/chat",
                "sessions": "/dashboard/hermes/sessions",
                "profiles": "/dashboard/hermes/profiles",
                "models": "/dashboard/hermes/models",
                "skills": "/dashboard/hermes/skills",
                "memory": "/dashboard/hermes/memory",
                "tools": "/dashboard/hermes/tools",
                "schedules": "/dashboard/hermes/schedules",
                "gateway": "/dashboard/hermes/gateway",
                "settings": "/dashboard/hermes/settings",
            },
            "integrations": [
                {
                    "id": "ashare",
                    "name": "A 股 Agent 交易系统",
                    "status": ashare_overview["execution"]["bridge_status"] or ("ready" if ashare_overview["runtime"]["available"] else "warning"),
                    "summary": "运行态、讨论、监督、执行、治理已挂载为 Hermes 业务域。",
                    "endpoint": "/system/hermes/integrations/ashare/overview",
                }
            ],
            "ashare_overview": ashare_overview,
        }

    def _build_memory() -> dict[str, Any]:
        sessions = _list_sessions()
        active_session = _get_session(str(_read_sessions_payload().get("active_session_id") or ""))
        runtime_context = _latest_runtime_context()
        latest_dispatch = dict(meeting_state_store.get("latest_execution_dispatch", {}) or {}) if meeting_state_store else {}
        latest_review_board = dict(meeting_state_store.get("latest_review_board", {}) or {}) if meeting_state_store else {}
        bookmarks = [
            {"id": "runtime_context", "label": "最新 runtime 上下文", "available": bool(runtime_context), "path": "/data/runtime-context/latest"},
            {"id": "execution_dispatch", "label": "最新执行派发", "available": bool(latest_dispatch), "path": "/system/discussions/execution-dispatch/latest"},
            {"id": "review_board", "label": "最新复盘看板", "available": bool(latest_review_board), "path": "/system/reports/postclose/review-board"},
        ]
        recent_commands: list[str] = []
        if active_session:
            for message in reversed(list(active_session.get("messages") or [])):
                if str(message.get("role") or "") == "user":
                    recent_commands.append(_normalize_text(str(message.get("content") or "")))
                if len(recent_commands) >= 6:
                    break
        return {
            "generated_at": _now(),
            "active_session_id": active_session.get("session_id") if active_session else "",
            "recent_commands": recent_commands,
            "bookmarks": bookmarks,
            "workspace_notes": list((_latest_workspace_context().get("summary_lines") or [])[:6]),
            "runtime_notes": list((runtime_context.get("summary_lines") or [])[:6]),
            "review_notes": list((latest_review_board.get("summary_lines") or [])[:6]),
            "session_count": len(sessions),
        }

    def _build_gateway() -> dict[str, Any]:
        bridge_health = _latest_bridge_health()
        return {
            "generated_at": _now(),
            "go_platform": {
                "enabled": settings.go_platform.enabled,
                "base_url": settings.go_platform.base_url,
                "timeout_sec": settings.go_platform.timeout_sec,
            },
            "windows_gateway": {
                "base_url": settings.windows_gateway.base_url,
                "timeout_sec": settings.windows_gateway.timeout_sec,
                "token_configured": bool(settings.windows_gateway.token or settings.windows_gateway.token_file),
            },
            "feishu": {
                "control_plane_base_url": settings.notify.feishu_control_plane_base_url,
                "chat_id_configured": bool(settings.notify.feishu_chat_id),
                "important_chat_id_configured": bool(settings.notify.feishu_important_chat_id),
                "supervision_chat_id_configured": bool(settings.notify.feishu_supervision_chat_id),
            },
            "bridge_health": bridge_health,
            "service_units": [
                {"id": "openclaw", "unit": "openclaw-gateway.service"},
                {"id": "feishu_longconn", "unit": "ashare-feishu-longconn.service"},
                {"id": "control_plane", "unit": "ashare-system-v2.service"},
                {"id": "scheduler", "unit": "ashare-scheduler.service"},
            ],
        }

    def _build_settings() -> dict[str, Any]:
        model_slots = model_router.list_slots()
        return {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "run_mode": settings.run_mode,
            "live_trade_enabled": settings.live_trade_enabled,
            "market_mode": settings.market_mode,
            "execution_mode": settings.execution_mode,
            "execution_plane": settings.execution_plane,
            "workspace": str(settings.workspace),
            "storage_root": str(settings.storage_root),
            "logs_dir": str(settings.logs_dir),
            "service": {"host": settings.service.host, "port": settings.service.port},
            "hermes_model_policy": {
                "routing_policy": settings.hermes.routing_policy,
                "providers": [
                    {
                        "provider_id": "minimax",
                        "provider_name": settings.hermes.minimax_provider_name,
                        "model": settings.hermes.minimax_model,
                        "base_url_configured": bool(settings.hermes.minimax_base_url),
                        "credential_configured": bool(settings.hermes.minimax_api_key),
                        "assigned_slots": [item["id"] for item in model_slots if item.get("provider_id") == "minimax"],
                    },
                    {
                        "provider_id": "compat-gpt54",
                        "provider_name": settings.hermes.compat_provider_name,
                        "model": settings.hermes.compat_model,
                        "base_url_configured": bool(settings.hermes.compat_base_url),
                        "credential_configured": bool(settings.hermes.compat_api_key),
                        "assigned_slots": [item["id"] for item in model_slots if item.get("provider_id") == "compat-gpt54"],
                    },
                ],
            },
        }

    def _build_activity(limit: int = 40) -> dict[str, Any]:
        hermes_events = list(meeting_state_store.get("hermes_activity_events", []) or []) if meeting_state_store else []
        records: list[dict[str, Any]] = []
        for item in reversed(hermes_events[-limit:]):
            records.append(
                {
                    "event_id": item.get("event_id"),
                    "kind": item.get("kind"),
                    "title": item.get("title"),
                    "detail": item.get("detail"),
                    "created_at": item.get("created_at"),
                    "source": "hermes",
                }
            )
        if audit_store:
            for audit in audit_store.recent(limit=max(limit // 2, 8)):
                category = str(audit.category or "")
                audit_created_at = ""
                if hasattr(audit, "created_at"):
                    audit_created_at = str(getattr(audit, "created_at") or "")
                elif hasattr(audit, "timestamp"):
                    audit_created_at = str(getattr(audit, "timestamp") or "")
                elif isinstance(audit.payload, dict):
                    audit_created_at = str(audit.payload.get("created_at") or audit.payload.get("timestamp") or "")
                records.append(
                    {
                        "event_id": audit.audit_id,
                        "kind": "tool_read" if category in {"runtime", "monitor", "research"} else "tool_exec",
                        "title": audit.message,
                        "detail": f"category={category}",
                        "created_at": audit_created_at,
                        "source": "audit",
                    }
                )
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"generated_at": _now(), "items": records[:limit]}

    def _resolve_command(command: str) -> dict[str, Any]:
        text = _normalize_text(command).lower()
        if not text:
            return {"intent_id": "help", "label": "帮助", "target": "workspace", "safe": True, "summary": "查看工作台能力入口"}
        matchers = [
            (("状态", "overview", "workspace", "总览", "status"), "workspace", "工作台总览"),
            (("session", "会话", "聊天"), "sessions", "会话与聊天"),
            (("profile", "角色"), "profiles", "角色配置"),
            (("model", "模型"), "models", "模型槽位"),
            (("skill", "技能"), "skills", "能力模块"),
            (("memory", "记忆"), "memory", "记忆与书签"),
            (("tool", "工具"), "tools", "工具目录"),
            (("schedule", "cron", "调度", "计划"), "schedules", "调度与计划"),
            (("gateway", "网关", "bridge"), "gateway", "网关与桥接"),
            (("setting", "配置"), "settings", "平台设置"),
            (("ashare", "a股", "执行", "监督", "runtime"), "ashare_overview", "A股业务集成"),
        ]
        for keywords, target, label in matchers:
            if any(keyword in text for keyword in keywords):
                return {"intent_id": target, "label": label, "target": target, "safe": True, "summary": f"查询 {label}"}
        return {"intent_id": "help", "label": "帮助", "target": "help", "safe": True, "summary": "未识别明确目标，返回帮助与入口"}

    def _execute_command(command: str) -> dict[str, Any]:
        resolved = _resolve_command(command)
        target = resolved["target"]
        if target == "workspace":
            payload = _build_workspace()
            answer = "Hermes 当前已接好会话、角色、工具、调度和 A 股业务域，优先看工作台总览与右侧集成摘要。"
        elif target == "sessions":
            payload = {"active_session_id": _read_sessions_payload().get("active_session_id"), "items": _list_sessions()}
            answer = f"当前共有 {len(payload['items'])} 个会话，可继续在当前活动会话内发控制指令。"
        elif target == "profiles":
            payload = {"items": _list_profiles()}
            answer = f"当前可用角色合同 {len(payload['items'])} 份，已从 hermes/prompts 真实读取。"
        elif target == "models":
            payload = {"items": model_router.list_slots()}
            answer = "模型页展示的是 Hermes 当前可执行的槽位策略，不再只是展示别名。"
        elif target == "skills":
            payload = {"items": _list_skills()}
            answer = f"当前列出的技能/提示词合同共 {len(payload['items'])} 个，包含角色合同和 cron 合同。"
        elif target == "memory":
            payload = _build_memory()
            answer = "记忆页会优先展示最近会话命令、系统书签和运行摘要，方便恢复上下文。"
        elif target == "tools":
            payload = {"items": _list_tools()}
            answer = f"当前 Hermes 平台暴露 {len(payload['items'])} 个通用与集成工具入口。"
        elif target == "schedules":
            payload = {"items": _list_schedules()}
            answer = f"当前可见调度项 {len(payload['items'])} 个，既包括正式 scheduler，也包括 Hermes cron 合同。"
        elif target == "gateway":
            payload = _build_gateway()
            answer = "网关页展示 Go 平台、Windows 网关、飞书与执行桥健康信息。"
        elif target == "settings":
            payload = _build_settings()
            answer = "设置页展示的是控制平台当前有效配置快照，不包含敏感明文。"
        elif target == "ashare_overview":
            payload = _build_ashare_overview()
            answer = "A 股集成页会统一回显 runtime、discussion、supervision、execution 和 governance 口径。"
        else:
            payload = {
                "recommended_commands": [
                    "查看状态",
                    "查看角色",
                    "查看技能",
                    "查看工具",
                    "查看调度",
                    "查看网关",
                    "查看 A股 集成",
                ]
            }
            answer = "这条指令没有命中明确能力域，我先返回 Hermes 的常用入口。"
        return {"resolved": resolved, "payload": payload, "answer": answer}

    @router.get("/workspace")
    async def get_workspace():
        return {"ok": True, **_build_workspace()}

    @router.get("/sessions")
    async def list_sessions():
        payload = _ensure_default_session()
        return {"ok": True, "active_session_id": payload.get("active_session_id"), "items": _list_sessions()}

    @router.post("/sessions")
    async def create_session(payload: HermesCreateSessionInput):
        now = _now()
        item = {
            "session_id": f"session-{uuid4().hex[:10]}",
            "title": payload.title.strip() or "新对话",
            "profile_id": payload.profile_id,
            "model_id": payload.model_id,
            "kind": "workspace",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        _save_session(item, active=True)
        _append_activity("session_opened", "新建会话", item["title"], {"session_id": item["session_id"]})
        return {"ok": True, "item": item}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        session = _get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        return {"ok": True, "item": session}

    @router.post("/messages")
    async def append_message(payload: HermesAppendMessageInput):
        session = _append_message(payload.session_id, payload.role, payload.content)
        _append_activity("message", "会话消息", _normalize_text(payload.content)[:80], {"session_id": payload.session_id, "role": payload.role})
        return {"ok": True, "item": session}

    @router.get("/profiles")
    async def get_profiles():
        items = _list_profiles()
        return {"ok": True, "items": items, "count": len(items)}

    @router.get("/models")
    async def get_models():
        items = model_router.list_slots()
        return {"ok": True, "items": items, "count": len(items)}

    @router.get("/models/resolve")
    async def resolve_model(role: str = "main", task_kind: str = "chat", risk_level: str = "medium", prefer_fast: bool = False, require_deep_reasoning: bool = False):
        return {
            "ok": True,
            **model_router.resolve(
                role=role,
                task_kind=task_kind,
                risk_level=risk_level,
                prefer_fast=prefer_fast,
                require_deep_reasoning=require_deep_reasoning,
            ),
        }

    @router.get("/skills")
    async def get_skills():
        items = _list_skills()
        return {"ok": True, "items": items, "count": len(items)}

    @router.get("/memory")
    async def get_memory():
        return {"ok": True, **_build_memory()}

    @router.get("/tools")
    async def get_tools():
        items = _list_tools()
        return {"ok": True, "items": items, "count": len(items)}

    @router.get("/schedules")
    async def get_schedules():
        items = _list_schedules()
        return {"ok": True, "items": items, "count": len(items)}

    @router.get("/gateway")
    async def get_gateway():
        return {"ok": True, **_build_gateway()}

    @router.get("/settings")
    async def get_settings_snapshot():
        return {"ok": True, **_build_settings()}

    @router.get("/activity")
    async def get_activity(limit: int = 40):
        return {"ok": True, **_build_activity(limit=max(min(limit, 100), 1))}

    @router.post("/command/preview")
    async def preview_command(payload: HermesCommandInput):
        resolved = _resolve_command(payload.command)
        _append_activity("command_preview", "指令预判", resolved["label"], {"command": payload.command, "target": resolved["target"]})
        return {"ok": True, "command": payload.command, "resolved": resolved}

    @router.post("/command/execute")
    async def execute_command(payload: HermesCommandInput):
        session_id = payload.session_id.strip() or str(_ensure_default_session().get("active_session_id") or "workspace-default")
        _append_message(session_id, "user", payload.command)
        result = _execute_command(payload.command)
        _append_message(session_id, "assistant", result["answer"])
        _append_activity("command_execute", "执行指令", result["resolved"]["label"], {"command": payload.command, "target": result["resolved"]["target"]})
        return {
            "ok": True,
            "session_id": session_id,
            "resolved": result["resolved"],
            "answer": result["answer"],
            "payload": result["payload"],
        }

    @router.get("/integrations/ashare/overview")
    async def get_ashare_overview():
        return {"ok": True, **_build_ashare_overview()}

    return router
