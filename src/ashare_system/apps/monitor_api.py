"""监控查询 API"""

from __future__ import annotations

from fastapi import APIRouter

from ..data.serving import ServingStore
from ..monitor.stock_pool import StockPoolManager
from ..monitor.alert_engine import AlertEngine
from ..monitor.persistence import MonitorStateService
from ..settings import AppSettings


def build_router(
    pool_mgr: StockPoolManager,
    alert_engine: AlertEngine,
    monitor_state_service: MonitorStateService | None = None,
    settings: AppSettings | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/monitor", tags=["monitor"])
    serving_store = ServingStore(settings.storage_root) if settings else None

    def _build_serving_pool_payload(top_n: int) -> dict | None:
        if not serving_store:
            return None
        discussion_context = serving_store.get_latest_discussion_context()
        if discussion_context:
            reply_pack = discussion_context.get("reply_pack") or {}
            items = (
                (reply_pack.get("selected") or [])
                + (reply_pack.get("watchlist") or [])
                + (reply_pack.get("rejected") or [])
            )[:top_n]
            if items:
                return {
                    "symbols": [item["symbol"] for item in items],
                    "items": [
                        {
                            "symbol": item["symbol"],
                            "name": item.get("name", item["symbol"]),
                            "score": item.get("selection_score"),
                            "final_status": item.get("final_status"),
                            "risk_gate": item.get("risk_gate"),
                            "audit_gate": item.get("audit_gate"),
                        }
                        for item in items
                    ],
                    "date": discussion_context.get("trade_date"),
                    "total": discussion_context.get("case_count", len(items)),
                    "source": "serving_discussion_context",
                    "discussion_state": (discussion_context.get("cycle") or {}).get("discussion_state"),
                }
        dossier_pack = serving_store.get_latest_dossier_pack()
        if not dossier_pack:
            return None
        items = dossier_pack.get("items", [])[:top_n]
        return {
            "symbols": [item["symbol"] for item in items],
            "items": [
                {
                    "symbol": item["symbol"],
                    "name": item.get("name", item["symbol"]),
                    "score": item.get("selection_score"),
                    "final_status": item.get("final_status"),
                    "risk_gate": item.get("risk_gate"),
                    "audit_gate": item.get("audit_gate"),
                }
                for item in items
            ],
            "date": dossier_pack.get("trade_date"),
            "total": dossier_pack.get("symbol_count", len(dossier_pack.get("items", []))),
            "source": "serving_dossier",
        }

    @router.get("/pool")
    async def get_pool(top_n: int = 10):
        pool = pool_mgr.get()
        if pool is None:
            latest_pool_snapshot = (monitor_state_service.get_state().get("latest_pool_snapshot") if monitor_state_service else None)
            if not latest_pool_snapshot:
                serving_pool = _build_serving_pool_payload(top_n)
                return serving_pool or {"symbols": [], "date": None}
            items = latest_pool_snapshot.get("candidate_pool", [])[:top_n]
            return {
                "symbols": [item["symbol"] for item in items],
                "items": [{"symbol": item["symbol"], "name": item.get("name", item["symbol"]), "score": item.get("selection_score")} for item in items],
                "date": latest_pool_snapshot.get("trade_date"),
                "total": latest_pool_snapshot.get("counts", {}).get("candidate_pool", 0),
                "source": "monitor_state",
            }
        top_symbols = pool_mgr.get_top_n(top_n)
        return {
            "symbols": top_symbols,
            "items": [{"symbol": symbol, "name": pool.names.get(symbol, symbol), "score": pool.scores.get(symbol)} for symbol in top_symbols],
            "date": pool.date,
            "total": len(pool.symbols),
            "source": pool.source,
        }

    @router.get("/pool/layers")
    async def get_pool_layers():
        if not monitor_state_service:
            discussion_context = serving_store.get_latest_discussion_context() if serving_store else None
            if discussion_context:
                return discussion_context
            return {"available": False}
        latest = monitor_state_service.get_state().get("latest_pool_snapshot")
        return latest or (serving_store.get_latest_discussion_context() if serving_store else None) or {"available": False}

    @router.get("/discussion/latest")
    async def get_latest_discussion_view():
        if serving_store:
            payload = serving_store.get_latest_discussion_context()
            if payload:
                return payload
        return {"available": False}

    @router.get("/alerts/stats")
    async def alert_stats():
        if not monitor_state_service:
            monitor_context = serving_store.get_latest_monitor_context() if serving_store else None
            if monitor_context:
                return {
                    "event_count": monitor_context.get("event_count", 0),
                    "latest_heartbeat_at": (monitor_context.get("latest_heartbeat") or {}).get("generated_at"),
                    "recent_event_count": len(monitor_context.get("recent_events", [])),
                }
            return {"message": "告警统计功能待实现"}
        state = monitor_state_service.get_state(event_limit=1000)
        return {
            "event_count": state["event_count"],
            "latest_heartbeat_at": (state["latest_heartbeat"] or {}).get("generated_at"),
            "recent_event_count": len(state["recent_events"]),
        }

    @router.get("/state")
    async def get_monitor_state(event_limit: int = 20):
        if not monitor_state_service:
            monitor_context = serving_store.get_latest_monitor_context() if serving_store else None
            return monitor_context or {"available": False}
        return monitor_state_service.get_state(event_limit=event_limit)

    @router.get("/changes/summary")
    async def get_change_summary(event_limit: int = 20):
        if not monitor_state_service:
            return {"available": False}
        return monitor_state_service.build_change_summary(event_limit=event_limit)

    return router
