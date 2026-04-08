from __future__ import annotations

import json
from datetime import datetime

from ashare_system.discussion.candidate_case import CandidateCaseService, CandidateOpinion
from ashare_system.discussion.contracts import DiscussionCaseRecord, DiscussionOpinion as HelperOpinion, DiscussionRuntimeSnapshot
from ashare_system.discussion.discussion_service import DiscussionCycleService
from ashare_system.discussion.finalizer import build_finalize_bundle
from ashare_system.discussion.opinion_ingress import adapt_openclaw_opinion_payload, extract_opinion_items
from ashare_system.discussion.opinion_validator import validate_opinion_batch
from ashare_system.discussion.round_summarizer import build_trade_date_summary
from ashare_system.discussion.state_machine import DiscussionStateMachine
from ashare_system.governance.param_registry import ParameterRegistry
from ashare_system.governance.param_service import ParameterService
from ashare_system.governance.param_store import ParameterEventStore


class TestDiscussionContracts:
    def test_contract_models_accept_current_case_shape(self):
        case = DiscussionCaseRecord(
            case_id="case-20260407-600519-SH",
            trade_date="2026-04-07",
            symbol="600519.SH",
            name="贵州茅台",
            runtime_snapshot=DiscussionRuntimeSnapshot(rank=1, selection_score=92.0, action="BUY", summary="强势候选"),
            opinions=[
                HelperOpinion(
                    case_id="case-20260407-600519-SH",
                    round=1,
                    agent_id="ashare-research",
                    stance="support",
                    reasons=["研究支持"],
                    evidence_refs=["news:1"],
                )
            ],
        )
        assert case.symbol == "600519.SH"
        assert case.runtime_snapshot.rank == 1
        assert case.opinions[0].stance == "support"


class TestDiscussionOpinionValidator:
    def test_round_2_batch_requires_substantive_fields(self):
        result = validate_opinion_batch(
            [
                {
                    "case_id": "case-1",
                    "round": 2,
                    "agent_id": "ashare-risk",
                    "stance": "limit",
                    "reasons": ["仍需观察"],
                    "evidence_refs": ["risk:1"],
                }
            ],
            expected_round=2,
            expected_agent_id="ashare-risk",
            expected_case_ids=["case-1"],
        )
        assert result.ok is False
        assert any(item.code == "round_2_not_substantive" for item in result.issues)


class TestDiscussionOpinionIngress:
    def test_extract_items_supports_nested_output_envelope(self):
        items = extract_opinion_items(
            {
                "status": "ok",
                "output": {
                    "items": [
                        {
                            "case_id": "case-1",
                            "round": 1,
                            "agent_id": "ashare-research",
                            "stance": "support",
                            "reasons": ["研究支持"],
                            "evidence_refs": ["news:1"],
                        }
                    ]
                },
            }
        )
        assert len(items) == 1
        assert items[0]["case_id"] == "case-1"

    def test_ingress_fills_missing_round_and_agent_id_from_expected_values(self):
        result = adapt_openclaw_opinion_payload(
            [
                {
                    "case_id": "case-20260407-600519-SH",
                    "stance": "support",
                    "reasons": ["研究支持"],
                    "evidence_refs": ["news:1"],
                }
            ],
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=["case-20260407-600519-SH"],
        )
        assert result["ok"] is True
        assert result["normalized_payloads"][0]["round"] == 1
        assert result["normalized_payloads"][0]["agent_id"] == "ashare-research"
        assert result["writeback_items"][0][1].round == 1
        assert result["writeback_items"][0][1].agent_id == "ashare-research"

    def test_ingress_normalizes_and_builds_writeback_items(self):
        result = adapt_openclaw_opinion_payload(
            {
                "opinions": [
                    {
                        "symbol": "600519.SH",
                        "stance": "selected",
                        "reasons": ["研究支持"],
                        "evidence_refs": ["news:1"],
                    }
                ]
            },
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=["case-20260407-600519-SH"],
            case_id_map={"600519.SH": "case-20260407-600519-SH"},
        )
        assert result["ok"] is True
        assert result["normalized_payloads"][0]["case_id"] == "case-20260407-600519-SH"
        assert result["normalized_payloads"][0]["agent_id"] == "ashare-research"
        assert result["normalized_payloads"][0]["round"] == 1
        assert result["normalized_items"][0]["stance"] == "support"
        assert len(result["writeback_items"]) == 1
        case_id, opinion = result["writeback_items"][0]
        assert case_id == "case-20260407-600519-SH"
        assert opinion.agent_id == "ashare-research"
        assert opinion.stance == "support"

    def test_ingress_reports_missing_case_id_when_symbol_mapping_fails(self):
        result = adapt_openclaw_opinion_payload(
            {
                "opinions": [
                    {
                        "symbol": "600519.SH",
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "reasons": ["研究支持"],
                        "evidence_refs": ["news:1"],
                    }
                ]
            },
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=["case-20260407-600519-SH"],
            case_id_map={"000001.SZ": "case-20260407-000001-SZ"},
        )
        assert result["ok"] is False
        assert result["missing_case_ids"] == ["case-20260407-600519-SH"]
        assert result["writeback_items"] == []
        assert any(item["code"] == "missing_case_coverage" for item in result["issues"])

    def test_ingress_surfaces_invalid_nested_opinion_shape(self):
        result = adapt_openclaw_opinion_payload(
            {
                "output": {
                    "items": [
                        {
                            "case_id": "case-20260407-600519-SH",
                            "agent_id": "ashare-research",
                            "evidence_refs": ["news:1"],
                        }
                    ]
                }
            },
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=["case-20260407-600519-SH"],
        )
        assert result["ok"] is False
        assert result["raw_count"] == 1
        assert result["normalized_payloads"][0]["agent_id"] == "ashare-research"
        assert result["normalized_payloads"][0]["round"] == 1
        assert result["writeback_items"] == []
        assert any(item["code"] == "invalid_schema" for item in result["issues"])


class TestDiscussionRoundSummarizer:
    def test_summary_builds_gate_and_selected_count(self):
        case = DiscussionCaseRecord(
            case_id="case-20260407-600519-SH",
            trade_date="2026-04-07",
            symbol="600519.SH",
            name="贵州茅台",
            runtime_snapshot=DiscussionRuntimeSnapshot(rank=1, selection_score=92.0, action="BUY", summary="强势候选"),
            opinions=[
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-research", stance="support", reasons=["研究支持"], evidence_refs=["news:1"]),
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-strategy", stance="support", reasons=["策略支持"], evidence_refs=["runtime:1"]),
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-risk", stance="support", reasons=["风险可控"], evidence_refs=["risk:1"]),
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-audit", stance="support", reasons=["审计闭环"], evidence_refs=["audit:1"]),
            ],
        )
        summary = build_trade_date_summary([case], "2026-04-07")
        assert summary["selected_count"] == 1
        assert summary["risk_gate_counts"]["allow"] == 1
        assert summary["audit_gate_counts"]["clear"] == 1


class TestDiscussionFinalizer:
    def test_finalize_bundle_outputs_ready_brief(self):
        case = DiscussionCaseRecord(
            case_id="case-20260407-600519-SH",
            trade_date="2026-04-07",
            symbol="600519.SH",
            name="贵州茅台",
            runtime_snapshot=DiscussionRuntimeSnapshot(rank=1, selection_score=92.0, action="BUY", summary="强势候选"),
            opinions=[
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-research", stance="support", reasons=["研究支持"], evidence_refs=["news:1"]),
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-strategy", stance="support", reasons=["策略支持"], evidence_refs=["runtime:1"]),
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-risk", stance="support", reasons=["风险可控"], evidence_refs=["risk:1"]),
                HelperOpinion(case_id="case-20260407-600519-SH", round=1, agent_id="ashare-audit", stance="support", reasons=["审计闭环"], evidence_refs=["audit:1"]),
            ],
        )
        bundle = build_finalize_bundle(trade_date="2026-04-07", cases=[case], include_client_brief=False)
        assert bundle.final_brief["status"] == "ready"
        assert bundle.reply_pack["selected_count"] == 1
        assert bundle.finalize_packet["blocked"] is False


class TestDiscussionServiceIntegration:
    def test_service_uses_helper_summary_and_finalize_entry(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )

        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case_id = case_service.list_cases(trade_date="2026-04-07", limit=1)[0].case_id
        for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
            case_service.record_opinion(
                case_id,
                CandidateOpinion(
                    round=1,
                    agent_id=agent_id,
                    stance="selected",
                    confidence="high",
                    reasons=[f"{agent_id} 支持"],
                    evidence_refs=[f"{agent_id}:1"],
                    recorded_at=clock[0].isoformat(),
                ),
            )
        case_service.rebuild_case(case_id)
        cycle = cycle_service.bootstrap_cycle("2026-04-07")
        summary = cycle_service.build_summary_snapshot("2026-04-07")
        bundle = cycle_service.build_finalize_bundle("2026-04-07", include_client_brief=False)

        assert cycle.summary_snapshot["selected_count"] == 1
        assert summary["selected_count"] == 1
        assert bundle["final_brief"]["status"] == "ready"
        assert bundle["reply_pack"]["selected_count"] == 1

    def test_service_can_adapt_and_write_openclaw_opinions(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case = case_service.list_cases(trade_date="2026-04-07", limit=1)[0]
        preview = cycle_service.adapt_openclaw_opinion_payload(
            {"output": {"items": [{"symbol": "600519.SH", "stance": "selected", "reasons": ["研究支持"], "evidence_refs": ["news:1"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
        )
        assert preview["ok"] is True
        assert preview["case_id_map"] == {"600519.SH": case.case_id}
        assert preview["default_case_id"] == case.case_id
        assert preview["normalized_items"][0]["stance"] == "support"

        persisted = cycle_service.write_openclaw_opinions(
            {"output": {"items": [{"symbol": "600519.SH", "stance": "selected", "reasons": ["研究支持"], "evidence_refs": ["news:1"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
            auto_rebuild=True,
        )
        assert persisted["ok"] is True
        assert persisted["case_id_map"] == {"600519.SH": case.case_id}
        assert persisted["writeback_items"][0][0] == case.case_id
        assert persisted["written_count"] == 1
        assert persisted["written_case_ids"] == [case.case_id]
        assert persisted["rebuilt_case_ids"] == [case.case_id]
        assert persisted["refresh_summary"] is True
        assert persisted["refreshed_summary_snapshot"]["selected_count"] == 0
        assert persisted["touched_case_summaries"][0]["case_id"] == case.case_id
        assert persisted["touched_case_summaries"][0]["opinion_count"] == 1
        assert persisted["touched_case_summaries"][0]["latest_opinion"]["agent_id"] == "ashare-research"
        assert persisted["count"] == 1
        updated = case_service.get_case(case.case_id)
        assert updated is not None
        assert len(updated.opinions) == 1
        assert updated.opinions[0].agent_id == "ashare-research"
        assert updated.opinions[0].stance == "support"
        assert updated.final_status == "watchlist"

    def test_service_write_openclaw_opinions_can_skip_summary_refresh(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case = case_service.list_cases(trade_date="2026-04-07", limit=1)[0]
        result = cycle_service.write_openclaw_opinions(
            {"output": {"items": [{"symbol": "600519.SH", "stance": "selected", "reasons": ["研究支持"], "evidence_refs": ["news:1"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
            auto_rebuild=False,
            refresh_summary=False,
        )
        assert result["ok"] is True
        assert result["refresh_summary"] is False
        assert result["refreshed_summary_snapshot"] == {}
        assert result["written_count"] == 1
        assert result["touched_case_summaries"][0]["case_id"] == case.case_id

    def test_service_write_openclaw_opinions_stops_before_writeback_when_mapping_missing(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                    {"symbol": "000333.SZ", "name": "美的集团", "rank": 2, "selection_score": 88.0, "action": "BUY", "summary": "第二候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        cases = case_service.list_cases(trade_date="2026-04-07", limit=5)
        case = next(item for item in cases if item.symbol == "600519.SH")

        result = cycle_service.write_openclaw_opinions(
            {"output": {"items": [{"symbol": "000001.SZ", "stance": "selected", "reasons": ["映射失败"], "evidence_refs": ["news:404"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
            auto_rebuild=True,
        )

        assert result["ok"] is False
        assert result["written_count"] == 0
        assert result["written_case_ids"] == []
        assert result["rebuilt_case_ids"] == []
        assert result["items"] == []
        updated = case_service.get_case(case.case_id)
        assert updated is not None
        assert updated.opinions == []

    def test_service_can_build_openclaw_replay_proposal_packet_without_live_writeback(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case = case_service.list_cases(trade_date="2026-04-07", limit=1)[0]
        cycle_service.bootstrap_cycle("2026-04-07")

        packet = cycle_service.build_openclaw_replay_proposal_packet(
            {"output": {"items": [{"symbol": "600519.SH", "stance": "selected", "reasons": ["研究支持"], "evidence_refs": ["news:1"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
        )

        assert packet["packet_type"] == "openclaw_replay_proposal"
        assert packet["offline_only"] is True
        assert packet["live_trigger"] is False
        assert packet["packet_id"].startswith("openclaw_replay_proposal-20260407-")
        assert packet["research_track"] == "replay_proposal_dual_track"
        assert any(item["kind"] == "summary_snapshot" for item in packet["source_refs"])
        assert "openclaw" in packet["archive_tags"]
        assert "offline_only" in packet["archive_tags"]
        assert packet["archive_manifest"]["artifact_name"] == f"{packet['packet_id']}.json"
        assert packet["archive_manifest"]["archive_group"] == "openclaw/discussion/20260407"
        assert packet["archive_manifest"]["archive_path"].startswith(
            "openclaw/discussion/20260407/replay_proposal_dual_track/"
        )
        assert "openclaw/discussion/20260407/replay_proposal_dual_track/latest.json" in packet["archive_manifest"]["latest_aliases"]
        suggested_tracks = {item["research_track"] for item in packet["archive_manifest"]["recommended_tracks"]}
        assert {"post_close_replay", "self_evolution_research"} <= suggested_tracks
        assert packet["archive_manifest"]["recommended_tracks"][0]["archive_path_prefix"].endswith("/post_close_replay/")
        assert packet["archive_manifest"]["offline_only"] is True
        assert packet["archive_manifest"]["live_trigger"] is False
        assert cycle_service.build_openclaw_archive_manifest(packet) == packet["archive_manifest"]
        assert packet["latest_descriptor"]["packet_type"] == packet["packet_type"]
        assert packet["latest_descriptor"]["packet_id"] == packet["packet_id"]
        assert packet["latest_descriptor"]["research_track"] == packet["research_track"]
        assert packet["latest_descriptor"]["artifact_name"] == packet["archive_manifest"]["artifact_name"]
        assert packet["latest_descriptor"]["archive_path"] == packet["archive_manifest"]["archive_path"]
        assert packet["latest_descriptor"]["latest_aliases"] == packet["archive_manifest"]["latest_aliases"]
        assert packet["latest_descriptor"]["source_refs"] == packet["source_refs"]
        assert packet["latest_descriptor"]["archive_tags"] == packet["archive_tags"]
        assert packet["latest_descriptor"]["offline_only"] is True
        assert packet["latest_descriptor"]["live_trigger"] is False
        assert cycle_service.build_openclaw_latest_descriptor(packet) == packet["latest_descriptor"]
        assert packet["contract_sample"]["replay_descriptor_minimal"]["packet_type"] == "openclaw_replay_packet"
        assert packet["contract_sample"]["proposal_descriptor_minimal"]["packet_type"] == "openclaw_proposal_packet"
        assert packet["contract_sample"]["replay_descriptor_minimal"]["research_track"] == "post_close_replay"
        assert packet["contract_sample"]["proposal_descriptor_minimal"]["research_track"] == "self_evolution_research"
        assert packet["contract_sample"]["replay_descriptor_minimal"]["archive_path"].startswith(
            "openclaw/discussion/20260407/post_close_replay/"
        )
        assert packet["contract_sample"]["proposal_descriptor_minimal"]["archive_path"].startswith(
            "openclaw/discussion/20260407/self_evolution_research/"
        )
        assert packet["contract_sample"]["manifest_latest_mapping"][0]["relation"] == "equal"
        assert packet["contract_sample"]["linux_openclaw_samples"]["latest_pull"]["expect_descriptor_fields"][0] == "packet_type"
        assert packet["contract_sample"]["linux_openclaw_samples"]["review_reference"]["packet_id"] == packet["packet_id"]
        assert packet["contract_sample"]["linux_openclaw_samples"]["archive_reference"]["archive_group"] == (
            packet["archive_manifest"]["archive_group"]
        )
        assert packet["contract_sample"]["offline_only"] is True
        assert packet["contract_sample"]["live_trigger"] is False
        assert cycle_service.build_openclaw_contract_sample(packet) == packet["contract_sample"]
        assert packet["preview"]["ok"] is True
        assert packet["preview"]["case_id_map"] == {"600519.SH": case.case_id}
        assert packet["writeback_preview"]["count"] == 1
        assert packet["writeback_preview"]["items"][0]["case_id"] == case.case_id
        assert packet["writeback_preview"]["items"][0]["symbol"] == "600519.SH"
        assert packet["summary_snapshot"]["case_count"] == 1
        assert packet["replay_packet"]["offline_only"] is True
        assert packet["proposal_packet"]["live_trigger"] is False
        assert packet["proposal_packet"]["writeback_count"] == 1
        assert any("不直接触发 live" in line for line in packet["proposal_packet"]["summary_lines"])
        json.dumps(packet, ensure_ascii=False)
        updated = case_service.get_case(case.case_id)
        assert updated is not None
        assert updated.opinions == []

    def test_service_builds_openclaw_replay_packet_for_offline_replay_only(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case = case_service.list_cases(trade_date="2026-04-07", limit=1)[0]
        packet = cycle_service.build_openclaw_replay_packet(
            {"output": {"items": [{"symbol": "600519.SH", "stance": "selected", "reasons": ["研究支持"], "evidence_refs": ["news:1"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
        )

        assert packet["packet_type"] == "openclaw_replay_packet"
        assert packet["offline_only"] is True
        assert packet["live_trigger"] is False
        assert packet["packet_id"].startswith("openclaw_replay_packet-20260407-")
        assert packet["research_track"] == "post_close_replay"
        assert any(item["kind"] == "discussion_cycle" for item in packet["source_refs"])
        assert "research_track:post_close_replay" in packet["archive_tags"]
        assert packet["archive_manifest"]["artifact_name"] == f"{packet['packet_id']}.json"
        assert packet["archive_manifest"]["archive_path"].startswith(
            "openclaw/discussion/20260407/post_close_replay/"
        )
        assert "openclaw/discussion/20260407/post_close_replay/latest.json" in packet["archive_manifest"]["latest_aliases"]
        assert packet["archive_manifest"]["research_track"] == "post_close_replay"
        assert packet["archive_manifest"]["recommended_tracks"][1]["archive_path_prefix"].endswith(
            "/self_evolution_research/"
        )
        assert cycle_service.build_openclaw_archive_manifest(packet) == packet["archive_manifest"]
        assert packet["latest_descriptor"]["packet_type"] == packet["packet_type"]
        assert packet["latest_descriptor"]["packet_id"] == packet["packet_id"]
        assert packet["latest_descriptor"]["research_track"] == packet["research_track"]
        assert packet["latest_descriptor"]["artifact_name"] == packet["archive_manifest"]["artifact_name"]
        assert packet["latest_descriptor"]["archive_path"] == packet["archive_manifest"]["archive_path"]
        assert packet["latest_descriptor"]["latest_aliases"] == packet["archive_manifest"]["latest_aliases"]
        assert packet["latest_descriptor"]["source_refs"] == packet["source_refs"]
        assert packet["latest_descriptor"]["archive_tags"] == packet["archive_tags"]
        assert packet["latest_descriptor"]["offline_only"] is True
        assert packet["latest_descriptor"]["live_trigger"] is False
        assert cycle_service.build_openclaw_latest_descriptor(packet) == packet["latest_descriptor"]
        assert packet["contract_sample"]["replay_descriptor_minimal"]["packet_type"] == "openclaw_replay_packet"
        assert packet["contract_sample"]["proposal_descriptor_minimal"]["packet_type"] == "openclaw_proposal_packet"
        assert cycle_service.build_openclaw_contract_sample(packet) == packet["contract_sample"]
        assert packet["preview"]["ok"] is True
        assert packet["writeback_preview"]["count"] == 1
        assert packet["replay_packet"]["mode"] == "post_close_replay"
        assert packet["summary_snapshot"]["trade_date"] == "2026-04-07"
        json.dumps(packet, ensure_ascii=False)
        updated = case_service.get_case(case.case_id)
        assert updated is not None
        assert updated.opinions == []

    def test_service_builds_openclaw_proposal_packet_for_research_only(self, tmp_path):
        clock = [datetime(2026, 4, 7, 10, 0, 0)]
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        param_service = ParameterService(
            registry=ParameterRegistry(),
            store=ParameterEventStore(tmp_path / "params.json"),
        )
        cycle_service = DiscussionCycleService(
            tmp_path / "discussion_cycles.json",
            candidate_case_service=case_service,
            parameter_service=param_service,
            now_factory=lambda: clock[0],
        )
        case_service.sync_from_runtime_report(
            {
                "job_id": "runtime-1",
                "generated_at": "2026-04-07T10:00:00",
                "top_picks": [
                    {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 92.0, "action": "BUY", "summary": "强势候选"},
                ],
            },
            focus_pool_capacity=5,
            execution_pool_capacity=3,
        )
        case = case_service.list_cases(trade_date="2026-04-07", limit=1)[0]
        packet = cycle_service.build_openclaw_proposal_packet(
            {"output": {"items": [{"symbol": "600519.SH", "stance": "selected", "reasons": ["研究支持"], "evidence_refs": ["news:1"]}]}},
            trade_date="2026-04-07",
            expected_round=1,
            expected_agent_id="ashare-research",
            expected_case_ids=[case.case_id],
        )

        assert packet["packet_type"] == "openclaw_proposal_packet"
        assert packet["offline_only"] is True
        assert packet["live_trigger"] is False
        assert packet["packet_id"].startswith("openclaw_proposal_packet-20260407-")
        assert packet["research_track"] == "self_evolution_research"
        assert any(item["kind"] == "openclaw_preview" for item in packet["source_refs"])
        assert "research_track:self_evolution_research" in packet["archive_tags"]
        assert packet["archive_manifest"]["artifact_name"] == f"{packet['packet_id']}.json"
        assert packet["archive_manifest"]["archive_path"].startswith(
            "openclaw/discussion/20260407/self_evolution_research/"
        )
        assert "openclaw/discussion/20260407/self_evolution_research/latest.json" in packet["archive_manifest"]["latest_aliases"]
        assert packet["archive_manifest"]["research_track"] == "self_evolution_research"
        assert cycle_service.build_openclaw_archive_manifest(packet) == packet["archive_manifest"]
        assert packet["latest_descriptor"]["packet_type"] == packet["packet_type"]
        assert packet["latest_descriptor"]["packet_id"] == packet["packet_id"]
        assert packet["latest_descriptor"]["research_track"] == packet["research_track"]
        assert packet["latest_descriptor"]["artifact_name"] == packet["archive_manifest"]["artifact_name"]
        assert packet["latest_descriptor"]["archive_path"] == packet["archive_manifest"]["archive_path"]
        assert packet["latest_descriptor"]["latest_aliases"] == packet["archive_manifest"]["latest_aliases"]
        assert packet["latest_descriptor"]["source_refs"] == packet["source_refs"]
        assert packet["latest_descriptor"]["archive_tags"] == packet["archive_tags"]
        assert packet["latest_descriptor"]["offline_only"] is True
        assert packet["latest_descriptor"]["live_trigger"] is False
        assert cycle_service.build_openclaw_latest_descriptor(packet) == packet["latest_descriptor"]
        assert packet["contract_sample"]["replay_descriptor_minimal"]["packet_type"] == "openclaw_replay_packet"
        assert packet["contract_sample"]["proposal_descriptor_minimal"]["packet_type"] == "openclaw_proposal_packet"
        assert cycle_service.build_openclaw_contract_sample(packet) == packet["contract_sample"]
        assert packet["preview"]["ok"] is True
        assert packet["writeback_preview"]["count"] == 1
        assert packet["proposal_packet"]["mode"] == "self_evolution_research"
        assert packet["proposal_packet"]["writeback_candidates"][0]["case_id"] == case.case_id
        json.dumps(packet, ensure_ascii=False)
        updated = case_service.get_case(case.case_id)
        assert updated is not None
        assert updated.opinions == []

class TestDiscussionStateMachineGuards:
    def test_state_machine_guard_methods_follow_summary(self):
        pending_summary = {"round_coverage": {"needs_round_2": 1}}
        ready_summary = {"round_coverage": {"needs_round_2": 0}}
        assert DiscussionStateMachine.needs_round_2(pending_summary) is True
        assert DiscussionStateMachine.can_finalize_from_summary("round_1_summarized", pending_summary) is False
        assert DiscussionStateMachine.can_finalize_from_summary("round_1_summarized", ready_summary) is True
