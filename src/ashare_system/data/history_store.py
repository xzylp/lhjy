"""本地历史底座第一阶段。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import md5
from pathlib import Path
import importlib.util
import json
import math
from typing import Any

from .catalog_service import CatalogService
from .control_db import ControlPlaneDB
from .storage import ensure_storage_layout


STYLE_TAG_SUMMARY = {
    "high_beta": "高波动高弹性，适合超短快进快出",
    "leader": "龙头属性较强，适合主升段跟随",
    "trend": "趋势延续型，适合顺势持有",
    "defensive": "偏防守，爆发力有限",
    "mixed": "股性混合，需要结合当日强度再判断",
}


def _now() -> str:
    return datetime.now().isoformat()


def _trade_time_sort_key(record: dict[str, Any]) -> str:
    return str(record.get("trade_time") or record.get("trade_date") or "")


def _finite_float(value: Any) -> float:
    try:
        resolved = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return resolved if math.isfinite(resolved) else 0.0


def _normalize_trade_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    return text


def _slugify_filename(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "").strip())
    collapsed = "_".join(part for part in normalized.split("_") if part)
    return collapsed[:80] or "batch"


class HistoryStore:
    """历史数据写入、股性摘要与能力探测。"""

    def __init__(self, storage_root: Path, db: ControlPlaneDB, catalog_service: CatalogService) -> None:
        self.layout = ensure_storage_layout(storage_root)
        self.db = db
        self.catalog_service = catalog_service
        self.catalog_service.ensure_default_catalog()

    def capabilities(self) -> dict[str, Any]:
        pyarrow_enabled = importlib.util.find_spec("pyarrow") is not None
        fastparquet_enabled = importlib.util.find_spec("fastparquet") is not None
        duckdb_enabled = importlib.util.find_spec("duckdb") is not None
        parquet_enabled = pyarrow_enabled or fastparquet_enabled
        return {
            "db_path": str(self.db.db_path),
            "lake_root": str(self.layout.lake_root),
            "state_root": str(self.layout.state_root),
            "reports_root": str(self.layout.reports_root),
            "parquet_enabled": parquet_enabled,
            "duckdb_enabled": duckdb_enabled,
            "pyarrow_enabled": pyarrow_enabled,
            "fastparquet_enabled": fastparquet_enabled,
            "preferred_file_format": "parquet" if parquet_enabled else "jsonl",
        }

    def write_records(
        self,
        *,
        dataset_name: str,
        trade_date: str,
        period: str,
        records: list[dict[str, Any]],
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ValueError(f"{dataset_name} 无可写入记录")
        target_dir = self.layout.lake_root / dataset_name / f"trade_date={trade_date}"
        target_dir.mkdir(parents=True, exist_ok=True)
        capabilities = self.capabilities()
        file_format = str(capabilities["preferred_file_format"])
        file_tag = _slugify_filename(str((extra or {}).get("file_tag") or source or "batch"))
        file_name = f"{dataset_name}_{period}_{trade_date}_{file_tag}.{file_format}"
        target_path = target_dir / file_name
        if file_format == "parquet":
            import pandas as pd

            frame = pd.DataFrame(records)
            engine = "pyarrow" if capabilities["pyarrow_enabled"] else "fastparquet"
            frame.to_parquet(target_path, index=False, engine=engine)
        else:
            with target_path.open("w", encoding="utf-8") as handle:
                for row in records:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        symbol_count = len({str(item.get("symbol") or "").strip() for item in records if item.get("symbol")})
        trade_times = [str(item.get("trade_time") or "") for item in records if item.get("trade_time")]
        min_time = min(trade_times) if trade_times else ""
        max_time = max(trade_times) if trade_times else ""
        checksum = md5(target_path.read_bytes()).hexdigest()
        self.catalog_service.record_partition(
            dataset_name=dataset_name,
            period=period,
            trade_date=trade_date,
            path=str(target_path),
            file_format=file_format,
            row_count=len(records),
            symbol_count=int(symbol_count),
            min_time=min_time,
            max_time=max_time,
            source=source,
            checksum=checksum,
            freshness_status="fresh",
            extra=extra,
        )
        return {
            "dataset_name": dataset_name,
            "trade_date": trade_date,
            "period": period,
            "path": str(target_path),
            "file_format": file_format,
            "row_count": len(records),
            "symbol_count": int(symbol_count),
            "min_time": min_time,
            "max_time": max_time,
        }

    def write_time_partitioned_records(
        self,
        *,
        dataset_name: str,
        period: str,
        records: list[dict[str, Any]],
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ValueError(f"{dataset_name} 无可写入记录")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            trade_date = self._extract_trade_date(item)
            if not trade_date:
                continue
            grouped.setdefault(trade_date, []).append(item)
        if not grouped:
            raise ValueError(f"{dataset_name} 无法从 records 中解析 trade_date")
        partition_results: list[dict[str, Any]] = []
        for trade_date, bucket in sorted(grouped.items()):
            partition_extra = dict(extra or {})
            if not partition_extra.get("file_tag"):
                partition_extra["file_tag"] = str(partition_extra.get("run_id") or source or "batch")
            partition_results.append(
                self.write_records(
                    dataset_name=dataset_name,
                    trade_date=trade_date,
                    period=period,
                    records=bucket,
                    source=source,
                    extra=partition_extra,
                )
            )
        symbol_set = {
            str(item.get("symbol") or "").strip()
            for item in records
            if str(item.get("symbol") or "").strip()
        }
        return {
            "dataset_name": dataset_name,
            "period": period,
            "partition_count": len(partition_results),
            "trade_dates": sorted(grouped.keys()),
            "row_count": sum(int(item.get("row_count", 0) or 0) for item in partition_results),
            "symbol_count": len(symbol_set),
            "latest_path": str(partition_results[-1].get("path") if partition_results else ""),
            "partitions": partition_results,
        }

    def upsert_behavior_profiles(self, payload: dict[str, Any], *, source: str = "research_state") -> dict[str, Any]:
        trade_date = str(payload.get("trade_date") or "").strip()
        items = list(payload.get("items") or [])
        if not trade_date or not items:
            raise ValueError("股性画像 payload 不完整")

        rows: list[tuple[Any, ...]] = []
        for item in items:
            symbol = str(item.get("symbol") or "").strip()
            profile = item.get("profile") or {}
            if not symbol or not isinstance(profile, dict):
                continue
            summary = self._build_profile_summary(symbol, trade_date, profile)
            rows.append(
                (
                    symbol,
                    trade_date,
                    str(profile.get("style_tag") or ""),
                    int(profile.get("optimal_hold_days") or 1),
                    float(profile.get("board_success_rate_20d") or 0.0),
                    float(profile.get("bomb_rate_20d") or 0.0),
                    float(profile.get("next_day_premium_20d") or 0.0),
                    float(profile.get("reseal_rate_20d") or 0.0),
                    float(profile.get("avg_sector_rank_30d") or 99.0),
                    float(profile.get("leader_frequency_30d") or 0.0),
                    summary,
                    json.dumps(
                        {
                            "symbol": symbol,
                            "trade_date": trade_date,
                            "profile": profile,
                            "source": item.get("source"),
                            "profile_trade_date": item.get("profile_trade_date"),
                        },
                        ensure_ascii=False,
                    ),
                    _now(),
                )
            )

        self.db.executemany(
            """
            INSERT INTO stock_behavior_profiles(
                symbol, trade_date, style_tag, optimal_hold_days, board_success_rate_20d, bomb_rate_20d,
                next_day_premium_20d, reseal_rate_20d, avg_sector_rank_30d, leader_frequency_30d,
                summary_text, payload_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                style_tag=excluded.style_tag,
                optimal_hold_days=excluded.optimal_hold_days,
                board_success_rate_20d=excluded.board_success_rate_20d,
                bomb_rate_20d=excluded.bomb_rate_20d,
                next_day_premium_20d=excluded.next_day_premium_20d,
                reseal_rate_20d=excluded.reseal_rate_20d,
                avg_sector_rank_30d=excluded.avg_sector_rank_30d,
                leader_frequency_30d=excluded.leader_frequency_30d,
                summary_text=excluded.summary_text,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        self.catalog_service.record_partition(
            dataset_name="stock_behavior_profiles",
            period="1d",
            trade_date=trade_date,
            path=f"sqlite://stock_behavior_profiles/{trade_date}",
            file_format="sqlite",
            row_count=len(rows),
            symbol_count=len({row[0] for row in rows}),
            source=source,
            freshness_status="fresh",
            extra={"source_counts": payload.get("source_counts", {})},
        )
        return {
            "trade_date": trade_date,
            "row_count": len(rows),
            "symbol_count": len({row[0] for row in rows}),
            "source_counts": payload.get("source_counts", {}),
        }

    def get_behavior_profile(self, symbol: str, trade_date: str | None = None) -> dict[str, Any] | None:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        if trade_date:
            row = self.db.query_one(
                """
                SELECT * FROM stock_behavior_profiles
                WHERE symbol = ? AND trade_date = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (normalized_symbol, trade_date),
            )
        else:
            row = self.db.query_one(
                """
                SELECT * FROM stock_behavior_profiles
                WHERE symbol = ?
                ORDER BY trade_date DESC, updated_at DESC LIMIT 1
                """,
                (normalized_symbol,),
            )
        if row and row.get("payload_json"):
            try:
                row["payload"] = json.loads(row["payload_json"])
            except Exception:
                row["payload"] = {}
        return row

    def list_behavior_profiles(self, trade_date: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if trade_date:
            return self.db.query_all(
                """
                SELECT symbol, trade_date, style_tag, optimal_hold_days, board_success_rate_20d,
                       bomb_rate_20d, next_day_premium_20d, reseal_rate_20d, avg_sector_rank_30d,
                       leader_frequency_30d, summary_text, updated_at
                FROM stock_behavior_profiles
                WHERE trade_date = ?
                ORDER BY leader_frequency_30d DESC, board_success_rate_20d DESC
                LIMIT ?
                """,
                (trade_date, limit),
            )
        return self.db.query_all(
            """
            SELECT symbol, trade_date, style_tag, optimal_hold_days, board_success_rate_20d,
                   bomb_rate_20d, next_day_premium_20d, reseal_rate_20d, avg_sector_rank_30d,
                   leader_frequency_30d, summary_text, updated_at
            FROM stock_behavior_profiles
            ORDER BY trade_date DESC, leader_frequency_30d DESC, board_success_rate_20d DESC
            LIMIT ?
            """,
            (limit,),
        )

    def get_stock_summary(self, symbol: str, trade_date: str | None = None) -> dict[str, Any] | None:
        row = self.get_behavior_profile(symbol, trade_date=trade_date)
        if row is None:
            return None
        return {
            "symbol": row["symbol"],
            "trade_date": row["trade_date"],
            "style_tag": row["style_tag"],
            "optimal_hold_days": row["optimal_hold_days"],
            "summary": row["summary_text"],
            "metrics": {
                "board_success_rate_20d": row["board_success_rate_20d"],
                "bomb_rate_20d": row["bomb_rate_20d"],
                "next_day_premium_20d": row["next_day_premium_20d"],
                "reseal_rate_20d": row["reseal_rate_20d"],
                "avg_sector_rank_30d": row["avg_sector_rank_30d"],
                "leader_frequency_30d": row["leader_frequency_30d"],
            },
            "updated_at": row["updated_at"],
        }

    def list_partitions(
        self,
        *,
        dataset_name: str,
        period: str | None = None,
        trade_date_start: str | None = None,
        trade_date_end: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.catalog_service.list_partitions(
            dataset_name,
            period=period,
            trade_date_start=trade_date_start,
            trade_date_end=trade_date_end,
            limit=max(int(limit or 1), 1),
        )

    def read_bars(
        self,
        *,
        symbols: list[str],
        period: str = "1d",
        start_trade_date: str | None = None,
        end_trade_date: str | None = None,
        limit: int = 240,
    ) -> dict[str, Any]:
        normalized_symbols = [str(item or "").strip().upper() for item in list(symbols or []) if str(item or "").strip()]
        if not normalized_symbols:
            return {"dataset_name": f"bars_{period}", "period": period, "count": 0, "items": [], "by_symbol": {}}
        dataset_name = "bars_1d" if period == "1d" else ("bars_1m" if period == "1m" else f"bars_{period}")
        partitions = self.list_partitions(
            dataset_name=dataset_name,
            period=period,
            trade_date_start=_normalize_trade_date(start_trade_date or ""),
            trade_date_end=_normalize_trade_date(end_trade_date or ""),
            limit=max(limit * max(len(normalized_symbols), 1) * 4, 400),
        )
        requested_symbol_set = set(normalized_symbols)
        buckets: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in normalized_symbols}
        hard_cap = max(limit, 1)
        open_range = bool(start_trade_date or end_trade_date)
        for partition in partitions:
            rows = self._read_partition_records(partition)
            for row in rows:
                symbol = str(row.get("symbol") or "").strip().upper()
                if symbol not in requested_symbol_set:
                    continue
                buckets.setdefault(symbol, []).append(row)
            if not open_range and all(len(buckets.get(symbol, [])) >= hard_cap for symbol in normalized_symbols):
                break

        items: list[dict[str, Any]] = []
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for symbol in normalized_symbols:
            rows = sorted(buckets.get(symbol, []), key=_trade_time_sort_key)
            if hard_cap > 0:
                rows = rows[-hard_cap:]
            by_symbol[symbol] = rows
            items.extend(rows)
        items.sort(key=lambda item: (str(item.get("symbol") or ""), _trade_time_sort_key(item)))
        return {
            "dataset_name": dataset_name,
            "period": period,
            "count": len(items),
            "symbols": normalized_symbols,
            "items": items,
            "by_symbol": by_symbol,
            "partitions_scanned": len(partitions),
        }

    def latest_bars(self, *, symbols: list[str], period: str = "1d") -> list[dict[str, Any]]:
        payload = self.read_bars(symbols=symbols, period=period, limit=1)
        result: list[dict[str, Any]] = []
        for symbol in list(payload.get("symbols") or []):
            rows = list((payload.get("by_symbol") or {}).get(symbol) or [])
            if rows:
                result.append(rows[-1])
        return result

    def build_history_context(
        self,
        *,
        symbols: list[str],
        trade_date: str | None = None,
        daily_limit: int = 60,
        minute_limit: int = 120,
    ) -> dict[str, Any]:
        normalized_symbols = [str(item or "").strip().upper() for item in list(symbols or []) if str(item or "").strip()]
        summaries: list[dict[str, Any]] = []
        summary_lines: list[str] = []
        for symbol in normalized_symbols[:20]:
            daily_payload = self.read_bars(
                symbols=[symbol],
                period="1d",
                end_trade_date=trade_date,
                limit=max(int(daily_limit or 1), 1),
            )
            minute_payload = self.read_bars(
                symbols=[symbol],
                period="1m",
                end_trade_date=trade_date,
                limit=max(int(minute_limit or 1), 1),
            )
            daily_rows = list((daily_payload.get("by_symbol") or {}).get(symbol) or [])
            minute_rows = list((minute_payload.get("by_symbol") or {}).get(symbol) or [])
            stock_summary = self.get_stock_summary(symbol, trade_date=trade_date)
            if not daily_rows and not minute_rows and stock_summary is None:
                continue
            metrics = self._build_symbol_history_metrics(daily_rows, minute_rows)
            summary_text = self._format_symbol_history_summary(symbol, metrics, stock_summary)
            summaries.append(
                {
                    "symbol": symbol,
                    "daily_rows": len(daily_rows),
                    "minute_rows": len(minute_rows),
                    "metrics": metrics,
                    "stock_summary": stock_summary,
                    "summary": summary_text,
                }
            )
            summary_lines.append(summary_text)
        return {
            "available": bool(summaries),
            "symbol_count": len(summaries),
            "trade_date": trade_date,
            "items": summaries,
            "summary_lines": summary_lines,
        }

    def _build_profile_summary(self, symbol: str, trade_date: str, profile: dict[str, Any]) -> str:
        style_tag = str(profile.get("style_tag") or "mixed")
        board_success_rate = float(profile.get("board_success_rate_20d") or 0.0)
        bomb_rate = float(profile.get("bomb_rate_20d") or 0.0)
        next_day_premium = float(profile.get("next_day_premium_20d") or 0.0)
        reseal_rate = float(profile.get("reseal_rate_20d") or 0.0)
        optimal_hold_days = int(profile.get("optimal_hold_days") or 1)
        leader_frequency = float(profile.get("leader_frequency_30d") or 0.0)
        avg_sector_rank = float(profile.get("avg_sector_rank_30d") or 99.0)
        return (
            f"{symbol} 在 {trade_date} 的股性画像：{STYLE_TAG_SUMMARY.get(style_tag, STYLE_TAG_SUMMARY['mixed'])}；"
            f"近20日封板率 {board_success_rate:.1%}，炸板率 {bomb_rate:.1%}，回封率 {reseal_rate:.1%}，"
            f"次日平均溢价 {next_day_premium:.2f}%，近30日龙头频次 {leader_frequency:.1%}，"
            f"板块平均排序 {avg_sector_rank:.1f}，建议持有 {optimal_hold_days} 天。"
        )

    def catalog_snapshot(self) -> dict[str, Any]:
        health = self.catalog_service.build_health_snapshot()
        behavior_counts = Counter(item["trade_date"] for item in self.list_behavior_profiles(limit=200))
        health["behavior_trade_dates"] = dict(behavior_counts)
        health["capabilities"] = self.capabilities()
        return health

    def _extract_trade_date(self, record: dict[str, Any]) -> str:
        explicit_trade_date = _normalize_trade_date(str(record.get("trade_date") or ""))
        if explicit_trade_date:
            return explicit_trade_date
        return _normalize_trade_date(str(record.get("trade_time") or ""))

    def _read_partition_records(self, partition: dict[str, Any]) -> list[dict[str, Any]]:
        path = Path(str(partition.get("path") or ""))
        if not path.exists():
            return []
        file_format = str(partition.get("file_format") or path.suffix.lstrip(".")).strip().lower()
        if file_format == "parquet":
            import pandas as pd

            frame = pd.read_parquet(path)
            return frame.to_dict(orient="records")
        if file_format == "jsonl":
            rows: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    text = str(line or "").strip()
                    if not text:
                        continue
                    try:
                        rows.append(json.loads(text))
                    except json.JSONDecodeError:
                        continue
            return rows
        return []

    def _build_symbol_history_metrics(
        self,
        daily_rows: list[dict[str, Any]],
        minute_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest_close = float(daily_rows[-1].get("close", 0.0) or 0.0) if daily_rows else 0.0
        latest_close = _finite_float(daily_rows[-1].get("close", 0.0)) if daily_rows else 0.0
        prev_close = _finite_float(daily_rows[-2].get("close", 0.0)) if len(daily_rows) >= 2 else latest_close
        close_5 = _finite_float(daily_rows[-5].get("close", 0.0)) if len(daily_rows) >= 5 else 0.0
        close_20 = _finite_float(daily_rows[-20].get("close", 0.0)) if len(daily_rows) >= 20 else 0.0
        intraday_latest = (
            _finite_float(minute_rows[-1].get("close", 0.0) or minute_rows[-1].get("last_price", 0.0))
            if minute_rows
            else 0.0
        )
        return {
            "latest_trade_time": str(daily_rows[-1].get("trade_time") or "") if daily_rows else "",
            "latest_close": latest_close,
            "day_return_pct": round(((latest_close - prev_close) / prev_close) * 100, 3) if prev_close > 0 else 0.0,
            "return_5d_pct": round(((latest_close - close_5) / close_5) * 100, 3) if close_5 > 0 else 0.0,
            "return_20d_pct": round(((latest_close - close_20) / close_20) * 100, 3) if close_20 > 0 else 0.0,
            "daily_high_20": max((_finite_float(item.get("high", 0.0)) for item in daily_rows[-20:]), default=0.0),
            "daily_low_20": min(
                (_finite_float(item.get("low", 0.0)) for item in daily_rows[-20:] if _finite_float(item.get("low", 0.0)) > 0),
                default=0.0,
            ),
            "daily_rows": len(daily_rows),
            "minute_rows": len(minute_rows),
            "intraday_latest": intraday_latest,
        }

    def _format_symbol_history_summary(
        self,
        symbol: str,
        metrics: dict[str, Any],
        stock_summary: dict[str, Any] | None,
    ) -> str:
        summary = (
            f"{symbol} 历史摘要：最新收盘 {float(metrics.get('latest_close', 0.0) or 0.0):.2f}，"
            f"1日 {float(metrics.get('day_return_pct', 0.0) or 0.0):+.2f}% ，"
            f"5日 {float(metrics.get('return_5d_pct', 0.0) or 0.0):+.2f}% ，"
            f"20日 {float(metrics.get('return_20d_pct', 0.0) or 0.0):+.2f}% 。"
        )
        if stock_summary and stock_summary.get("summary"):
            summary += f"股性：{stock_summary.get('summary')}"
        return summary
