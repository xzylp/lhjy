"""策略原子仓库 - 因子/战法/模板/学习产物的注册、分区与状态流转。"""

from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..logging_config import get_logger

logger = get_logger("strategy.atomic_repository")

StrategyAssetType = Literal["factor", "playbook", "template", "learned_combo"]
StrategyAssetStatus = Literal[
    "active",
    "experimental",
    "learned",
    "archived",
    "draft",
    "review_required",
    "rejected",
]
StrategyRuntimeConsumeMode = Literal[
    "default",
    "explicit_only",
    "auto_or_explicit",
    "governance_only",
    "blocked",
]


class StrategyRepositoryEntry(BaseModel):
    """策略仓库中的单个资产定义。"""

    id: str
    name: str
    type: StrategyAssetType
    status: StrategyAssetStatus = "experimental"
    version: str = "v1"
    author: str = "system"
    source: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    params_schema: dict[str, Any] = Field(default_factory=dict)
    evidence_schema: dict[str, Any] = Field(default_factory=dict)
    evaluation_summary: dict[str, Any] = Field(default_factory=dict)
    risk_notes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    content: dict[str, Any] = Field(default_factory=dict)

    def repository_key(self) -> str:
        return f"{self.id}:{self.version}"

    def runtime_policy(self) -> dict[str, Any]:
        """返回 runtime 对该条目的主链消费策略。"""

        blocked_statuses = {"archived", "rejected"}
        if self.status in blocked_statuses:
            return {
                "mode": "blocked",
                "default_enabled": False,
                "explicit_enabled": False,
                "auto_selectable": False,
                "reason": f"status={self.status}，已下线或被拒绝，不可进入 runtime 主链",
            }

        if self.type in {"factor", "playbook"}:
            if self.status == "active":
                return {
                    "mode": "default",
                    "default_enabled": True,
                    "explicit_enabled": True,
                    "auto_selectable": False,
                    "reason": "active 因子/战法默认可进入 runtime 主链，也可被 Agent 显式调用",
                }
            return {
                "mode": "explicit_only",
                "default_enabled": False,
                "explicit_enabled": True,
                "auto_selectable": False,
                "reason": f"status={self.status}，不参与默认主链，仅允许 Agent 显式试验或评审使用",
            }

        if self.type in {"learned_combo", "template"}:
            if self.status == "active":
                return {
                    "mode": "auto_or_explicit",
                    "default_enabled": False,
                    "explicit_enabled": True,
                    "auto_selectable": True,
                    "reason": "active 学习产物不默认混入主链，但可被 Agent 显式引用或由 auto_apply_active 自动吸附",
                }
            return {
                "mode": "governance_only",
                "default_enabled": False,
                "explicit_enabled": False,
                "auto_selectable": False,
                "reason": f"status={self.status}，当前仅处于治理/审批阶段，不应参与 runtime 加权",
            }

        return {
            "mode": "blocked",
            "default_enabled": False,
            "explicit_enabled": False,
            "auto_selectable": False,
            "reason": f"未识别的资产类型={self.type}，默认阻断",
        }


class StrategyRepository:
    """线程安全的策略原子仓库。"""

    def __init__(self) -> None:
        self._entries: dict[str, StrategyRepositoryEntry] = {}
        self._lock = Lock()

    def register(self, entry: StrategyRepositoryEntry) -> StrategyRepositoryEntry:
        key = entry.repository_key()
        with self._lock:
            if key in self._entries:
                raise ValueError(f"策略仓库条目已存在: {key}")
            self._entries[key] = entry.model_copy()
        logger.info("策略仓库注册: %s status=%s type=%s", key, entry.status, entry.type)
        return entry.model_copy()

    def get(self, asset_id: str, version: str | None = None) -> StrategyRepositoryEntry | None:
        with self._lock:
            if version is not None:
                entry = self._entries.get(f"{asset_id}:{version}")
                return entry.model_copy() if entry else None

            matched = [entry for entry in self._entries.values() if entry.id == asset_id]
            if not matched:
                return None
            matched.sort(key=lambda item: (item.created_at, item.version), reverse=True)
            return matched[0].model_copy()

    def list_entries(
        self,
        *,
        type: StrategyAssetType | None = None,
        status: StrategyAssetStatus | None = None,
        author: str | None = None,
    ) -> list[StrategyRepositoryEntry]:
        with self._lock:
            items = list(self._entries.values())
        if type is not None:
            items = [item for item in items if item.type == type]
        if status is not None:
            items = [item for item in items if item.status == status]
        if author is not None:
            items = [item for item in items if item.author == author]
        items.sort(key=lambda item: (item.updated_at, item.id, item.version), reverse=True)
        return [item.model_copy() for item in items]

    def version_view(
        self,
        *,
        type: StrategyAssetType | None = None,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self.list_entries(type=type)
        if asset_id is not None:
            normalized_asset_id = str(asset_id).strip()
            items = [item for item in items if item.id == normalized_asset_id]

        grouped: dict[tuple[str, str], list[StrategyRepositoryEntry]] = {}
        for item in items:
            grouped.setdefault((item.type, item.id), []).append(item)

        result: list[dict[str, Any]] = []
        for (entry_type, entry_id), versions in grouped.items():
            versions.sort(key=lambda item: (item.updated_at, item.version), reverse=True)
            default_candidates = [item for item in versions if bool(item.runtime_policy().get("default_enabled"))]
            explicit_candidates = [item for item in versions if bool(item.runtime_policy().get("explicit_enabled"))]
            blocked_candidates = [item for item in versions if str(item.runtime_policy().get("mode") or "") == "blocked"]
            recommended_entry = default_candidates[0] if default_candidates else (explicit_candidates[0] if explicit_candidates else versions[0])
            result.append(
                {
                    "type": entry_type,
                    "id": entry_id,
                    "name": versions[0].name,
                    "version_count": len(versions),
                    "recommended_version": recommended_entry.version,
                    "recommended_runtime_mode": recommended_entry.runtime_policy().get("mode"),
                    "default_versions": [item.version for item in default_candidates],
                    "explicit_candidate_versions": [item.version for item in explicit_candidates],
                    "blocked_versions": [item.version for item in blocked_candidates],
                    "versions": [
                        {
                            "version": item.version,
                            "status": item.status,
                            "updated_at": item.updated_at,
                            "runtime_policy": item.runtime_policy(),
                        }
                        for item in versions
                    ],
                }
            )
        result.sort(key=lambda item: (item["type"], item["id"]))
        return result

    def set_status(self, asset_id: str, version: str, status: StrategyAssetStatus) -> StrategyRepositoryEntry:
        with self._lock:
            key = f"{asset_id}:{version}"
            entry = self._entries.get(key)
            if entry is None:
                raise KeyError(f"未找到策略仓库条目: {key}")
            updated = entry.model_copy(update={"status": status, "updated_at": datetime.now().isoformat()})
            self._entries[key] = updated
        logger.info("策略仓库状态更新: %s -> %s", key, status)
        return updated.model_copy()

    def update_entry(self, entry: StrategyRepositoryEntry) -> StrategyRepositoryEntry:
        key = entry.repository_key()
        with self._lock:
            if key not in self._entries:
                raise KeyError(f"未找到策略仓库条目: {key}")
            updated = entry.model_copy(update={"updated_at": datetime.now().isoformat()})
            self._entries[key] = updated
        logger.info("策略仓库条目更新: %s", key)
        return updated.model_copy()

    def submit_learned_entry(
        self,
        *,
        asset_id: str,
        name: str,
        asset_type: Literal["playbook", "template", "learned_combo"],
        author: str,
        source: str,
        content: dict[str, Any],
        version: str = "v1",
        params_schema: dict[str, Any] | None = None,
        evidence_schema: dict[str, Any] | None = None,
        risk_notes: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> StrategyRepositoryEntry:
        entry = StrategyRepositoryEntry(
            id=asset_id,
            name=name,
            type=asset_type,
            status="draft",
            version=version,
            author=author,
            source=source,
            params_schema=params_schema or {},
            evidence_schema=evidence_schema or {},
            risk_notes=risk_notes or [],
            tags=tags or [],
            content=content,
        )
        return self.register(entry)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._entries.values())
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for item in items:
            by_status[item.status] = by_status.get(item.status, 0) + 1
            by_type[item.type] = by_type.get(item.type, 0) + 1
        return {
            "total": len(items),
            "by_status": by_status,
            "by_type": by_type,
        }


strategy_atomic_repository = StrategyRepository()
