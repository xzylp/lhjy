"""研究 API - 研究事件写入与摘要同步"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..data.archive import DataArchiveStore
from ..data.contracts import EventRecord
from ..data.freshness import build_freshness_meta
from ..infra.audit_store import AuditStore, StateStore
from ..settings import AppSettings

SentimentLabel = Literal["positive", "neutral", "negative"]
ImpactScope = Literal["market", "sector", "symbol", "macro", "unknown"]


class ResearchEventInput(BaseModel):
    symbol: str = ""
    name: str = ""
    title: str
    summary: str
    sentiment: SentimentLabel = "neutral"
    source: str
    source_type: str = ""
    severity: str = "info"
    impact_scope: ImpactScope = "unknown"
    evidence_url: str = ""
    dedupe_key: str = ""
    tags: list[str] = Field(default_factory=list)
    event_time: str | None = None


class ResearchEventBatch(BaseModel):
    items: list[ResearchEventInput] = Field(default_factory=list)


class ResearchSyncRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)


def build_router(
    settings: AppSettings,
    audit_store: AuditStore,
    research_state_store: StateStore,
) -> APIRouter:
    router = APIRouter(prefix="/research", tags=["research"])
    archive_store = DataArchiveStore(settings.storage_root)

    def _load_events(event_type: str) -> list[dict]:
        return research_state_store.get(event_type, [])

    def _save_events(event_type: str, items: list[dict]) -> None:
        research_state_store.set(event_type, items[-200:])

    def _append_events(event_type: str, payload: ResearchEventInput | ResearchEventBatch) -> list[dict]:
        events = payload.items if isinstance(payload, ResearchEventBatch) else [payload]
        stored = _load_events(event_type)
        now = datetime.now().isoformat()
        existing_by_key = {
            _event_dedupe_key(item): item
            for item in stored
        }
        new_records: list[EventRecord] = []
        for item in events:
            event_time = item.event_time or now
            freshness = build_freshness_meta(
                source_at=event_time,
                fetched_at=now,
                generated_at=now,
                fresh_seconds=300 if event_type == "news" else 86400,
                warm_seconds=3600 if event_type == "news" else 86400 * 7,
                expiry_seconds=300 if event_type == "news" else 86400,
            )
            normalized = EventRecord(
                event_id=f"{event_type}-{uuid4().hex[:10]}",
                symbol=item.symbol,
                name=item.name,
                source=item.source,
                source_type=item.source_type or event_type,
                category=event_type,
                title=item.title,
                summary=item.summary,
                severity=item.severity,
                sentiment=item.sentiment,
                event_at=event_time,
                recorded_at=now,
                dedupe_key=item.dedupe_key or _build_dedupe_key(event_type, item),
                impact_scope=item.impact_scope,
                evidence_url=item.evidence_url,
                payload={"tags": item.tags},
                **freshness.model_dump(),
            )
            existing_by_key[normalized.dedupe_key] = normalized.model_dump()
            new_records.append(normalized)
        stored = list(existing_by_key.values())
        _save_events(event_type, stored)
        archive_store.persist_event_records(event_type, new_records)
        return [item.model_dump() for item in new_records]

    def _build_dedupe_key(event_type: str, item: ResearchEventInput) -> str:
        symbol = item.symbol.strip().upper() if item.symbol else ""
        title = item.title.strip().lower()
        source = item.source.strip().lower()
        event_time = (item.event_time or "").strip()
        return f"{event_type}|{symbol}|{source}|{title}|{event_time}"

    def _event_dedupe_key(item: dict) -> str:
        return str(
            item.get("dedupe_key")
            or f"{item.get('category') or item.get('event_type')}|{item.get('symbol','')}|{item.get('source','')}|{item.get('title','')}|{item.get('event_at') or item.get('event_time','')}"
        )

    def _build_summary(symbols: list[str] | None = None) -> dict:
        news = _load_events("news")
        announcements = _load_events("announcements")
        policy = _load_events("policy")
        allowed = set(symbols or [])
        all_items = news + announcements + policy
        if allowed:
            all_items = [item for item in all_items if item["symbol"] in allowed]
            news = [item for item in news if item["symbol"] in allowed]
            announcements = [item for item in announcements if item["symbol"] in allowed]
            policy = [item for item in policy if item["symbol"] in allowed]
        unique_symbols = sorted({item["symbol"] for item in all_items})
        return {
            "symbols": unique_symbols,
            "news_count": len(news),
            "announcement_count": len(announcements),
            "policy_count": len(policy),
            "event_titles": [item["title"] for item in sorted(all_items, key=lambda item: item.get("recorded_at", ""), reverse=True)[:10]],
            "latest_news": news[-5:],
            "latest_announcements": announcements[-5:],
            "latest_policy": policy[-5:],
            "updated_at": datetime.now().isoformat(),
        }

    @router.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "research",
            "environment": settings.environment,
            "timestamp": datetime.now().isoformat(),
        }

    @router.post("/sync")
    async def sync_research(request: ResearchSyncRequest):
        summary = _build_summary(request.symbols)
        sync_payload = {
            "symbols": request.symbols,
            "requested_at": datetime.now().isoformat(),
            "news_count": summary["news_count"],
            "announcement_count": summary["announcement_count"],
        }
        history = research_state_store.get("sync_history", [])
        history.append(sync_payload)
        research_state_store.set("sync_history", history[-50:])
        research_state_store.set("summary", summary)
        audit_store.append(
            category="research",
            message="研究摘要同步完成",
            payload=sync_payload,
        )
        return {"status": "ok", **summary}

    @router.post("/events/news")
    async def write_news(payload: ResearchEventInput | ResearchEventBatch):
        items = _append_events("news", payload)
        summary = _build_summary()
        research_state_store.set("summary", summary)
        audit_store.append(
            category="research",
            message=f"写入新闻事件 {len(items)} 条",
            payload={"symbols": sorted({item['symbol'] for item in items}), "event_titles": [item["title"] for item in items]},
        )
        return {
            "status": "ok",
            "event_type": "news",
            "count": len(items),
            "symbols": sorted({item["symbol"] for item in items}),
            "event_titles": [item["title"] for item in items],
        }

    @router.post("/events/policy")
    async def write_policy_events(payload: ResearchEventInput | ResearchEventBatch):
        items = _append_events("policy", payload)
        summary = _build_summary()
        research_state_store.set("summary", summary)
        audit_store.append(
            category="research",
            message=f"写入政策事件 {len(items)} 条",
            payload={"symbols": sorted({item['symbol'] for item in items}), "event_titles": [item["title"] for item in items]},
        )
        return {
            "status": "ok",
            "event_type": "policy",
            "count": len(items),
            "symbols": sorted({item["symbol"] for item in items}),
            "event_titles": [item["title"] for item in items],
        }

    @router.post("/events/announcements")
    async def write_announcements(payload: ResearchEventInput | ResearchEventBatch):
        items = _append_events("announcements", payload)
        summary = _build_summary()
        research_state_store.set("summary", summary)
        audit_store.append(
            category="research",
            message=f"写入公告事件 {len(items)} 条",
            payload={"symbols": sorted({item['symbol'] for item in items}), "event_titles": [item["title"] for item in items]},
        )
        return {
            "status": "ok",
            "event_type": "announcements",
            "count": len(items),
            "symbols": sorted({item["symbol"] for item in items}),
            "event_titles": [item["title"] for item in items],
        }

    @router.get("/summary")
    async def summary(symbol: str | None = None):
        return _build_summary([symbol] if symbol else None)

    return router
