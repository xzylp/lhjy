"""数据底座归档与 serving 写入。"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts import BarSnapshot, QuoteSnapshot
from .contracts import (
    DossierRecord,
    EventRecord,
    IndexSnapshotRecord,
    MarketBarRecord,
    MarketSnapshotRecord,
    MarketStructureSnapshotRecord,
    SymbolContextRecord,
)
from .freshness import build_freshness_meta
from .storage import StorageLayout, ensure_storage_layout


class DataArchiveStore:
    """按交易日分片的文件归档存储。"""

    def __init__(self, storage_root: Path) -> None:
        self.layout: StorageLayout = ensure_storage_layout(storage_root)

    def persist_symbol_snapshots(
        self,
        snapshots: list[QuoteSnapshot],
        *,
        generated_at: str,
        source: str = "xtquant",
    ) -> list[MarketSnapshotRecord]:
        records: list[MarketSnapshotRecord] = []
        for item in snapshots:
            snapshot_at = generated_at
            change_pct = self._change_pct(item.last_price, item.pre_close)
            records.append(
                MarketSnapshotRecord(
                    symbol=item.symbol,
                    name=item.name,
                    source=source,
                    snapshot_at=snapshot_at,
                    last_price=item.last_price,
                    bid_price=item.bid_price,
                    ask_price=item.ask_price,
                    pre_close=item.pre_close,
                    change_pct=change_pct,
                    volume=item.volume,
                    **build_freshness_meta(
                        source_at=snapshot_at,
                        fetched_at=generated_at,
                        generated_at=generated_at,
                        fresh_seconds=300,
                    ).model_dump(),
                )
            )
        if not records:
            return []
        trade_date = self._trade_date_from_iso(generated_at)
        raw_payloads = [self._raw_quote_payload(item, source=source, fetched_at=generated_at) for item in snapshots]
        self._upsert_jsonl(
            self.layout.raw_market_symbol_root / "snapshot" / f"{trade_date}.jsonl",
            raw_payloads,
            key_func=lambda payload: f"{payload['symbol']}|{payload['snapshot_at']}",
        )
        self._upsert_jsonl(
            self.layout.normalized_market_symbol_root / "snapshot" / f"{trade_date}.jsonl",
            [item.model_dump() for item in records],
            key_func=lambda payload: f"{payload['symbol']}|{payload['snapshot_at']}",
        )
        return records

    def persist_index_snapshots(
        self,
        snapshots: list[QuoteSnapshot],
        *,
        generated_at: str,
        source: str = "xtquant",
        index_name_map: dict[str, str] | None = None,
    ) -> list[IndexSnapshotRecord]:
        records: list[IndexSnapshotRecord] = []
        for item in snapshots:
            snapshot_at = generated_at
            records.append(
                IndexSnapshotRecord(
                    index_symbol=item.symbol,
                    index_name=(index_name_map or {}).get(item.symbol, item.name or item.symbol),
                    source=source,
                    snapshot_at=snapshot_at,
                    last_price=item.last_price,
                    change_pct=self._change_pct(item.last_price, item.pre_close),
                    volume=item.volume,
                    **build_freshness_meta(
                        source_at=snapshot_at,
                        fetched_at=generated_at,
                        generated_at=generated_at,
                        fresh_seconds=60,
                    ).model_dump(),
                )
            )
        if not records:
            return []
        trade_date = self._trade_date_from_iso(generated_at)
        raw_payloads = []
        for item in snapshots:
            payload = self._raw_quote_payload(item, source=source, fetched_at=generated_at)
            payload["index_symbol"] = payload.pop("symbol")
            payload["index_name"] = (index_name_map or {}).get(item.symbol, item.name or item.symbol)
            raw_payloads.append(payload)
        self._upsert_jsonl(
            self.layout.raw_market_index_root / "snapshot" / f"{trade_date}.jsonl",
            raw_payloads,
            key_func=lambda payload: f"{payload['index_symbol']}|{payload['snapshot_at']}",
        )
        self._upsert_jsonl(
            self.layout.normalized_market_index_root / "snapshot" / f"{trade_date}.jsonl",
            [item.model_dump() for item in records],
            key_func=lambda payload: f"{payload['index_symbol']}|{payload['snapshot_at']}",
        )
        return records

    def persist_market_bars(
        self,
        bars: list[BarSnapshot],
        *,
        generated_at: str,
        source: str = "xtquant",
        name_map: dict[str, str] | None = None,
    ) -> list[MarketBarRecord]:
        by_period: dict[str, list[MarketBarRecord]] = {}
        raw_by_period: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            record = MarketBarRecord(
                symbol=bar.symbol,
                name=(name_map or {}).get(bar.symbol, ""),
                period=bar.period,
                trade_time=bar.trade_time,
                source=source,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                pre_close=bar.pre_close,
                **build_freshness_meta(
                    source_at=bar.trade_time,
                    fetched_at=generated_at,
                    generated_at=generated_at,
                    fresh_seconds=86400,
                    warm_seconds=86400 * 10,
                    expiry_seconds=86400,
                ).model_dump(),
            )
            by_period.setdefault(bar.period, []).append(record)
            raw_payload = bar.model_dump()
            raw_payload["name"] = record.name
            raw_payload["source"] = source
            raw_payload["fetched_at"] = generated_at
            raw_by_period.setdefault(bar.period, []).append(raw_payload)

        all_records: list[MarketBarRecord] = []
        for period, records in by_period.items():
            if not records:
                continue
            trade_date = self._trade_date_from_any(records[0].trade_time)
            self._upsert_jsonl(
                self.layout.raw_market_symbol_root / period / f"{trade_date}.jsonl",
                raw_by_period[period],
                key_func=lambda payload: f"{payload['symbol']}|{payload['period']}|{payload['trade_time']}",
            )
            self._upsert_jsonl(
                self.layout.normalized_market_symbol_root / period / f"{trade_date}.jsonl",
                [item.model_dump() for item in records],
                key_func=lambda payload: f"{payload['symbol']}|{payload['period']}|{payload['trade_time']}",
            )
            all_records.extend(records)
        return all_records

    def persist_market_structure_snapshot(self, record: MarketStructureSnapshotRecord) -> None:
        trade_date = self._trade_date_from_iso(record.snapshot_at)
        payload = record.model_dump()
        self._upsert_jsonl(
            self.layout.normalized_market_structure_root / "intraday" / f"{trade_date}.jsonl",
            [payload],
            key_func=lambda item: item["snapshot_at"],
        )

    def persist_event_records(self, event_type: str, records: list[EventRecord]) -> list[EventRecord]:
        if not records:
            return []
        normalized_type = event_type.strip().lower()
        if normalized_type not in {"news", "announcements", "policy"}:
            normalized_type = "news"
        root = {
            "news": self.layout.raw_events_news_root,
            "announcements": self.layout.raw_events_announcements_root,
            "policy": self.layout.raw_events_policy_root,
        }[normalized_type]
        normalized_root = self.layout.normalized_events_root / normalized_type
        by_date: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            event_at = record.event_at or record.generated_at or datetime.now().isoformat()
            trade_date = self._trade_date_from_any(event_at)
            by_date.setdefault(trade_date, []).append(record.model_dump())
        for trade_date, payloads in by_date.items():
            self._upsert_jsonl(
                root / f"{trade_date}.jsonl",
                payloads,
                key_func=lambda payload: payload.get("dedupe_key") or payload["event_id"],
            )
            self._upsert_jsonl(
                normalized_root / f"{trade_date}.jsonl",
                payloads,
                key_func=lambda payload: payload.get("dedupe_key") or payload["event_id"],
            )
        return records

    def persist_event_context(self, trade_date: str, payload: dict) -> None:
        self._write_json(self.layout.features_event_context_root / f"{trade_date}.json", payload)
        self._write_json(self.layout.serving_root / "latest_event_context.json", payload)
        self.refresh_workspace_context()

    def persist_market_context(self, trade_date: str, payload: dict) -> None:
        self._write_json(self.layout.features_market_context_root / f"{trade_date}.json", payload)
        self._write_json(self.layout.serving_root / "latest_market_context.json", payload)
        self.refresh_workspace_context()

    def persist_discussion_context(self, trade_date: str, payload: dict) -> None:
        self._write_json(self.layout.features_discussion_context_root / f"{trade_date}.json", payload)
        self._write_json(self.layout.serving_root / "latest_discussion_context.json", payload)
        self.refresh_workspace_context()

    def persist_monitor_context(self, trade_date: str, payload: dict) -> None:
        self._write_json(self.layout.features_monitor_context_root / f"{trade_date}.json", payload)
        self._write_json(self.layout.serving_root / "latest_monitor_context.json", payload)
        self.refresh_workspace_context()

    def persist_runtime_context(self, trade_date: str, payload: dict) -> None:
        self._write_json(self.layout.features_runtime_context_root / f"{trade_date}.json", payload)
        self._write_json(self.layout.serving_root / "latest_runtime_context.json", payload)
        self.refresh_workspace_context()

    def persist_offline_self_improvement_export(self, payload: dict) -> dict[str, Any]:
        generated_at = str(payload.get("generated_at") or datetime.now().isoformat())
        trade_date = str(
            (payload.get("filters") or {}).get("trade_date")
            or self._trade_date_from_any(generated_at)
        )
        artifact_name = str(payload.get("artifact_name") or "latest_offline_self_improvement_export.json")
        feature_dir = self.layout.features_root / "offline_self_improvement" / trade_date
        self._write_json(feature_dir / artifact_name, payload)
        self._write_json(self.layout.serving_root / "latest_offline_self_improvement_export.json", payload)
        archive_manifest = payload.get("archive_ready_manifest")
        if isinstance(archive_manifest, dict):
            relative_archive_path = self._coerce_relative_storage_path(
                archive_manifest.get("relative_archive_path"),
                root=self.layout.features_root,
            )
            if relative_archive_path is not None:
                self._write_json(relative_archive_path, payload)
            latest_serving_path = self._coerce_relative_storage_path(
                archive_manifest.get("latest_serving_path"),
                root=self.layout.root,
            )
            if latest_serving_path is not None:
                self._write_json(latest_serving_path, payload)
        return self._sanitize_json_compatible(payload)

    def persist_openclaw_packet(self, packet: dict) -> dict[str, Any]:
        packet_type = str(packet.get("packet_type") or "openclaw_packet")
        generated_at = str(packet.get("generated_at") or datetime.now().isoformat())
        trade_date = str(packet.get("trade_date") or self._trade_date_from_any(generated_at))
        packet_id = str(packet.get("packet_id") or f"{packet_type}-{trade_date.replace('-', '')}")
        feature_dir = self.layout.features_root / "openclaw_packets" / trade_date
        self._write_json(feature_dir / f"{packet_id}.json", packet)
        self._write_json(self.layout.serving_root / f"latest_{packet_type}.json", packet)
        archive_manifest = packet.get("archive_manifest")
        if isinstance(archive_manifest, dict):
            archive_path = self._coerce_relative_storage_path(
                archive_manifest.get("archive_path"),
                root=self.layout.features_root,
            )
            if archive_path is not None:
                self._write_json(archive_path, packet)
            for alias in list(archive_manifest.get("latest_aliases") or []):
                latest_alias_path = self._coerce_relative_storage_path(alias, root=self.layout.features_root)
                if latest_alias_path is not None:
                    self._write_json(latest_alias_path, packet)
        return self._sanitize_json_compatible(packet)

    def persist_symbol_contexts(
        self,
        trade_date: str,
        items: list[dict],
        *,
        generated_at: str,
        signature: str = "",
    ) -> list[SymbolContextRecord]:
        records: list[SymbolContextRecord] = []
        context_dir = self.layout.features_symbol_context_root / trade_date
        context_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            symbol = item.get("symbol")
            if not symbol:
                continue
            record = SymbolContextRecord(
                trade_date=trade_date,
                symbol=symbol,
                name=item.get("name", ""),
                signature=signature,
                payload=item,
                **build_freshness_meta(
                    source_at=item.get("generated_at") or generated_at,
                    fetched_at=generated_at,
                    generated_at=generated_at,
                    fresh_seconds=300,
                    expiry_seconds=300,
                ).model_dump(),
            )
            records.append(record)
            self._write_json(context_dir / f"{symbol.replace('.', '_')}.json", record.model_dump())
        self._write_json(
            self.layout.serving_root / "latest_symbol_contexts.json",
            {
                "trade_date": trade_date,
                "generated_at": generated_at,
                "signature": signature,
                "symbol_count": len(records),
                "items": items,
            },
        )
        self.refresh_workspace_context()
        return records

    def persist_dossier_pack(self, pack: dict) -> list[DossierRecord]:
        trade_date = pack.get("trade_date") or self._trade_date_from_iso(pack.get("generated_at") or datetime.now().isoformat())
        pack_dir = self.layout.features_dossiers_root / trade_date
        pack_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(pack_dir / f"{pack.get('pack_id', 'latest-pack')}.json", pack)
        self._write_json(self.layout.serving_root / "latest_dossier_pack.json", pack)
        records: list[DossierRecord] = []
        generated_at = pack.get("generated_at")
        expires_at = pack.get("expires_at")
        for item in pack.get("items", []):
            symbol = item.get("symbol")
            if not symbol:
                continue
            record = DossierRecord(
                trade_date=trade_date,
                symbol=symbol,
                name=item.get("name", ""),
                signature=pack.get("signature", ""),
                payload=item,
                **build_freshness_meta(
                    source_at=generated_at,
                    fetched_at=generated_at,
                    generated_at=generated_at,
                    fresh_seconds=300,
                    expiry_seconds=max(
                        int((self._parse_dt(expires_at) - self._parse_dt(generated_at)).total_seconds()),
                        0,
                    )
                    if generated_at and expires_at and self._parse_dt(generated_at) and self._parse_dt(expires_at)
                    else 300,
                ).model_dump(),
            )
            records.append(record)
            self._write_json(pack_dir / f"{symbol.replace('.', '_')}.json", record.model_dump())
        self.refresh_workspace_context()
        return records

    def persist_workspace_context(self, trade_date: str, payload: dict) -> None:
        self._write_json(self.layout.features_workspace_context_root / f"{trade_date}.json", payload)
        self._write_json(self.layout.serving_root / "latest_workspace_context.json", payload)

    def refresh_workspace_context(self) -> dict[str, Any] | None:
        market_context_raw = self._read_json(self.layout.serving_root / "latest_market_context.json")
        event_context_raw = self._read_json(self.layout.serving_root / "latest_event_context.json")
        symbol_contexts_raw = self._read_json(self.layout.serving_root / "latest_symbol_contexts.json")
        dossier_pack_raw = self._read_json(self.layout.serving_root / "latest_dossier_pack.json")
        discussion_context_raw = self._read_json(self.layout.serving_root / "latest_discussion_context.json")
        monitor_context_raw = self._read_json(self.layout.serving_root / "latest_monitor_context.json")
        runtime_context_raw = self._read_json(self.layout.serving_root / "latest_runtime_context.json")
        contexts = [
            discussion_context_raw,
            runtime_context_raw,
            dossier_pack_raw,
            monitor_context_raw,
            market_context_raw,
            event_context_raw,
            symbol_contexts_raw,
        ]
        available_contexts = [item for item in contexts if item]
        if not available_contexts:
            return None

        candidate_trade_dates = [
            str(item.get("trade_date")).strip()
            for item in available_contexts
            if str(item.get("trade_date") or "").strip()
        ]
        trade_date = max(candidate_trade_dates) if candidate_trade_dates else self._trade_date_from_iso(datetime.now().isoformat())

        def _pick_for_trade_date(payload: dict[str, Any] | None) -> dict[str, Any] | None:
            if not payload:
                return None
            payload_trade_date = str(payload.get("trade_date") or "").strip()
            if payload_trade_date and payload_trade_date != trade_date:
                return None
            return payload

        market_context = _pick_for_trade_date(market_context_raw)
        event_context = _pick_for_trade_date(event_context_raw)
        symbol_contexts = _pick_for_trade_date(symbol_contexts_raw)
        dossier_pack = _pick_for_trade_date(dossier_pack_raw)
        discussion_context = _pick_for_trade_date(discussion_context_raw)
        monitor_context = _pick_for_trade_date(monitor_context_raw)
        runtime_context = _pick_for_trade_date(runtime_context_raw)
        generated_at = datetime.now().isoformat()
        summary_lines = [
            f"workspace trade_date={trade_date} runtime={'yes' if runtime_context else 'no'} discussion={'yes' if discussion_context else 'no'} monitor={'yes' if monitor_context else 'no'} dossier={'yes' if dossier_pack else 'no'}"
        ]
        if runtime_context:
            summary_lines.append(
                f"runtime job={runtime_context.get('job_id')} decisions={runtime_context.get('decision_count', 0)} mode={runtime_context.get('run_mode')}/{runtime_context.get('execution_mode')}"
            )
        if discussion_context:
            cycle = discussion_context.get("cycle") or {}
            summary_lines.append(
                f"discussion status={discussion_context.get('status')} cycle={cycle.get('discussion_state')} cases={discussion_context.get('case_count', 0)}"
            )
        if monitor_context:
            freshness = monitor_context.get("heartbeat_freshness") or {}
            summary_lines.append(
                f"monitor events={monitor_context.get('event_count', 0)} heartbeat_fresh={freshness.get('is_fresh')} candidate_due={(monitor_context.get('polling_status') or {}).get('candidate', {}).get('due_now')}"
            )
        if dossier_pack:
            summary_lines.append(
                f"dossier pack_id={dossier_pack.get('pack_id')} symbols={dossier_pack.get('symbol_count', len(dossier_pack.get('items', [])))}"
            )

        payload = {
            "available": True,
            "resource": "workspace_context",
            "trade_date": trade_date,
            "generated_at": generated_at,
            "status": "ready" if runtime_context or discussion_context or monitor_context else "partial",
            "runtime_context": runtime_context,
            "discussion_context": discussion_context,
            "monitor_context": monitor_context,
            "dossier_pack": dossier_pack,
            "market_context": market_context,
            "event_context": event_context,
            "symbol_contexts": symbol_contexts,
            "summary_lines": summary_lines,
            "summary_text": "\n".join(summary_lines),
        }
        self.persist_workspace_context(trade_date, payload)
        return payload

    @staticmethod
    def _upsert_jsonl(path: Path, payloads: list[dict[str, Any]], key_func) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                merged[key_func(item)] = item
        for payload in payloads:
            merged[key_func(payload)] = payload
        ordered = list(merged.values())
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in ordered) + ("\n" if ordered else ""),
            encoding="utf-8",
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        sanitized = DataArchiveStore._sanitize_json_compatible(payload)
        path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _coerce_relative_storage_path(value: Any, *, root: Path) -> Path | None:
        relative_path = str(value or "").strip().replace("\\", "/")
        if not relative_path:
            return None
        target = Path(relative_path)
        if target.is_absolute():
            return None
        return root / target

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _sanitize_json_compatible(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {key: DataArchiveStore._sanitize_json_compatible(item) for key, item in value.items()}
        if isinstance(value, list):
            return [DataArchiveStore._sanitize_json_compatible(item) for item in value]
        if isinstance(value, tuple):
            return [DataArchiveStore._sanitize_json_compatible(item) for item in value]
        return value

    @staticmethod
    def _change_pct(last_price: float, pre_close: float) -> float:
        if not pre_close:
            return 0.0
        return round((last_price - pre_close) / pre_close, 6)

    @staticmethod
    def _trade_date_from_iso(value: str) -> str:
        return value.split("T", 1)[0]

    @staticmethod
    def _trade_date_from_any(value: str) -> str:
        if "T" in value:
            return value.split("T", 1)[0]
        if len(value) == 8 and value.isdigit():
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return value

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        normalized = value
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _raw_quote_payload(item: QuoteSnapshot, *, source: str, fetched_at: str) -> dict[str, Any]:
        payload = item.model_dump()
        payload["source"] = source
        payload["snapshot_at"] = fetched_at
        payload["fetched_at"] = fetched_at
        return payload
