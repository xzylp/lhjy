"""dataset catalog / partitions / ingestion run 服务。"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from uuid import uuid4

from .control_db import ControlPlaneDB


DEFAULT_DATASETS: list[dict[str, Any]] = [
    {
        "dataset_name": "bars_1d",
        "description": "A股日线历史底座",
        "storage_kind": "lake",
        "retention_policy": "full_history",
        "owner_module": "history_ingest",
        "primary_keys": ["symbol", "trade_time"],
        "partition_keys": ["trade_date"],
        "version": "v1",
    },
    {
        "dataset_name": "bars_1m",
        "description": "A股分钟线近端窗口",
        "storage_kind": "lake",
        "retention_policy": "rolling_120_trade_days",
        "owner_module": "history_ingest",
        "primary_keys": ["symbol", "trade_time"],
        "partition_keys": ["trade_date"],
        "version": "v1",
    },
    {
        "dataset_name": "stock_behavior_profiles",
        "description": "个股股性画像与摘要层",
        "storage_kind": "sqlite",
        "retention_policy": "latest_per_trade_date",
        "owner_module": "history_store",
        "primary_keys": ["symbol", "trade_date"],
        "partition_keys": ["trade_date"],
        "version": "v1",
    },
    {
        "dataset_name": "documents",
        "description": "项目文档与任务单全文检索",
        "storage_kind": "sqlite_fts5",
        "retention_policy": "workspace_snapshot",
        "owner_module": "document_index",
        "primary_keys": ["doc_id"],
        "partition_keys": ["category"],
        "version": "v1",
    },
]


def _now() -> str:
    return datetime.now().isoformat()


class CatalogService:
    """统一管理数据集目录、分区索引和 ingest 运行记录。"""

    def __init__(self, db: ControlPlaneDB) -> None:
        self.db = db

    def ensure_default_catalog(self) -> None:
        for item in DEFAULT_DATASETS:
            self.register_dataset(**item)

    def register_dataset(
        self,
        *,
        dataset_name: str,
        description: str,
        storage_kind: str,
        retention_policy: str,
        owner_module: str,
        primary_keys: list[str],
        partition_keys: list[str],
        version: str = "v1",
    ) -> None:
        now = _now()
        self.db.execute(
            """
            INSERT INTO dataset_catalog(
                dataset_name, description, storage_kind, retention_policy, owner_module,
                primary_keys, partition_keys, version, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_name) DO UPDATE SET
                description=excluded.description,
                storage_kind=excluded.storage_kind,
                retention_policy=excluded.retention_policy,
                owner_module=excluded.owner_module,
                primary_keys=excluded.primary_keys,
                partition_keys=excluded.partition_keys,
                version=excluded.version,
                updated_at=excluded.updated_at
            """,
            (
                dataset_name,
                description,
                storage_kind,
                retention_policy,
                owner_module,
                json.dumps(primary_keys, ensure_ascii=False),
                json.dumps(partition_keys, ensure_ascii=False),
                version,
                now,
                now,
            ),
        )

    def record_partition(
        self,
        *,
        dataset_name: str,
        period: str,
        trade_date: str,
        path: str,
        file_format: str,
        row_count: int,
        symbol_count: int,
        min_time: str = "",
        max_time: str = "",
        source: str = "",
        checksum: str = "",
        freshness_status: str = "fresh",
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        self.db.execute(
            """
            INSERT INTO dataset_partitions(
                dataset_name, period, trade_date, path, file_format, row_count, symbol_count,
                min_time, max_time, source, checksum, freshness_status, created_at, updated_at, extra_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_name, period, trade_date, path) DO UPDATE SET
                file_format=excluded.file_format,
                row_count=excluded.row_count,
                symbol_count=excluded.symbol_count,
                min_time=excluded.min_time,
                max_time=excluded.max_time,
                source=excluded.source,
                checksum=excluded.checksum,
                freshness_status=excluded.freshness_status,
                updated_at=excluded.updated_at,
                extra_json=excluded.extra_json
            """,
            (
                dataset_name,
                period,
                trade_date,
                path,
                file_format,
                int(row_count or 0),
                int(symbol_count or 0),
                min_time,
                max_time,
                source,
                checksum,
                freshness_status,
                now,
                now,
                json.dumps(extra or {}, ensure_ascii=False),
            ),
        )

    def start_ingestion_run(
        self,
        *,
        dataset_name: str,
        period: str,
        trade_date: str,
        source: str = "",
        extra: dict[str, Any] | None = None,
    ) -> str:
        run_id = f"run-{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO ingestion_runs(
                run_id, dataset_name, period, trade_date, status, row_count, symbol_count,
                source, error_message, started_at, finished_at, extra_json
            )
            VALUES(?, ?, ?, ?, 'running', 0, 0, ?, '', ?, '', ?)
            """,
            (
                run_id,
                dataset_name,
                period,
                trade_date,
                source,
                _now(),
                json.dumps(extra or {}, ensure_ascii=False),
            ),
        )
        return run_id

    def finish_ingestion_run(
        self,
        run_id: str,
        *,
        status: str,
        row_count: int = 0,
        symbol_count: int = 0,
        error_message: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        existing = self.db.query_one("SELECT extra_json FROM ingestion_runs WHERE run_id = ?", (run_id,))
        merged_extra: dict[str, Any] = {}
        if existing and existing.get("extra_json"):
            try:
                merged_extra.update(json.loads(existing["extra_json"]))
            except Exception:
                pass
        merged_extra.update(extra or {})
        self.db.execute(
            """
            UPDATE ingestion_runs
            SET status=?, row_count=?, symbol_count=?, error_message=?, finished_at=?, extra_json=?
            WHERE run_id=?
            """,
            (
                status,
                int(row_count or 0),
                int(symbol_count or 0),
                error_message,
                _now(),
                json.dumps(merged_extra, ensure_ascii=False),
                run_id,
            ),
        )

    def list_datasets(self) -> list[dict[str, Any]]:
        items = self.db.query_all("SELECT * FROM dataset_catalog ORDER BY dataset_name")
        for item in items:
            item["primary_keys"] = json.loads(item.get("primary_keys") or "[]")
            item["partition_keys"] = json.loads(item.get("partition_keys") or "[]")
        return items

    def list_partitions(
        self,
        dataset_name: str | None = None,
        *,
        period: str | None = None,
        trade_date_start: str | None = None,
        trade_date_end: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if dataset_name:
            conditions.append("dataset_name = ?")
            params.append(dataset_name)
        if period:
            conditions.append("period = ?")
            params.append(period)
        if trade_date_start:
            conditions.append("trade_date >= ?")
            params.append(trade_date_start)
        if trade_date_end:
            conditions.append("trade_date <= ?")
            params.append(trade_date_end)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        items = self.db.query_all(
            f"""
            SELECT * FROM dataset_partitions
            {where_clause}
            ORDER BY trade_date DESC, updated_at DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        for item in items:
            item["extra"] = json.loads(item.get("extra_json") or "{}")
        return items

    def recent_runs(self, dataset_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if dataset_name:
            items = self.db.query_all(
                """
                SELECT * FROM ingestion_runs
                WHERE dataset_name = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (dataset_name, limit),
            )
        else:
            items = self.db.query_all(
                "SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        for item in items:
            item["extra"] = json.loads(item.get("extra_json") or "{}")
        return items

    def build_health_snapshot(self) -> dict[str, Any]:
        dataset_items = self.list_datasets()
        partitions = self.list_partitions(limit=200)
        by_dataset: dict[str, dict[str, Any]] = {
            item["dataset_name"]: {
                "dataset_name": item["dataset_name"],
                "storage_kind": item["storage_kind"],
                "latest_trade_date": "",
                "partition_count": 0,
                "latest_partition_path": "",
            }
            for item in dataset_items
        }
        for item in partitions:
            bucket = by_dataset.setdefault(
                item["dataset_name"],
                {
                    "dataset_name": item["dataset_name"],
                    "storage_kind": "",
                    "latest_trade_date": "",
                    "partition_count": 0,
                    "latest_partition_path": "",
                },
            )
            bucket["partition_count"] += 1
            if str(item.get("trade_date") or "") >= str(bucket.get("latest_trade_date") or ""):
                bucket["latest_trade_date"] = item.get("trade_date") or ""
                bucket["latest_partition_path"] = item.get("path") or ""
        return {
            "dataset_count": len(dataset_items),
            "partition_count": len(partitions),
            "datasets": list(by_dataset.values()),
            "recent_runs": self.recent_runs(limit=10),
        }
