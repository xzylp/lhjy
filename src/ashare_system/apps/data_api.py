"""数据 serving API。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter

from ..data.serving import ServingStore
from ..settings import AppSettings


def _resource_entry(
    *,
    resource: str,
    description: str,
    latest_endpoint: str,
    latest_payload: dict[str, Any] | None,
    storage_path: str,
    recommended_for: list[str],
    fetch_when: str,
) -> dict[str, Any]:
    payload = latest_payload or {}
    return {
        "resource": resource,
        "description": description,
        "latest_endpoint": latest_endpoint,
        "storage_path": storage_path,
        "available": bool(latest_payload),
        "trade_date": payload.get("trade_date"),
        "generated_at": payload.get("generated_at"),
        "expires_at": payload.get("expires_at"),
        "recommended_for": recommended_for,
        "fetch_when": fetch_when,
    }


def build_router(settings: AppSettings) -> APIRouter:
    router = APIRouter(prefix="/data", tags=["data"])
    serving_store = ServingStore(settings.storage_root)

    @router.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "data-serving",
            "environment": settings.environment,
            "timestamp": datetime.now().isoformat(),
        }

    @router.get("/catalog")
    async def get_data_catalog():
        latest_market_context = serving_store.get_latest_market_context()
        latest_event_context = serving_store.get_latest_event_context()
        latest_symbol_contexts = serving_store.get_latest_symbol_contexts()
        latest_dossier_pack = serving_store.get_latest_dossier_pack()
        latest_discussion_context = serving_store.get_latest_discussion_context()
        latest_monitor_context = serving_store.get_latest_monitor_context()
        latest_runtime_context = serving_store.get_latest_runtime_context()
        latest_workspace_context = serving_store.get_latest_workspace_context()
        layout = serving_store.layout

        resources = [
            _resource_entry(
                resource="market_context",
                description="大盘、指数、市场结构的统一上下文。",
                latest_endpoint="/data/market-context/latest",
                latest_payload=latest_market_context,
                storage_path=str(layout.serving_root / "latest_market_context.json"),
                recommended_for=["ashare", "ashare-strategy", "ashare-risk", "ashare-audit"],
                fetch_when="需要判断大盘环境、风险偏好、指数背景时优先读取。",
            ),
            _resource_entry(
                resource="event_context",
                description="新闻、公告、政策等事件聚合上下文。",
                latest_endpoint="/data/event-context/latest",
                latest_payload=latest_event_context,
                storage_path=str(layout.serving_root / "latest_event_context.json"),
                recommended_for=["ashare", "ashare-research", "ashare-risk", "ashare-audit"],
                fetch_when="需要识别最近催化、风险事件、公告冲击时优先读取。",
            ),
            _resource_entry(
                resource="symbol_contexts",
                description="候选股票的标准化个股上下文索引。",
                latest_endpoint="/data/symbol-contexts/latest",
                latest_payload=latest_symbol_contexts,
                storage_path=str(layout.serving_root / "latest_symbol_contexts.json"),
                recommended_for=["ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"],
                fetch_when="需要按 symbol 批量浏览候选池上下文时优先读取。",
            ),
            _resource_entry(
                resource="dossiers",
                description="候选股票统一 dossier 包，包含 runtime、symbol_context、event_context、research 等证据。",
                latest_endpoint="/data/dossiers/latest",
                latest_payload=latest_dossier_pack,
                storage_path=str(layout.serving_root / "latest_dossier_pack.json"),
                recommended_for=["ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"],
                fetch_when="会议讨论、候选解释、最终汇总时优先读取。",
            ),
            _resource_entry(
                resource="discussion_context",
                description="会议统一视图，汇总 cycle、shared_context、reply_pack、final_brief、client_brief。",
                latest_endpoint="/data/discussion-context/latest",
                latest_payload=latest_discussion_context,
                storage_path=str(layout.serving_root / "latest_discussion_context.json"),
                recommended_for=["ashare", "ashare-audit", "monitor-ui"],
                fetch_when="需要回看最新会议结论、监控展示或给主控复用统一摘要时优先读取。",
            ),
            _resource_entry(
                resource="monitor_context",
                description="盯盘统一视图，汇总心跳、池层快照、事件数和轮询状态。",
                latest_endpoint="/data/monitor-context/latest",
                latest_payload=latest_monitor_context,
                storage_path=str(layout.serving_root / "latest_monitor_context.json"),
                recommended_for=["monitor-ui", "ashare", "ashare-runtime", "ashare-risk"],
                fetch_when="需要查看最新盯盘状态、心跳新鲜度和池层快照时优先读取。",
            ),
            _resource_entry(
                resource="runtime_context",
                description="运行统一视图，汇总最新 runtime job、候选结果、模式、账户和报告位置。",
                latest_endpoint="/data/runtime-context/latest",
                latest_payload=latest_runtime_context,
                storage_path=str(layout.serving_root / "latest_runtime_context.json"),
                recommended_for=["ashare", "ashare-runtime", "ashare-strategy", "ashare-audit"],
                fetch_when="需要回看最新选股任务、候选生成结果和 runtime 模式时优先读取。",
            ),
            _resource_entry(
                resource="workspace_context",
                description="总览统一视图，汇总 runtime、discussion、monitor、dossier、market、event 当前状态。",
                latest_endpoint="/data/workspace-context/latest",
                latest_payload=latest_workspace_context,
                storage_path=str(layout.serving_root / "latest_workspace_context.json"),
                recommended_for=["main", "ashare", "monitor-ui"],
                fetch_when="需要先看全局态势，再决定下钻到哪个上下文时优先读取。",
            ),
        ]

        return {
            "status": "ok",
            "catalog_version": "v1",
            "storage_root": str(settings.storage_root),
            "generated_at": datetime.now().isoformat(),
            "preferred_read_order": [
                "先读取 /system/discussions/agent-packets 获取会议统一包。",
                "若需了解数据位置、最新时间戳和推荐入口，读取 /data/catalog。",
                "若统一包证据不足，再补读 /data/market-context/latest、/data/event-context/latest、/data/symbol-contexts/latest、/data/dossiers/latest。",
                "若 serving 层仍不足，再按职责调用 research/runtime/market 等接口或允许的外部工具补充事实源。",
            ],
            "resources": resources,
            "storage_domains": {
                "db_root": str(layout.db_root),
                "lake_root": str(layout.lake_root),
                "state_root": str(layout.state_root),
                "reports_root": str(layout.reports_root),
                "raw_market_symbol_root": str(layout.raw_market_symbol_root),
                "raw_market_index_root": str(layout.raw_market_index_root),
                "raw_market_structure_root": str(layout.raw_market_structure_root),
                "raw_events_news_root": str(layout.raw_events_news_root),
                "raw_events_announcements_root": str(layout.raw_events_announcements_root),
                "raw_events_policy_root": str(layout.raw_events_policy_root),
                "normalized_events_root": str(layout.normalized_events_root),
                "features_market_context_root": str(layout.features_market_context_root),
                "features_symbol_context_root": str(layout.features_symbol_context_root),
                "features_event_context_root": str(layout.features_event_context_root),
                "features_dossiers_root": str(layout.features_dossiers_root),
                "features_discussion_context_root": str(layout.features_discussion_context_root),
                "features_monitor_context_root": str(layout.features_monitor_context_root),
                "features_runtime_context_root": str(layout.features_runtime_context_root),
                "features_workspace_context_root": str(layout.features_workspace_context_root),
                "serving_root": str(layout.serving_root),
            },
            "agent_usage": {
                "meeting_mode": [
                    "讨论前先看统一 packet，不要四个子代理各自拼上下文。",
                    "引用证据时优先使用标准化字段，并带上 trade_date、generated_at 或 expires_at。",
                    "若 packet 中缺关键证据，可以继续补取行情、研究、新闻或外部事实源，但必须标明来源和时间。",
                ],
                "roles": {
                    "ashare-research": ["event_context", "dossiers", "symbol_contexts"],
                    "ashare-strategy": ["dossiers", "market_context", "symbol_contexts"],
                    "ashare-risk": ["market_context", "event_context", "dossiers"],
                    "ashare-audit": ["dossiers", "event_context", "market_context"],
                },
            },
        }

    @router.get("/market-context/latest")
    async def get_latest_market_context():
        payload = serving_store.get_latest_market_context()
        return payload or {"available": False, "resource": "market_context"}

    @router.get("/event-context/latest")
    async def get_latest_event_context():
        payload = serving_store.get_latest_event_context()
        return payload or {"available": False, "resource": "event_context"}

    @router.get("/symbol-contexts/latest")
    async def get_latest_symbol_contexts():
        payload = serving_store.get_latest_symbol_contexts()
        return payload or {"available": False, "resource": "symbol_contexts"}

    @router.get("/dossiers/latest")
    async def get_latest_dossiers():
        payload = serving_store.get_latest_dossier_pack()
        return payload or {"available": False, "resource": "dossiers"}

    @router.get("/discussion-context/latest")
    async def get_latest_discussion_context():
        payload = serving_store.get_latest_discussion_context()
        return payload or {"available": False, "resource": "discussion_context"}

    @router.get("/monitor-context/latest")
    async def get_latest_monitor_context():
        payload = serving_store.get_latest_monitor_context()
        return payload or {"available": False, "resource": "monitor_context"}

    @router.get("/runtime-context/latest")
    async def get_latest_runtime_context():
        payload = serving_store.get_latest_runtime_context()
        return payload or {"available": False, "resource": "runtime_context"}

    @router.get("/workspace-context/latest")
    async def get_latest_workspace_context():
        payload = serving_store.get_latest_workspace_context()
        return payload or {"available": False, "resource": "workspace_context"}

    @router.get("/dossiers/{trade_date}/{symbol}")
    async def get_dossier(trade_date: str, symbol: str):
        payload = serving_store.get_dossier(trade_date, symbol)
        return payload or {"available": False, "trade_date": trade_date, "symbol": symbol}

    @router.get("/symbol-contexts/{trade_date}/{symbol}")
    async def get_symbol_context(trade_date: str, symbol: str):
        payload = serving_store.get_symbol_context(trade_date, symbol)
        return payload or {"available": False, "trade_date": trade_date, "symbol": symbol}

    return router
