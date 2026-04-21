"""统一检索 API。"""

from __future__ import annotations

from fastapi import APIRouter

from ..data.catalog_service import CatalogService
from ..data.document_index import DocumentIndexService
from ..data.history_store import HistoryStore
from ..infra.audit_store import StateStore


def build_router(
    *,
    document_index: DocumentIndexService,
    catalog_service: CatalogService,
    history_store: HistoryStore | None = None,
    runtime_state_store: StateStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/system/search", tags=["search"])

    @router.get("/documents")
    async def search_documents(q: str = "", limit: int = 10, category: str = ""):
        items = document_index.search(q, limit=max(min(limit, 50), 1), category=category or None)
        return {
            "ok": True,
            "query": q,
            "count": len(items),
            "items": items,
            "stats": document_index.stats(),
        }

    @router.get("/catalog")
    async def search_catalog():
        latest_history_runtime = {
            "daily": (runtime_state_store.get("latest_history_daily_ingest", {}) if runtime_state_store else {}),
            "minute": (runtime_state_store.get("latest_history_minute_ingest", {}) if runtime_state_store else {}),
            "behavior_profiles": (
                runtime_state_store.get("latest_history_behavior_profile_ingest", {}) if runtime_state_store else {}
            ),
        }
        return {
            "ok": True,
            **catalog_service.build_health_snapshot(),
            "capabilities": history_store.capabilities() if history_store else {},
            "latest_history_runtime": latest_history_runtime,
        }

    return router
