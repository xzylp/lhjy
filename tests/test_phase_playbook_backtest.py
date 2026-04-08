"""离线战法回测 attribution 测试。"""

import json
from datetime import datetime

import pandas as pd

from ashare_system.backtest.attribution import (
    OFFLINE_BACKTEST_ATTRIBUTION_NOTE,
    OfflineBacktestAttributionService,
)
from ashare_system.backtest.engine import BacktestMetrics, BacktestResult
from ashare_system.backtest.metrics import MetricsCalculator
from ashare_system.backtest.playbook_runner import (
    ARCHIVE_OFFLINE_SELF_IMPROVEMENT_EXPORT_DIR,
    LATEST_OFFLINE_SELF_IMPROVEMENT_DESCRIPTOR_FILENAME,
    LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME,
    OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP,
    OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK,
    PlaybookBacktestRunner,
)


def _sample_trades() -> list[dict]:
    return [
        {
            "trade_id": "t-1",
            "symbol": "600519.SH",
            "playbook": "leader_chase",
            "regime": "trend",
            "exit_reason": "time_stop",
            "return_pct": 0.05,
            "holding_days": 2,
            "trade_date": "2026-04-01",
        },
        {
            "trade_id": "t-2",
            "symbol": "000001.SZ",
            "playbook": "leader_chase",
            "regime": "trend",
            "exit_reason": "board_break",
            "return_pct": -0.02,
            "holding_days": 1,
            "trade_date": "2026-04-01",
        },
        {
            "trade_id": "t-3",
            "symbol": "600036.SH",
            "playbook": "divergence_reseal",
            "regime": "range",
            "exit_reason": "time_stop",
            "return_pct": 0.01,
            "holding_days": 3,
            "trade_date": "2026-04-02",
        },
    ]


class TestPlaybookBacktestRunner:
    def test_runner_filters_by_playbook_regime_and_exit_reason(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(
            _sample_trades(),
            playbook="leader_chase",
            regime="trend",
            exit_reason="time_stop",
        )

        assert result.available is True
        assert result.trade_count == 1
        assert result.total_return_pct == 0.05
        assert result.attribution.attribution_scope == "offline_backtest"
        assert result.attribution.semantics_note == OFFLINE_BACKTEST_ATTRIBUTION_NOTE
        assert result.filters["playbook"] == "leader_chase"
        assert result.filters["regime"] == "trend"
        assert result.filters["exit_reason"] == "time_stop"
        assert result.filters["weakest_bucket_sort_by"] == "avg_return_pct"
        assert result.filters["compare_bucket_sort_order"] == "desc"
        assert result.metrics["metrics_scope"] == "offline_backtest_metrics"
        assert result.metrics_export["overview"]["total_trades"] == 1
        assert result.metrics_export["by_playbook"][0]["key"] == "leader_chase"
        assert result.metrics_export["self_improvement_inputs"]["live_promotion_allowed"] is False
        assert result.attribution.self_improvement_inputs["live_promotion_allowed"] is False
        assert result.self_improvement_proposal["packet_scope"] == "offline_self_improvement_proposal"
        assert result.self_improvement_proposal["live_execution_allowed"] is False
        assert result.self_improvement_proposal["proposal"]["selected_compare_view"]["dimension"] == "playbook"
        assert result.self_improvement_proposal["proposal"]["selected_metric_bucket"]["dimension"] in {
            "playbook",
            "regime",
            "exit_reason",
        }
        assert result.self_improvement_export["packet_scope"] == "offline_self_improvement_export"
        assert result.self_improvement_export["live_execution_allowed"] is False
        assert result.self_improvement_export["proposal_packet"] == result.self_improvement_proposal
        assert result.self_improvement_export["metrics"]["overview"]["total_trades"] == 1
        assert result.self_improvement_export["attribution"]["overview"]["trade_count"] == 1
        assert result.serving_ready_latest_export["serving_ready"] is True
        assert result.serving_ready_latest_export["artifact_name"] == LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        assert result.serving_ready_latest_export["packet_scope"] == "offline_self_improvement_export"
        assert result.serving_ready_latest_export["live_execution_allowed"] is False
        assert result.serving_ready_latest_export["proposal_packet"] == result.self_improvement_proposal
        assert result.serving_ready_latest_export["metrics"]["overview"]["total_trades"] == 1
        assert result.archive_ready_manifest["manifest_scope"] == "offline_self_improvement_archive_manifest"
        assert result.archive_ready_manifest["packet_scope"] == "offline_self_improvement_export"
        assert result.archive_ready_manifest["trade_date"] == "2026-04-07"
        assert result.archive_ready_manifest["relative_archive_path"].startswith(
            f"{OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP}/2026-04-07/"
        )
        assert result.archive_ready_manifest["latest_serving_path"] == (
            f"serving/{LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME}"
        )
        assert result.archive_ready_manifest["live_execution_allowed"] is False
        assert result.latest_descriptor["descriptor_scope"] == "offline_self_improvement_latest_descriptor"
        assert result.latest_descriptor["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK
        assert result.latest_descriptor["live_execution_allowed"] is False
        assert result.latest_descriptor["latest"]["artifact_name"] == LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        assert result.latest_descriptor["latest"]["serving_path"] == (
            f"serving/{LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME}"
        )
        assert result.latest_descriptor["archive_ref"]["relative_archive_path"] == (
            result.archive_ready_manifest["relative_archive_path"]
        )
        assert result.latest_descriptor["archive_ref"]["archive_path"] == (
            result.archive_ready_manifest["relative_archive_path"]
        )
        assert result.latest_descriptor["archive_ref"]["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK
        assert result.descriptor_contract_sample["contract_scope"] == (
            "offline_self_improvement_descriptor_contract_sample"
        )
        assert result.descriptor_contract_sample["latest_descriptor_sample"] == result.latest_descriptor
        assert result.descriptor_contract_sample["archive_ref_sample"] == result.latest_descriptor["archive_ref"]
        assert result.descriptor_contract_sample["consumption_order"][0]["target"] == "serving_latest"
        assert result.descriptor_contract_sample["recommended_fields"]["main"]["serving_path"] == (
            "latest_descriptor.latest.serving_path"
        )
        assert result.proposal_packet == result.self_improvement_proposal
        assert result.export_packet == result.self_improvement_export
        assert "离线回测命中 1 笔样本" in result.summary_lines[0]
        assert result.summary_lines[1].startswith("metrics 已输出 by_playbook")
        assert "不可直接推 live" in result.summary_lines[2]

    def test_runner_returns_empty_result_when_no_match(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="low_suction")

        assert result.available is False
        assert result.trade_count == 0
        assert result.attribution.available is False
        assert result.metrics == {}
        assert result.metrics_export == {}
        assert result.self_improvement_proposal["available"] is False
        assert result.self_improvement_proposal["live_execution_allowed"] is False
        assert result.self_improvement_export["available"] is False
        assert result.serving_ready_latest_export["available"] is False
        assert result.serving_ready_latest_export["serving_ready"] is True
        assert result.serving_ready_latest_export["live_execution_allowed"] is False
        assert result.archive_ready_manifest["available"] is False
        assert result.archive_ready_manifest["offline_only"] is True
        assert result.latest_descriptor["latest"]["available"] is False
        assert result.latest_descriptor["guardrails"]["live_execution_allowed"] is False
        assert result.descriptor_contract_sample["latest_descriptor_sample"]["latest"]["available"] is False
        assert result.proposal_packet == result.self_improvement_proposal
        assert result.export_packet == result.self_improvement_export
        assert result.summary_lines == ["离线回测无匹配样本，未生成统计结果。"]

    def test_runner_can_consume_backtest_engine_result(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        engine_result = BacktestResult(
            metrics=BacktestMetrics(),
            equity_curve=None,
            trades=[
                {"date": "2026-04-01", "symbol": "600519.SH", "side": "BUY", "price": 10.0, "qty": 100, "pnl": 0},
                {"date": "2026-04-03", "symbol": "600519.SH", "side": "SELL", "price": 10.5, "qty": 100, "pnl": 50.0},
            ],
            positions={},
        )

        result = runner.run(
            engine_result,
            playbook="leader_chase",
            metadata_by_symbol={
                "600519.SH": {
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                }
            },
        )

        assert result.available is True
        assert result.input_source == "backtest_engine"
        assert result.trade_count == 1
        assert result.trades[0].return_pct == 0.05
        assert result.trades[0].playbook == "leader_chase"
        assert result.metrics_export["overview"]["total_trades"] == 1
        assert result.metrics_export["by_exit_reason"][0]["key"] == "time_stop"
        assert result.proposal_packet["proposal"]["metrics_anchor"]["self_improvement_inputs"]["weak_dimensions"][0]["dimension"] in {"playbook", "regime", "exit_reason"}
        assert result.proposal_packet["proposal"]["attribution_anchor"]["self_improvement_inputs"]["weakest_bucket"]["key"] in {"leader_chase", "trend", "time_stop"}
        assert result.self_improvement_export["input_source"] == "backtest_engine"
        assert result.proposal_packet == result.self_improvement_proposal
        assert result.serving_ready_latest_export["input_source"] == "backtest_engine"
        assert result.serving_ready_latest_export["packet_scope"] == "offline_self_improvement_export"
        assert result.archive_ready_manifest["manifest"]["input_source"] == "backtest_engine"
        assert result.latest_descriptor["latest"]["input_source"] == "backtest_engine"
        assert "来源 backtest_engine" in result.summary_lines[0]

    def test_runner_serving_ready_helper_enforces_offline_live_guardrail(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase", regime="trend", exit_reason="time_stop")
        result.self_improvement_export["live_execution_allowed"] = True
        result.self_improvement_export["proposal_packet"]["live_execution_allowed"] = True

        latest_export = runner.build_serving_ready_latest_export(result)

        assert latest_export["artifact_name"] == LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        assert latest_export["serving_ready"] is True
        assert latest_export["live_execution_allowed"] is False
        assert latest_export["proposal_packet"]["live_execution_allowed"] is False

    def test_runner_can_write_serving_ready_latest_export(self, tmp_path):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")

        serving_payload = runner.build_serving_ready_latest_export(result)
        output_path = runner.write_latest_offline_self_improvement_export(result, tmp_path / "serving")

        assert output_path.name == LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        assert output_path.exists()

        written_payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert written_payload == serving_payload
        assert written_payload["serving_ready"] is True
        assert written_payload["packet_scope"] == "offline_self_improvement_export"
        assert written_payload["live_execution_allowed"] is False
        assert written_payload["proposal_packet"] == result.self_improvement_proposal
        assert written_payload["metrics"]["overview"]["total_trades"] == 2

    def test_runner_can_build_archive_ready_manifest(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")

        manifest = runner.build_archive_ready_manifest(result)

        assert manifest["manifest_scope"] == "offline_self_improvement_archive_manifest"
        assert manifest["archive_ready"] is True
        assert manifest["offline_only"] is True
        assert manifest["live_execution_allowed"] is False
        assert manifest["packet_scope"] == "offline_self_improvement_export"
        assert manifest["artifact_name"].startswith("offline_self_improvement_export-20260407-")
        assert manifest["artifact_name"].endswith(".json")
        assert manifest["archive_group"] == f"{OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP}/2026-04-07"
        assert manifest["relative_archive_path"] == (
            f"{OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP}/2026-04-07/{manifest['artifact_name']}"
        )
        assert manifest["latest_serving_path"] == f"serving/{LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME}"
        assert manifest["consumers"] == ["main", "openclaw"]
        assert manifest["manifest"]["packet_scope"] == "offline_self_improvement_export"
        assert manifest["manifest"]["serving_ready"] is True

    def test_runner_can_write_archive_offline_self_improvement_export(self, tmp_path):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")

        archived = runner.write_archive_offline_self_improvement_export(result, tmp_path / "archives")

        assert archived["manifest_scope"] == "offline_self_improvement_archive_manifest"
        assert archived["live_execution_allowed"] is False
        assert archived["write_mode"] == "append_archive"
        assert archived["content_type"] == "application/json"
        assert archived["encoding"] == "utf-8"
        written_path = tmp_path / "archives" / "2026-04-07" / archived["artifact_name"]
        assert archived["relative_archive_path"] == (
            f"{OFFLINE_SELF_IMPROVEMENT_ARCHIVE_GROUP}/2026-04-07/{archived['artifact_name']}"
        )
        assert archived["written_path"] == written_path.as_posix()
        assert written_path.exists()
        written_payload = json.loads(written_path.read_text(encoding="utf-8"))
        assert written_payload["artifact_name"] == LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        assert written_payload["serving_ready"] is True
        assert written_payload["live_execution_allowed"] is False

    def test_runner_can_build_latest_descriptor_and_keep_live_guardrail(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")
        result.serving_ready_latest_export["live_execution_allowed"] = True
        result.archive_ready_manifest["live_execution_allowed"] = True

        descriptor = runner.build_latest_descriptor(
            result.serving_ready_latest_export,
            archive_manifest=result.archive_ready_manifest,
        )

        assert descriptor["descriptor_scope"] == "offline_self_improvement_latest_descriptor"
        assert descriptor["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK
        assert descriptor["live_execution_allowed"] is False
        assert descriptor["latest"]["serving_path"] == f"serving/{LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME}"
        assert descriptor["archive_ref"]["relative_archive_path"] == result.archive_ready_manifest["relative_archive_path"]
        assert descriptor["archive_ref"]["archive_path"] == result.archive_ready_manifest["relative_archive_path"]
        assert descriptor["archive_ref"]["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK
        assert descriptor["guardrails"]["live_execution_allowed"] is False
        assert descriptor["guardrails"]["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK

    def test_runner_can_build_archive_ref_directly(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")

        archive_ref = runner.build_archive_ref(result)

        assert archive_ref["manifest_scope"] == "offline_self_improvement_archive_manifest"
        assert archive_ref["relative_archive_path"] == result.archive_ready_manifest["relative_archive_path"]
        assert archive_ref["archive_path"] == result.archive_ready_manifest["relative_archive_path"]
        assert archive_ref["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK
        assert archive_ref["live_execution_allowed"] is False

    def test_runner_can_build_descriptor_contract_sample(self):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")

        contract_sample = runner.build_descriptor_contract_sample(
            result.latest_descriptor,
            archive_manifest=result.archive_ready_manifest,
        )

        assert contract_sample["contract_scope"] == "offline_self_improvement_descriptor_contract_sample"
        assert contract_sample["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK
        assert contract_sample["live_execution_allowed"] is False
        assert contract_sample["latest_descriptor_sample"] == result.latest_descriptor
        assert contract_sample["archive_ref_sample"] == result.latest_descriptor["archive_ref"]
        assert contract_sample["consumption_order"][0]["read_from"] == "latest_descriptor.latest"
        assert contract_sample["consumption_order"][1]["read_from"] == "latest_descriptor.archive_ref"
        assert contract_sample["consumption_order"][2]["read_from"] == "latest_descriptor.guardrails"
        assert contract_sample["recommended_fields"]["main"]["archive_path"] == (
            "latest_descriptor.archive_ref.archive_path"
        )
        assert contract_sample["recommended_fields"]["openclaw"]["archive_ref"] == (
            "latest_descriptor.archive_ref.relative_archive_path"
        )

    def test_runner_can_write_latest_offline_self_improvement_descriptor(self, tmp_path):
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 10, 0, 0))
        result = runner.run(_sample_trades(), playbook="leader_chase")

        descriptor = runner.build_latest_descriptor(result)
        output_path = runner.write_latest_offline_self_improvement_descriptor(result, tmp_path / "serving")

        assert output_path.name == LATEST_OFFLINE_SELF_IMPROVEMENT_DESCRIPTOR_FILENAME
        assert output_path.exists()
        written = json.loads(output_path.read_text(encoding="utf-8"))
        assert written == descriptor
        assert written["live_execution_allowed"] is False
        assert written["latest"]["artifact_name"] == LATEST_OFFLINE_SELF_IMPROVEMENT_EXPORT_FILENAME
        assert written["archive_ref"]["research_track"] == OFFLINE_SELF_IMPROVEMENT_RESEARCH_TRACK


class TestOfflineBacktestAttributionService:
    def test_build_report_groups_by_playbook_regime_and_exit_reason(self):
        service = OfflineBacktestAttributionService(now_factory=lambda: datetime(2026, 4, 7, 10, 30, 0))
        report = service.build_report(_sample_trades())

        assert report.available is True
        assert report.trade_count == 3
        assert report.total_return_pct == 0.04
        assert report.avg_return_pct == round(0.04 / 3, 6)
        assert report.by_playbook[0].key == "leader_chase"
        assert report.by_playbook[0].trade_count == 2
        assert report.by_regime[0].key == "trend"
        assert report.by_exit_reason[0].key == "time_stop"
        assert report.by_exit_reason[0].trade_count == 2
        assert report.semantics_note == OFFLINE_BACKTEST_ATTRIBUTION_NOTE
        assert report.summary_lines[0].startswith("离线回测样本 3 笔")
        assert report.overview["attribution_scope"] == "offline_backtest"
        assert report.overview["weakest_bucket_count"] == 3
        assert report.export_payload["overview"]["trade_count"] == 3
        assert report.export_payload["by_playbook"][0]["key"] == "leader_chase"
        assert report.weakest_buckets[0].dimension == "exit_reason"
        assert report.weakest_buckets[0].key == "board_break"
        assert report.compare_views["playbook"].best_bucket["key"] == "leader_chase"
        assert report.compare_views["playbook"].weakest_bucket["key"] == "divergence_reseal"
        assert report.compare_views["exit_reason"].weakest_bucket["key"] == "board_break"
        assert report.compare_views["exit_reason"].spread_return_pct == 0.05
        assert report.self_improvement_inputs["live_promotion_allowed"] is False
        assert report.self_improvement_inputs["weakest_bucket"]["key"] == "board_break"
        assert report.export_payload["weakest_buckets"][0]["key"] == "board_break"
        assert report.export_payload["self_improvement_inputs"]["weakest_bucket"]["key"] == "board_break"
        assert report.export_payload["compare_views"]["regime"]["dimension"] == "regime"
        assert any(line.startswith("当前最弱分桶是") for line in report.summary_lines)

    def test_build_report_can_select_exact_weakest_bucket_and_compare_view(self):
        service = OfflineBacktestAttributionService(now_factory=lambda: datetime(2026, 4, 7, 10, 30, 0))
        report = service.build_report(
            _sample_trades(),
            weakest_bucket_dimension="regime",
            compare_view_dimension="playbook",
            weakest_bucket_sort_by="key",
            weakest_bucket_sort_order="desc",
            compare_bucket_sort_by="avg_return_pct",
            compare_bucket_sort_order="desc",
        )

        assert report.selected_weakest_bucket["dimension"] == "regime"
        assert report.selected_weakest_bucket["key"] == "range"
        assert report.selected_compare_view["dimension"] == "playbook"
        assert report.selected_compare_view["buckets"][0]["key"] == "leader_chase"
        assert report.compare_views["playbook"].buckets[0]["key"] == "leader_chase"
        assert report.filters["weakest_bucket_dimension"] == "regime"
        assert report.filters["compare_view_dimension"] == "playbook"
        assert report.filters["compare_bucket_sort_by"] == "avg_return_pct"
        assert report.overview["selected_weakest_bucket"]["key"] == "range"
        assert report.export_payload["selected_compare_view"]["dimension"] == "playbook"
        assert report.summary_lines[-2].startswith("已选最弱分桶")
        assert report.summary_lines[-1].startswith("已选对比视图")


class TestBacktestMetricsOfflineDimensions:
    def test_metrics_exposes_offline_by_playbook_regime_and_exit_reason(self):
        calc = MetricsCalculator()
        equity_curve = pd.Series(
            [1_000_000.0, 1_030_000.0, 1_010_000.0, 1_020_000.0],
            index=["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04"],
        )
        metrics = calc.calc(equity_curve, _sample_trades())

        assert metrics.total_trades == 3

        by_playbook = {item["key"]: item for item in metrics.win_rate_by_playbook}
        assert by_playbook["leader_chase"]["trade_count"] == 2
        assert by_playbook["leader_chase"]["win_rate"] == 0.5
        assert metrics.by_playbook()[0]["key"] == "leader_chase"

        by_regime = {item["key"]: item for item in metrics.avg_return_by_regime}
        assert by_regime["trend"]["avg_return_pct"] == 0.015
        assert by_regime["range"]["avg_return_pct"] == 0.01
        assert metrics.by_regime()[0]["key"] == "trend"

        by_exit_reason = {item["key"]: item for item in metrics.exit_reason_distribution}
        assert by_exit_reason["time_stop"]["trade_count"] == 2
        assert by_exit_reason["time_stop"]["ratio"] == round(2 / 3, 6)
        assert metrics.by_exit_reason()[0]["key"] == "time_stop"

        calmar_by_playbook = {item["key"]: item for item in metrics.calmar_by_playbook}
        assert "leader_chase" in calmar_by_playbook
        assert calmar_by_playbook["leader_chase"]["trade_count"] == 2
        assert metrics.metrics_scope == "offline_backtest_metrics"
        assert metrics.export_payload["overview"]["total_trades"] == 3
        assert metrics.self_improvement_inputs["live_promotion_allowed"] is False
        assert metrics.self_improvement_inputs["weakest_by_dimension"]["playbook"]["key"] == "divergence_reseal"
        assert metrics.export_payload["self_improvement_inputs"]["weak_dimensions"][0]["dimension"] in {
            "playbook",
            "regime",
            "exit_reason",
        }
        assert metrics.export_payload["by_playbook"][0]["key"] == "leader_chase"
        assert metrics.export_payload["by_exit_reason"][0]["key"] == "time_stop"

    def test_metrics_keeps_offline_dimension_splits_without_valid_equity_curve(self):
        calc = MetricsCalculator()
        empty_equity = pd.Series([], dtype=float)

        metrics = calc.calc(empty_equity, _sample_trades())

        assert metrics.total_return == 0.0
        assert metrics.total_trades == 3
        assert metrics.by_playbook()[0]["key"] == "leader_chase"
        assert metrics.by_regime()[0]["key"] == "trend"
        assert metrics.by_exit_reason()[0]["key"] == "time_stop"
        assert metrics.export_payload["overview"]["sharpe_ratio"] == 0.0
