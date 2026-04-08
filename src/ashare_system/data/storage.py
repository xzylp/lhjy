"""数据底座目录布局。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageLayout:
    """统一数据目录布局。"""

    root: Path
    raw_root: Path
    normalized_root: Path
    features_root: Path
    serving_root: Path
    cache_root: Path
    raw_market_symbol_root: Path
    raw_market_index_root: Path
    raw_market_structure_root: Path
    raw_events_news_root: Path
    raw_events_announcements_root: Path
    raw_events_policy_root: Path
    normalized_market_symbol_root: Path
    normalized_market_index_root: Path
    normalized_market_structure_root: Path
    normalized_events_root: Path
    features_market_context_root: Path
    features_symbol_context_root: Path
    features_event_context_root: Path
    features_dossiers_root: Path
    features_discussion_context_root: Path
    features_monitor_context_root: Path
    features_runtime_context_root: Path
    features_workspace_context_root: Path

    def ensure_directories(self) -> "StorageLayout":
        for path in self._all_directories():
            path.mkdir(parents=True, exist_ok=True)
        return self

    def _all_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.raw_root,
            self.normalized_root,
            self.features_root,
            self.serving_root,
            self.cache_root,
            self.raw_market_symbol_root,
            self.raw_market_index_root,
            self.raw_market_structure_root,
            self.raw_events_news_root,
            self.raw_events_announcements_root,
            self.raw_events_policy_root,
            self.normalized_market_symbol_root,
            self.normalized_market_index_root,
            self.normalized_market_structure_root,
            self.normalized_events_root,
            self.features_market_context_root,
            self.features_symbol_context_root,
            self.features_event_context_root,
            self.features_dossiers_root,
            self.features_discussion_context_root,
            self.features_monitor_context_root,
            self.features_runtime_context_root,
            self.features_workspace_context_root,
        )


def build_storage_layout(storage_root: Path) -> StorageLayout:
    raw_root = storage_root / "raw"
    normalized_root = storage_root / "normalized"
    features_root = storage_root / "features"
    serving_root = storage_root / "serving"
    cache_root = storage_root / "cache"
    return StorageLayout(
        root=storage_root,
        raw_root=raw_root,
        normalized_root=normalized_root,
        features_root=features_root,
        serving_root=serving_root,
        cache_root=cache_root,
        raw_market_symbol_root=raw_root / "market" / "symbol",
        raw_market_index_root=raw_root / "market" / "index",
        raw_market_structure_root=raw_root / "market" / "structure",
        raw_events_news_root=raw_root / "events" / "news",
        raw_events_announcements_root=raw_root / "events" / "announcements",
        raw_events_policy_root=raw_root / "events" / "policy",
        normalized_market_symbol_root=normalized_root / "market" / "symbol",
        normalized_market_index_root=normalized_root / "market" / "index",
        normalized_market_structure_root=normalized_root / "market" / "structure",
        normalized_events_root=normalized_root / "events",
        features_market_context_root=features_root / "market_context",
        features_symbol_context_root=features_root / "symbol_context",
        features_event_context_root=features_root / "event_context",
        features_dossiers_root=features_root / "dossiers",
        features_discussion_context_root=features_root / "discussion_context",
        features_monitor_context_root=features_root / "monitor_context",
        features_runtime_context_root=features_root / "runtime_context",
        features_workspace_context_root=features_root / "workspace_context",
    )


def ensure_storage_layout(storage_root: Path) -> StorageLayout:
    return build_storage_layout(storage_root).ensure_directories()
