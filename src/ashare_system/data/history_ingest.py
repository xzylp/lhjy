"""历史底座 ingest 服务。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any

from ..infra.audit_store import AuditStore, StateStore
from ..infra.market_adapter import MarketDataAdapter
from .catalog_service import CatalogService
from .history_store import HistoryStore


def _to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    raise TypeError(f"无法序列化 {type(value)}")


class HistoryIngestService:
    """把历史行情和摘要层写入正式底座。"""

    def __init__(
        self,
        market_adapter: MarketDataAdapter,
        history_store: HistoryStore,
        catalog_service: CatalogService,
        research_state_store: StateStore | None = None,
        audit_store: AuditStore | None = None,
    ) -> None:
        self.market_adapter = market_adapter
        self.history_store = history_store
        self.catalog_service = catalog_service
        self.research_state_store = research_state_store
        self.audit_store = audit_store

    def ingest_daily_bars(
        self,
        *,
        symbols: list[str],
        trade_date: str,
        count: int = 120,
        source: str = "market_adapter",
    ) -> dict[str, Any]:
        run_id = self.catalog_service.start_ingestion_run(
            dataset_name="bars_1d",
            period="1d",
            trade_date=trade_date,
            source=source,
            extra={"requested_symbols": len(symbols), "count": count},
        )
        try:
            bars = self.market_adapter.get_bars(
                symbols=symbols,
                period="1d",
                count=max(int(count or 1), 1),
                end_time=f"{trade_date}T15:00:00",
            )
            records = [_to_dict(item) for item in bars]
            result = self.history_store.write_time_partitioned_records(
                dataset_name="bars_1d",
                period="1d",
                records=records,
                source=source,
                extra={"run_id": run_id, "file_tag": run_id},
            )
            self.catalog_service.finish_ingestion_run(
                run_id,
                status="success",
                row_count=result["row_count"],
                symbol_count=result["symbol_count"],
                extra={
                    "latest_path": result.get("latest_path"),
                    "partition_count": result.get("partition_count", 0),
                    "trade_dates": result.get("trade_dates", []),
                },
            )
            response = {"run_id": run_id, "trade_date": trade_date, **result}
            self._audit("历史日线写入完成", response)
            return response
        except Exception as exc:
            self.catalog_service.finish_ingestion_run(run_id, status="failed", error_message=str(exc))
            self._audit("历史日线写入失败", {"run_id": run_id, "error": str(exc)})
            raise

    def ingest_minute_bars(
        self,
        *,
        symbols: list[str],
        trade_date: str,
        count: int = 240,
        period: str = "1m",
        source: str = "market_adapter",
    ) -> dict[str, Any]:
        dataset_name = "bars_1m" if period == "1m" else f"bars_{period}"
        run_id = self.catalog_service.start_ingestion_run(
            dataset_name=dataset_name,
            period=period,
            trade_date=trade_date,
            source=source,
            extra={"requested_symbols": len(symbols), "count": count},
        )
        try:
            bars = self.market_adapter.get_bars(
                symbols=symbols,
                period=period,
                count=max(int(count or 1), 1),
                end_time=f"{trade_date}T15:00:00",
            )
            records = [_to_dict(item) for item in bars]
            result = self.history_store.write_time_partitioned_records(
                dataset_name=dataset_name,
                period=period,
                records=records,
                source=source,
                extra={"run_id": run_id, "file_tag": run_id},
            )
            self.catalog_service.finish_ingestion_run(
                run_id,
                status="success",
                row_count=result["row_count"],
                symbol_count=result["symbol_count"],
                extra={
                    "latest_path": result.get("latest_path"),
                    "partition_count": result.get("partition_count", 0),
                    "trade_dates": result.get("trade_dates", []),
                },
            )
            response = {"run_id": run_id, "trade_date": trade_date, **result}
            self._audit("历史分钟线写入完成", response)
            return response
        except Exception as exc:
            self.catalog_service.finish_ingestion_run(run_id, status="failed", error_message=str(exc))
            self._audit("历史分钟线写入失败", {"run_id": run_id, "error": str(exc)})
            raise

    def backfill_daily_bars(
        self,
        *,
        symbols: list[str],
        end_trade_date: str,
        start_trade_date: str | None = None,
        window_count: int = 240,
        max_rounds: int = 12,
        source: str = "history_backfill",
    ) -> dict[str, Any]:
        resolved_start = str(start_trade_date or "").strip()
        run_id = self.catalog_service.start_ingestion_run(
            dataset_name="bars_1d",
            period="1d",
            trade_date=end_trade_date,
            source=source,
            extra={
                "requested_symbols": len(symbols),
                "window_count": window_count,
                "max_rounds": max_rounds,
                "start_trade_date": resolved_start,
            },
        )
        try:
            cursor = datetime.fromisoformat(f"{end_trade_date}T15:00:00")
            seen_keys: set[tuple[str, str]] = set()
            round_results: list[dict[str, Any]] = []
            total_rows = 0
            total_partitions = 0
            for index in range(1, max(int(max_rounds or 1), 1) + 1):
                bars = self.market_adapter.get_bars(
                    symbols=symbols,
                    period="1d",
                    count=max(int(window_count or 1), 1),
                    end_time=cursor.isoformat(),
                )
                records = [_to_dict(item) for item in bars]
                if not records:
                    break
                unique_records: list[dict[str, Any]] = []
                earliest_trade_time = ""
                for item in records:
                    symbol = str(item.get("symbol") or "").strip().upper()
                    trade_time = str(item.get("trade_time") or "").strip()
                    if not symbol or not trade_time:
                        continue
                    key = (symbol, trade_time)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    unique_records.append(item)
                    if not earliest_trade_time or trade_time < earliest_trade_time:
                        earliest_trade_time = trade_time
                if not unique_records:
                    break
                result = self.history_store.write_time_partitioned_records(
                    dataset_name="bars_1d",
                    period="1d",
                    records=unique_records,
                    source=source,
                    extra={"run_id": run_id, "file_tag": f"{run_id}_round_{index}"},
                )
                round_results.append(result)
                total_rows += int(result.get("row_count", 0) or 0)
                total_partitions += int(result.get("partition_count", 0) or 0)
                if not earliest_trade_time:
                    break
                earliest_dt = datetime.fromisoformat(earliest_trade_time.replace("Z", "+00:00"))
                next_cursor = earliest_dt - timedelta(seconds=1)
                if next_cursor >= cursor:
                    break
                cursor = next_cursor
                if resolved_start and next_cursor.date().isoformat() < resolved_start:
                    break
            payload = {
                "run_id": run_id,
                "dataset_name": "bars_1d",
                "period": "1d",
                "row_count": total_rows,
                "partition_count": total_partitions,
                "symbol_count": len({str(item).strip().upper() for item in symbols if str(item).strip()}),
                "round_count": len(round_results),
                "start_trade_date": resolved_start or None,
                "end_trade_date": end_trade_date,
                "latest_path": str(round_results[-1].get("latest_path") if round_results else ""),
                "rounds": round_results,
            }
            self.catalog_service.finish_ingestion_run(
                run_id,
                status="success",
                row_count=total_rows,
                symbol_count=payload["symbol_count"],
                extra={
                    "round_count": payload["round_count"],
                    "partition_count": total_partitions,
                    "latest_path": payload["latest_path"],
                    "start_trade_date": resolved_start,
                },
            )
            self._audit("历史日线回填完成", payload)
            return payload
        except Exception as exc:
            self.catalog_service.finish_ingestion_run(run_id, status="failed", error_message=str(exc))
            self._audit("历史日线回填失败", {"run_id": run_id, "error": str(exc)})
            raise

    def sync_behavior_profiles(self, trade_date: str | None = None) -> dict[str, Any]:
        payload = self._select_behavior_payload(trade_date)
        if not payload:
            raise ValueError("research_state 中没有可同步的股性画像")
        resolved_trade_date = str(payload.get("trade_date") or "")
        run_id = self.catalog_service.start_ingestion_run(
            dataset_name="stock_behavior_profiles",
            period="1d",
            trade_date=resolved_trade_date,
            source="research_state",
        )
        try:
            result = self.history_store.upsert_behavior_profiles(payload, source="research_state")
            self.catalog_service.finish_ingestion_run(
                run_id,
                status="success",
                row_count=result["row_count"],
                symbol_count=result["symbol_count"],
            )
            self._audit("股性画像同步完成", result | {"run_id": run_id})
            return {"run_id": run_id, **result}
        except Exception as exc:
            self.catalog_service.finish_ingestion_run(run_id, status="failed", error_message=str(exc))
            self._audit("股性画像同步失败", {"run_id": run_id, "error": str(exc)})
            raise

    def _select_behavior_payload(self, trade_date: str | None = None) -> dict[str, Any] | None:
        if self.research_state_store is None:
            return None
        latest = self.research_state_store.get("latest_stock_behavior_profiles", {}) or {}
        if latest and (not trade_date or latest.get("trade_date") == trade_date):
            return latest
        history = list(self.research_state_store.get("stock_behavior_profile_history", []) or [])
        if trade_date:
            for item in reversed(history):
                if str(item.get("trade_date") or "") == trade_date:
                    return item
        return latest or (history[-1] if history else None)

    def _audit(self, message: str, payload: dict[str, Any]) -> None:
        if self.audit_store:
            self.audit_store.append("history", message, payload)
