"""按战法过滤的最小离线回测执行器。

注意：
- 这里只处理离线 trade list 的统计回放。
- 返回中的 attribution 仅是离线回测 attribution，不代表线上真实成交后的事实归因。
- proposal/export packet 只服务离线研究与自我进化，不可直接推 live。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
from pydantic import BaseModel, Field

from .attribution import (
    OFFLINE_BACKTEST_ATTRIBUTION_NOTE,
    BacktestAttributionReport,
    BacktestTradeRecord,
    OfflineBacktestAttributionService,
)
from .engine import BacktestResult, build_trade_records_from_backtest_result
from .metrics import BacktestMetrics, MetricsCalculator

OFFLINE_SELF_IMPROVEMENT_NOTE = (
    "该 proposal/export packet 只服务离线研究与自我进化，不可直接推 live。"
)
LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME = "latest_offline_self_improvement_export.json"
LATEST_OFFLINE_SELF_IMPROVEMENT_DESCRIPTOR_FILENAME = "latest_offline_self_improvement_descriptor.json"
OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK = "self_evolution_research"
OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP = "offline_self_improvement"
ARCHIVE_OFFLINE_SELF_IMPROVEMENT_EXPORT_DIR = "archive/offline_self_improvement"


class PlaybookBacktestRunResult(BaseModel):
    """最小离线回测结果。"""

    available: bool = False
    run_scope: str = "offline_playbook_backtest"
    semantics_note: str = OFFLINE_BACKTEST_ATTRIBUTION_NOTE
    generated_at: str
    filters: dict = Field(default_factory=dict)
    input_source: str = "trade_list"
    trade_count: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_return_pct: float = 0.0
    attribution: BacktestAttributionReport
    metrics: dict = Field(default_factory=dict)
    metrics_export: dict = Field(default_factory=dict)
    self_improvement_proposal: dict = Field(default_factory=dict)
    self_improvement_export: dict = Field(default_factory=dict)
    serving_ready_latest_export: dict = Field(default_factory=dict)
    archive_ready_manifest: dict = Field(default_factory=dict)
    latest_descriptor: dict = Field(default_factory=dict)
    descriptor_contract_sample: dict = Field(default_factory=dict)
    # 兼容旧字段命名
    proposal_packet: dict = Field(default_factory=dict)
    export_packet: dict = Field(default_factory=dict)
    trades: list[BacktestTradeRecord] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


class PlaybookBacktestRunner:
    """按 playbook / regime / exit_reason 运行最小离线回测。"""

    def __init__(self, now_factory: Callable[[], datetime] | None = None) -> None:
        self._now_factory = now_factory or datetime.now
        self._attribution = OfflineBacktestAttributionService(now_factory=self._now_factory)
        self._metrics_calculator = MetricsCalculator()

    def run(
        self,
        trades: list[BacktestTradeRecord | dict] | BacktestResult,
        *,
        playbook: str | None = None,
        regime: str | None = None,
        exit_reason: str | None = None,
        metadata_by_symbol: dict[str, dict] | None = None,
    ) -> PlaybookBacktestRunResult:
        input_source = "trade_list"
        normalized_trades = trades
        if isinstance(trades, BacktestResult):
            input_source = "backtest_engine"
            normalized_trades = build_trade_records_from_backtest_result(
                trades,
                metadata_by_symbol=metadata_by_symbol,
            )
        report = self._attribution.build_report(
            normalized_trades,
            playbook=playbook,
            regime=regime,
            exit_reason=exit_reason,
        )
        filters = dict(report.filters)
        generated_at = self._now_factory().isoformat()
        if not report.available:
            empty_proposal = self._build_empty_self_improvement_proposal(
                generated_at=generated_at,
                filters=filters,
                input_source=input_source,
            )
            empty_export = self._build_self_improvement_export(
                generated_at=generated_at,
                filters=filters,
                input_source=input_source,
                attribution=report,
                metrics_export={},
                proposal=empty_proposal,
                summary_lines=["离线回测无匹配样本，未生成统计结果。"],
            )
            serving_ready_latest_export = self._build_serving_ready_latest_export(export_payload=empty_export)
            archive_ready_manifest = self.build_archive_ready_manifest(serving_ready_latest_export)
            latest_descriptor = self.build_latest_descriptor(
                serving_ready_latest_export,
                archive_manifest=archive_ready_manifest,
            )
            descriptor_contract_sample = self.build_descriptor_contract_sample(
                latest_descriptor,
                archive_manifest=archive_ready_manifest,
            )
            return PlaybookBacktestRunResult(
                available=False,
                generated_at=generated_at,
                filters=filters,
                input_source=input_source,
                attribution=report,
                metrics={},
                metrics_export={},
                self_improvement_proposal=empty_proposal,
                self_improvement_export=empty_export,
                serving_ready_latest_export=serving_ready_latest_export,
                archive_ready_manifest=archive_ready_manifest,
                latest_descriptor=latest_descriptor,
                descriptor_contract_sample=descriptor_contract_sample,
                proposal_packet=empty_proposal,
                export_packet=empty_export,
                summary_lines=["离线回测无匹配样本，未生成统计结果。"],
            )

        metrics = self._build_metrics_payload(report.items)
        metrics_payload = asdict(metrics)
        metrics_export = dict(metrics.export_payload)
        summary_lines = [
            f"离线回测命中 {report.trade_count} 笔样本，总收益 {report.total_return_pct:.2%}，来源 {input_source}。",
            (
                f"metrics 已输出 by_playbook/by_regime/by_exit_reason，"
                f"总交易 {metrics.total_trades}。"
            ),
            "proposal/export packet 仅服务离线研究与自我进化，不可直接推 live。",
            *report.summary_lines,
        ]
        self_improvement_proposal = self._build_self_improvement_proposal(
            generated_at=generated_at,
            filters=filters,
            input_source=input_source,
            attribution=report,
            metrics_export=metrics_export,
            summary_lines=summary_lines,
        )
        self_improvement_export = self._build_self_improvement_export(
            generated_at=generated_at,
            filters=filters,
            input_source=input_source,
            attribution=report,
            metrics_export=metrics_export,
            proposal=self_improvement_proposal,
            summary_lines=summary_lines,
        )
        serving_ready_latest_export = self._build_serving_ready_latest_export(
            export_payload=self_improvement_export,
        )
        archive_ready_manifest = self.build_archive_ready_manifest(serving_ready_latest_export)
        latest_descriptor = self.build_latest_descriptor(
            serving_ready_latest_export,
            archive_manifest=archive_ready_manifest,
        )
        descriptor_contract_sample = self.build_descriptor_contract_sample(
            latest_descriptor,
            archive_manifest=archive_ready_manifest,
        )
        return PlaybookBacktestRunResult(
            available=True,
            generated_at=generated_at,
            filters=filters,
            input_source=input_source,
            trade_count=report.trade_count,
            win_rate=report.win_rate,
            avg_return_pct=report.avg_return_pct,
            total_return_pct=report.total_return_pct,
            attribution=report,
            metrics=metrics_payload,
            metrics_export=metrics_export,
            self_improvement_proposal=self_improvement_proposal,
            self_improvement_export=self_improvement_export,
            serving_ready_latest_export=serving_ready_latest_export,
            archive_ready_manifest=archive_ready_manifest,
            latest_descriptor=latest_descriptor,
            descriptor_contract_sample=descriptor_contract_sample,
            proposal_packet=self_improvement_proposal,
            export_packet=self_improvement_export,
            trades=report.items,
            summary_lines=summary_lines,
        )

    def build_serving_ready_latest_export(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
    ) -> dict:
        """构建可直接落盘为 latest_offline_self_improvement_export.json 的结构。"""
        export_payload = self._coerce_self_improvement_export(result_or_payload)
        export_payload.setdefault("generated_at", self._now_factory().isoformat())
        return self._build_serving_ready_latest_export(export_payload=export_payload)

    def write_latest_offline_self_improvement_export(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
        output_path: str | Path = LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME,
    ) -> Path:
        """把 serving-ready latest export 直接写出为 JSON 文件。"""
        payload = self.build_serving_ready_latest_export(result_or_payload)
        target_path = self._resolve_latest_export_path(
            output_path=output_path,
            artifact_name=str(payload["artifact_name"]),
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target_path

    def build_latest_descriptor(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
        *,
        archive_manifest: dict | None = None,
    ) -> dict:
        """构建 latest descriptor，统一引用 serving latest 与 archive manifest。"""
        serving_payload = self.build_serving_ready_latest_export(result_or_payload)
        manifest = archive_manifest or self.build_archive_ready_manifest(serving_payload)
        serving_artifact = str(
            serving_payload.get("artifact_name") or LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        )
        archive_ref = self.build_archive_ref(serving_payload, archive_manifest=manifest)
        return {
            "version": "v1",
            "descriptor_scope": "offline_self_improvement_latest_descriptor",
            "research_track": OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK,
            "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            "live_execution_allowed": False,
            "offline_only": True,
            "generated_at": self._now_factory().isoformat(),
            "consumers": list(serving_payload.get("consumers") or manifest.get("consumers") or ["main", "openclaw"]),
            "latest": {
                "available": bool(serving_payload.get("available")),
                "artifact_name": serving_artifact,
                "serving_path": f"serving/{serving_artifact}",
                "packet_scope": str(serving_payload.get("packet_scope") or "offline_self_improvement_export"),
                "input_source": str(serving_payload.get("input_source") or "trade_list"),
            },
            "archive_ref": archive_ref,
            "guardrails": {
                "live_execution_allowed": False,
                "research_track": OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK,
                "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            },
        }

    def build_archive_ref(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
        *,
        archive_manifest: dict | None = None,
    ) -> dict:
        """构建 latest descriptor 可直接复用的 archive 引用。"""
        manifest = archive_manifest or self.build_archive_ready_manifest(result_or_payload)
        relative_archive_path = str(manifest.get("relative_archive_path") or "")
        return {
            "manifest_scope": str(manifest.get("manifest_scope") or "offline_self_improvement_archive_manifest"),
            "artifact_name": str(manifest.get("artifact_name") or ""),
            "archive_group": str(manifest.get("archive_group") or ""),
            "relative_archive_path": relative_archive_path,
            "archive_path": relative_archive_path,
            "trade_date": str(manifest.get("trade_date") or ""),
            "research_track": OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK,
            "semantics_note": str(manifest.get("semantics_note") or OFFLINE_SELF_IMPROVEMENT_NOTE),
            "live_execution_allowed": False,
        }

    def build_descriptor_contract_sample(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
        *,
        archive_manifest: dict | None = None,
    ) -> dict:
        """构建 latest descriptor / archive ref 的统一消费样例。"""
        if isinstance(result_or_payload, dict) and result_or_payload.get("descriptor_scope"):
            descriptor = deepcopy(result_or_payload)
        else:
            descriptor = self.build_latest_descriptor(
                result_or_payload,
                archive_manifest=archive_manifest,
            )
        archive_ref = dict(descriptor.get("archive_ref") or {})
        return {
            "contract_scope": "offline_self_improvement_descriptor_contract_sample",
            "descriptor_scope": str(
                descriptor.get("descriptor_scope") or "offline_self_improvement_latest_descriptor"
            ),
            "research_track": OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK,
            "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            "live_execution_allowed": False,
            "latest_descriptor_sample": descriptor,
            "archive_ref_sample": archive_ref,
            "consumption_order": [
                {
                    "step": 1,
                    "target": "serving_latest",
                    "read_from": "latest_descriptor.latest",
                    "purpose": "先定位 latest serving export，再决定是否读取完整 export 包。",
                    "fields": ["artifact_name", "serving_path", "packet_scope", "input_source"],
                },
                {
                    "step": 2,
                    "target": "archive_ref",
                    "read_from": "latest_descriptor.archive_ref",
                    "purpose": "需要历史归档或回放时，再读取 archive 路径引用。",
                    "fields": ["artifact_name", "relative_archive_path", "archive_path", "trade_date"],
                },
                {
                    "step": 3,
                    "target": "guardrails",
                    "read_from": "latest_descriptor.guardrails",
                    "purpose": "最后检查 research_track、semantics_note 与 live guardrail。",
                    "fields": ["research_track", "semantics_note", "live_execution_allowed"],
                },
            ],
            "recommended_fields": {
                "main": {
                    "descriptor_scope": "latest_descriptor.descriptor_scope",
                    "research_track": "latest_descriptor.research_track",
                    "serving_path": "latest_descriptor.latest.serving_path",
                    "latest_artifact_name": "latest_descriptor.latest.artifact_name",
                    "archive_path": "latest_descriptor.archive_ref.archive_path",
                    "trade_date": "latest_descriptor.archive_ref.trade_date",
                    "guardrail": "latest_descriptor.guardrails.live_execution_allowed",
                },
                "openclaw": {
                    "research_track": "latest_descriptor.research_track",
                    "packet_scope": "latest_descriptor.latest.packet_scope",
                    "input_source": "latest_descriptor.latest.input_source",
                    "archive_ref": "latest_descriptor.archive_ref.relative_archive_path",
                    "semantics_note": "latest_descriptor.guardrails.semantics_note",
                    "live_execution_allowed": "latest_descriptor.guardrails.live_execution_allowed",
                },
            },
        }

    def write_latest_offline_self_improvement_descriptor(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
        output_path: str | Path = LATEST_OFFLINE_SELF_IMPROVEMENT_DESCRIPTOR_FILENAME,
    ) -> Path:
        """写出 latest descriptor，供 Linux 主控/离线调度直接消费。"""
        descriptor = self.build_latest_descriptor(result_or_payload)
        target_path = self._resolve_latest_export_path(
            output_path=output_path,
            artifact_name=LATEST_OFFLINE_SELF_IMPROVEMENT_DESCRIPTOR_FILENAME,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target_path

    def build_archive_ready_manifest(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
    ) -> dict:
        """构建离线自我进化导出的 archive-ready manifest。"""
        serving_payload = self.build_serving_ready_latest_export(result_or_payload)
        source_generated_at = str(serving_payload.get("generated_at") or self._now_factory().isoformat())
        trade_date = self._resolve_trade_date(serving_payload)
        archive_group = f"{OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP}/{trade_date}"
        artifact_name = self._build_archive_artifact_name(
            trade_date=trade_date,
            source_generated_at=source_generated_at,
        )
        relative_archive_path = f"{archive_group}/{artifact_name}"
        return {
            "version": "v1",
            "available": bool(serving_payload.get("available")),
            "manifest_scope": "offline_self_improvement_archive_manifest",
            "packet_scope": str(serving_payload.get("packet_scope") or "offline_self_improvement_export"),
            "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            "live_execution_allowed": False,
            "archive_ready": True,
            "offline_only": True,
            "generated_at": self._now_factory().isoformat(),
            "source_generated_at": source_generated_at,
            "trade_date": trade_date,
            "artifact_name": artifact_name,
            "archive_group": archive_group,
            "relative_archive_path": relative_archive_path,
            "latest_serving_path": f"serving/{LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME}",
            "serving_artifact_name": str(
                serving_payload.get("artifact_name") or LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
            ),
            "consumers": list(serving_payload.get("consumers") or ["main", "openclaw"]),
            "archive_tags": ["offline_backtest", "self_improvement", "research_only"],
            "manifest": {
                "packet_scope": str(serving_payload.get("packet_scope") or "offline_self_improvement_export"),
                "input_source": str(serving_payload.get("input_source") or "trade_list"),
                "filters": dict(serving_payload.get("filters") or {}),
                "summary_line_count": len(serving_payload.get("summary_lines") or []),
                "serving_ready": bool(serving_payload.get("serving_ready")),
            },
        }

    def write_archive_offline_self_improvement_export(
        self,
        result_or_payload: PlaybookBacktestRunResult | dict,
        output_dir: str | Path = ARCHIVE_OFFLINE_SELF_IMPROVEMENT_EXPORT_DIR,
    ) -> dict:
        """把 serving-ready export 写入 archive 路径，并返回 manifest。"""
        serving_payload = self.build_serving_ready_latest_export(result_or_payload)
        manifest = self.build_archive_ready_manifest(serving_payload)
        target_path = self._resolve_archive_export_path(
            output_dir=output_dir,
            trade_date=str(manifest["trade_date"]),
            artifact_name=str(manifest["artifact_name"]),
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(serving_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        archived_manifest = deepcopy(manifest)
        archived_manifest["written_path"] = target_path.as_posix()
        archived_manifest["content_type"] = "application/json"
        archived_manifest["encoding"] = "utf-8"
        archived_manifest["write_mode"] = "append_archive"
        return archived_manifest

    def _build_metrics_payload(self, trades: list[BacktestTradeRecord]) -> BacktestMetrics:
        trade_dicts = [item.model_dump() for item in trades]
        equity_curve = self._build_equity_curve_from_trades(trade_dicts)
        return self._metrics_calculator.calc(equity_curve, trade_dicts)

    @staticmethod
    def _build_empty_self_improvement_proposal(
        *,
        generated_at: str,
        filters: dict,
        input_source: str,
    ) -> dict:
        return {
            "available": False,
            "packet_scope": "offline_self_improvement_proposal",
            "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            "live_execution_allowed": False,
            "generated_at": generated_at,
            "filters": dict(filters),
            "input_source": input_source,
            "actions": [],
            "summary_lines": ["无匹配离线样本，未生成自我进化建议。"],
        }

    def _build_self_improvement_proposal(
        self,
        *,
        generated_at: str,
        filters: dict,
        input_source: str,
        attribution: BacktestAttributionReport,
        metrics_export: dict,
        summary_lines: list[str],
    ) -> dict:
        attribution_inputs = attribution.self_improvement_inputs or {}
        metrics_inputs = metrics_export.get("self_improvement_inputs", {})
        weak_dimensions = metrics_inputs.get("weak_dimensions", [])
        selected_metric_bucket = weak_dimensions[0] if weak_dimensions else {}
        selected_weakest_bucket = attribution_inputs.get("weakest_bucket", {})
        selected_compare_view = attribution_inputs.get("selected_compare_view", {})

        actions: list[dict] = []
        if selected_weakest_bucket:
            actions.append(
                {
                    "action_type": "bucket_guardrail",
                    "target_dimension": selected_weakest_bucket.get("dimension", ""),
                    "target_key": selected_weakest_bucket.get("key", ""),
                    "priority": "high",
                    "rationale": "先收紧最弱分桶触发条件并限制仓位，避免弱势样本放大回撤。",
                }
            )
        if selected_metric_bucket:
            actions.append(
                {
                    "action_type": "offline_ablation",
                    "target_dimension": selected_metric_bucket.get("dimension", ""),
                    "target_key": selected_metric_bucket.get("key", ""),
                    "priority": "high",
                    "rationale": "针对低绩效维度做离线参数对比实验，再决定是否降权。",
                }
            )
        actions.append(
            {
                "action_type": "sample_expansion",
                "target_dimension": "global",
                "target_key": "offline_backtest",
                "priority": "medium",
                "rationale": "扩充弱势分桶样本并复跑回测，降低小样本噪声。",
            }
        )

        return {
            "available": True,
            "packet_scope": "offline_self_improvement_proposal",
            "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            "live_execution_allowed": False,
            "generated_at": generated_at,
            "consumers": ["main", "openclaw"],
            "filters": filters,
            "input_source": input_source,
            "summary_lines": summary_lines,
            "proposal": {
                "selected_weakest_bucket": selected_weakest_bucket,
                "selected_compare_view": selected_compare_view,
                "selected_metric_bucket": selected_metric_bucket,
                "actions": actions,
                "metrics_anchor": {
                    "overview": metrics_export.get("overview", {}),
                    "self_improvement_inputs": metrics_inputs,
                },
                "attribution_anchor": {
                    "self_improvement_inputs": attribution_inputs,
                },
            },
        }

    @staticmethod
    def _build_self_improvement_export(
        *,
        generated_at: str,
        filters: dict,
        input_source: str,
        attribution: BacktestAttributionReport,
        metrics_export: dict,
        proposal: dict,
        summary_lines: list[str],
    ) -> dict:
        return {
            "available": bool(proposal.get("available")),
            "packet_scope": "offline_self_improvement_export",
            "semantics_note": OFFLINE_SELF_IMPROVEMENT_NOTE,
            "live_execution_allowed": False,
            "generated_at": generated_at,
            "consumers": ["main", "openclaw"],
            "filters": filters,
            "input_source": input_source,
            "summary_lines": summary_lines,
            "proposal_packet": proposal,
            "attribution": attribution.export_payload,
            "metrics": metrics_export,
        }

    @staticmethod
    def _coerce_self_improvement_export(result_or_payload: PlaybookBacktestRunResult | dict) -> dict:
        if isinstance(result_or_payload, PlaybookBacktestRunResult):
            return deepcopy(result_or_payload.self_improvement_export or result_or_payload.export_packet)
        nested_export = result_or_payload.get("self_improvement_export")
        if isinstance(nested_export, dict):
            return deepcopy(nested_export)
        return deepcopy(result_or_payload)

    @staticmethod
    def _build_serving_ready_latest_export(
        *,
        export_payload: dict,
    ) -> dict:
        payload = deepcopy(export_payload or {})
        payload["packet_scope"] = payload.get("packet_scope", "offline_self_improvement_export")
        payload["live_execution_allowed"] = False
        payload["generated_at"] = payload.get("generated_at") or datetime.now().isoformat()
        payload["serving_ready"] = True
        payload["artifact_name"] = LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        payload["consumers"] = list(payload.get("consumers") or ["main", "openclaw"])
        payload["filters"] = dict(payload.get("filters") or {})
        payload["input_source"] = str(payload.get("input_source") or "trade_list")
        payload["summary_lines"] = list(payload.get("summary_lines") or [])
        payload["attribution"] = dict(payload.get("attribution") or {})
        payload["metrics"] = dict(payload.get("metrics") or {})
        if isinstance(payload.get("proposal_packet"), dict):
            payload["proposal_packet"]["live_execution_allowed"] = False
        else:
            payload["proposal_packet"] = {}
        payload["semantics_note"] = payload.get("semantics_note", OFFLINE_SELF_IMPROVEMENT_NOTE)
        return payload

    @staticmethod
    def _resolve_latest_export_path(
        *,
        output_path: str | Path,
        artifact_name: str,
    ) -> Path:
        path = Path(output_path)
        if path.suffix.lower() == ".json":
            return path
        return path / artifact_name

    @staticmethod
    def _resolve_archive_export_path(
        *,
        output_dir: str | Path,
        trade_date: str,
        artifact_name: str,
    ) -> Path:
        directory = Path(output_dir)
        return directory / trade_date / artifact_name

    @staticmethod
    def _build_archive_artifact_name(*, trade_date: str, source_generated_at: str) -> str:
        trade_date_compact = trade_date.replace("-", "")
        sanitized = source_generated_at.replace("-", "").replace(":", "").replace(".", "")
        sanitized = sanitized.replace("+", "_").replace("T", "T")
        if not sanitized:
            sanitized = datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"offline_self_improvement_export-{trade_date_compact}-{sanitized}.json"

    @staticmethod
    def _resolve_trade_date(serving_payload: dict) -> str:
        filters = serving_payload.get("filters") or {}
        trade_date = filters.get("trade_date")
        if trade_date:
            return str(trade_date)
        generated_at = str(serving_payload.get("generated_at") or "")
        if "T" in generated_at:
            return generated_at.split("T", 1)[0]
        if generated_at:
            return generated_at[:10]
        return datetime.now().date().isoformat()

    @staticmethod
    def _build_equity_curve_from_trades(trades: list[dict]) -> pd.Series:
        if not trades:
            return pd.Series([], dtype=float)
        base = 1.0
        values: list[float] = [base]
        index: list[str] = ["start"]
        for idx, item in enumerate(trades, start=1):
            try:
                return_pct = float(item.get("return_pct", 0.0) or 0.0)
            except (TypeError, ValueError):
                return_pct = 0.0
            base *= 1.0 + return_pct
            values.append(base)
            index.append(str(item.get("trade_date") or f"trade-{idx}"))
        return pd.Series(values, index=index, dtype=float)
