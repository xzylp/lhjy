"""交易归因与学习闭环聚合。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from ..infra.safe_json import atomic_write_json, read_json_with_backup
from .trade_review import TradeReviewer


class TradeAttributionRecord(BaseModel):
    trade_date: str
    score_date: str
    symbol: str
    name: str = ""
    account_id: str = ""
    case_id: str | None = None
    order_id: str | None = None
    side: str = ""
    source: str = "settlement"
    status: str = ""
    playbook: str = "unassigned"
    regime: str = "unknown"
    exit_reason: str = "unlabeled"
    next_day_close_pct: float = 0.0
    holding_return_pct: float = 0.0
    max_drawdown_during_hold: float = 0.0
    exit_price: float | None = None
    note: str = ""
    selection_score: float | None = None
    rank: int | None = None
    final_status: str = ""
    risk_gate: str = ""
    audit_gate: str = ""
    holding_days: int = 1
    filled_quantity: int = 0
    filled_value: float = 0.0
    avg_fill_price: float | None = None
    submitted_at: str | None = None
    reconciled_at: str | None = None
    exit_context_snapshot: dict = Field(default_factory=dict)
    review_tags: list[str] = Field(default_factory=list)
    recorded_at: str


class AttributionBucketSummary(BaseModel):
    key: str
    trade_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    flat_count: int = 0
    win_rate: float = 0.0
    avg_next_day_close_pct: float = 0.0
    avg_selection_score: float | None = None
    sample_symbols: list[str] = Field(default_factory=list)


class TradeAttributionReport(BaseModel):
    available: bool = False
    trade_date: str | None = None
    score_date: str | None = None
    generated_at: str
    filters: dict = Field(default_factory=dict)
    trade_count: int = 0
    avg_next_day_close_pct: float = 0.0
    win_rate: float = 0.0
    review_summary: dict = Field(default_factory=dict)
    review_tag_summary: list[dict] = Field(default_factory=list)
    parameter_hints: list[dict] = Field(default_factory=list)
    by_symbol: list[AttributionBucketSummary] = Field(default_factory=list)
    by_reason: list[AttributionBucketSummary] = Field(default_factory=list)
    by_playbook: list[AttributionBucketSummary] = Field(default_factory=list)
    by_regime: list[AttributionBucketSummary] = Field(default_factory=list)
    by_exit_reason: list[AttributionBucketSummary] = Field(default_factory=list)
    items: list[TradeAttributionRecord] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


class TradeAttributionService:
    """按战法、市场状态、退出原因聚合归因结果。"""

    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now
        self._reviewer = TradeReviewer()

    def record_outcomes(
        self,
        trade_date: str,
        score_date: str,
        items: list[TradeAttributionRecord | dict],
        holding_outcomes: list[dict] | None = None,
    ) -> TradeAttributionReport:
        payload = self._read_payload()
        records = [TradeAttributionRecord.model_validate(item) for item in payload.get("records", [])]
        record_map = {
            self._record_key(item.trade_date, item.score_date, item.symbol): item
            for item in records
        }
        for item in items:
            record = item if isinstance(item, TradeAttributionRecord) else TradeAttributionRecord.model_validate(item)
            record_map[self._record_key(record.trade_date, record.score_date, record.symbol)] = record
        if holding_outcomes:
            self._apply_holding_outcomes(record_map, holding_outcomes)
        persisted = sorted(
            record_map.values(),
            key=lambda item: (item.trade_date, item.score_date, item.rank or 999, item.symbol),
        )
        report = self.build_report(records=persisted, trade_date=trade_date, score_date=score_date)
        self._write_payload(
            {
                "records": [item.model_dump() for item in persisted],
                "latest_report": report.model_dump(),
            }
        )
        return report

    def latest_report(self) -> TradeAttributionReport:
        payload = self._read_payload()
        latest = payload.get("latest_report")
        if latest:
            return TradeAttributionReport.model_validate(latest)
        return self.build_report(records=[])

    def backfill_holding_outcomes(self, holding_outcomes: list[dict]) -> TradeAttributionReport:
        payload = self._read_payload()
        records = [TradeAttributionRecord.model_validate(item) for item in payload.get("records", [])]
        record_map = {
            self._record_key(item.trade_date, item.score_date, item.symbol): item
            for item in records
        }
        self._apply_holding_outcomes(record_map, holding_outcomes)
        persisted = sorted(
            record_map.values(),
            key=lambda item: (item.trade_date, item.score_date, item.rank or 999, item.symbol),
        )
        report = self.build_report(records=persisted)
        self._write_payload(
            {
                "records": [item.model_dump() for item in persisted],
                "latest_report": report.model_dump(),
            }
        )
        return report

    def build_report(
        self,
        *,
        records: list[TradeAttributionRecord | dict] | None = None,
        trade_date: str | None = None,
        score_date: str | None = None,
        symbol: str | None = None,
        reason: str | None = None,
        review_tag: str | None = None,
        exit_context_key: str | None = None,
        exit_context_value: str | None = None,
        use_holding_return: bool = True,
    ) -> TradeAttributionReport:
        if records is None:
            payload = self._read_payload()
            records = payload.get("records", [])
        normalized = [
            item if isinstance(item, TradeAttributionRecord) else TradeAttributionRecord.model_validate(item)
            for item in records
        ]
        filters: dict[str, str] = {}
        if trade_date:
            normalized = [item for item in normalized if item.trade_date == trade_date]
            filters["trade_date"] = trade_date
        if score_date:
            normalized = [item for item in normalized if item.score_date == score_date]
            filters["score_date"] = score_date
        if symbol:
            normalized = [item for item in normalized if item.symbol == symbol]
            filters["symbol"] = symbol
        if reason:
            normalized = [
                item
                for item in normalized
                if item.exit_reason == reason or reason in item.review_tags
            ]
            filters["reason"] = reason
        if review_tag:
            normalized = [item for item in normalized if review_tag in item.review_tags]
            filters["review_tag"] = review_tag
        if exit_context_key:
            normalized = [
                item
                for item in normalized
                if self._matches_exit_context(item.exit_context_snapshot, exit_context_key, exit_context_value)
            ]
            filters["exit_context_key"] = exit_context_key
            if exit_context_value is not None:
                filters["exit_context_value"] = exit_context_value
        normalized.sort(key=lambda item: (item.trade_date, item.score_date, item.rank or 999, item.symbol))

        if not normalized:
            return TradeAttributionReport(
                available=False,
                trade_date=trade_date,
                score_date=score_date,
                generated_at=self._now_factory().isoformat(),
                filters=filters,
            )

        review_input = [
            {
                "trade_id": item.case_id or f"{item.trade_date}-{item.symbol}",
                "symbol": item.symbol,
                "pnl": self._resolve_report_return(item, use_holding_return=use_holding_return),
                "entry_price": 1.0,
                "quantity": 1,
                "holding_days": item.holding_days,
                "exit_reason": item.exit_reason,
                "factors": {"playbook": item.playbook, "regime": item.regime},
            }
            for item in normalized
        ]
        review = self._reviewer.summarize(review_input)
        resolved_returns = [self._resolve_report_return(item, use_holding_return=use_holding_return) for item in normalized]
        avg_return = sum(resolved_returns) / len(resolved_returns)
        wins = sum(1 for item in normalized if self._resolve_report_return(item, use_holding_return=use_holding_return) >= 0.02)
        report_trade_date = trade_date or normalized[-1].trade_date
        report_score_date = score_date or normalized[-1].score_date
        by_symbol = self._group_by_dimension(normalized, "symbol")
        by_reason = self._group_by_reason(normalized)
        by_playbook = self._group_by_dimension(normalized, "playbook")
        by_regime = self._group_by_dimension(normalized, "regime")
        by_exit_reason = self._group_by_dimension(normalized, "exit_reason")
        exit_tag_counts = self._summarize_review_tags(normalized)
        parameter_hints = self._build_parameter_hints(exit_tag_counts)
        summary_lines = [
            f"样本 {len(normalized)} 笔，胜率 {wins / len(normalized):.1%}，平均次日收益 {avg_return:.2%}。",
        ]
        if by_playbook:
            best_playbook = by_playbook[0]
            summary_lines.append(
                f"最佳战法 {best_playbook.key}，样本 {best_playbook.trade_count}，平均收益 {best_playbook.avg_next_day_close_pct:.2%}。"
            )
        if by_regime:
            weakest_regime = min(by_regime, key=lambda item: item.avg_next_day_close_pct)
            summary_lines.append(
                f"最弱状态 {weakest_regime.key}，样本 {weakest_regime.trade_count}，平均收益 {weakest_regime.avg_next_day_close_pct:.2%}。"
            )
        if by_symbol:
            weakest_symbol = min(by_symbol, key=lambda item: item.avg_next_day_close_pct)
            summary_lines.append(
                f"波动标的 {weakest_symbol.key}，样本 {weakest_symbol.trade_count}，平均收益 {weakest_symbol.avg_next_day_close_pct:.2%}。"
            )
        if by_exit_reason:
            summary_lines.append(f"主要退出标签 {by_exit_reason[0].key}，样本 {by_exit_reason[0].trade_count}。")
        if by_reason:
            summary_lines.append(f"主要复盘原因 {by_reason[0].key}，关联样本 {by_reason[0].trade_count}。")
        if exit_tag_counts:
            summary_lines.append(f"主要快撤特征 {exit_tag_counts[0]['tag']}，样本 {exit_tag_counts[0]['count']}。")

        return TradeAttributionReport(
            available=True,
            trade_date=report_trade_date,
            score_date=report_score_date,
            generated_at=self._now_factory().isoformat(),
            filters=filters,
            trade_count=len(normalized),
            avg_next_day_close_pct=round(avg_return, 6),
            win_rate=round(wins / len(normalized), 6),
            review_summary={
                "total_trades": review.total_trades,
                "win_rate": review.win_rate,
                "avg_pnl": review.avg_pnl,
                "best_factors": review.best_factors,
                "worst_factors": review.worst_factors,
                "suggestions": review.suggestions,
                "exit_tag_counts": exit_tag_counts,
            },
            review_tag_summary=exit_tag_counts,
            parameter_hints=parameter_hints,
            by_symbol=by_symbol,
            by_reason=by_reason,
            by_playbook=by_playbook,
            by_regime=by_regime,
            by_exit_reason=by_exit_reason,
            items=normalized[-50:],
            summary_lines=summary_lines,
        )

    def build_playbook_priority_updates(
        self,
        report: TradeAttributionReport | None = None,
    ) -> dict[str, dict[str, Any]]:
        target_report = report or self.latest_report()
        updates: dict[str, dict[str, Any]] = {}
        for item in list(target_report.by_playbook or []):
            recent_pnl = float(item.avg_next_day_close_pct or 0.0)
            priority_score = 0.5
            probation = False
            reason = f"recent_avg_pnl={recent_pnl:.2%} trade_count={item.trade_count}"
            if item.trade_count >= 10 and recent_pnl > 0:
                priority_score = 0.6
            if item.trade_count >= 10 and recent_pnl < -0.02:
                priority_score = 0.3
            if item.trade_count >= 10 and priority_score < 0.1:
                probation = True
            if item.trade_count >= 10 and recent_pnl < -0.04:
                priority_score = 0.05
                probation = True
            updates[item.key] = {
                "priority_score": round(priority_score, 4),
                "probation": probation,
                "recent_pnl": round(recent_pnl, 6),
                "reason": reason,
            }
        return updates

    def _group_by_dimension(self, records: list[TradeAttributionRecord], field_name: str) -> list[AttributionBucketSummary]:
        buckets: dict[str, list[TradeAttributionRecord]] = {}
        for item in records:
            key = str(getattr(item, field_name) or "unlabeled")
            buckets.setdefault(key, []).append(item)
        return self._build_bucket_summaries(buckets)

    def _group_by_reason(self, records: list[TradeAttributionRecord]) -> list[AttributionBucketSummary]:
        buckets: dict[str, list[TradeAttributionRecord]] = {}
        for item in records:
            reasons: list[str] = []
            if item.exit_reason:
                reasons.append(str(item.exit_reason))
            reasons.extend(str(tag) for tag in item.review_tags if tag)
            for key in dict.fromkeys(reasons or ["unlabeled"]):
                buckets.setdefault(key, []).append(item)
        return self._build_bucket_summaries(buckets)

    @staticmethod
    def _build_bucket_summaries(buckets: dict[str, list[TradeAttributionRecord]]) -> list[AttributionBucketSummary]:
        summaries: list[AttributionBucketSummary] = []
        for key, items in buckets.items():
            returns = [TradeAttributionService._resolve_report_return(item, use_holding_return=True) for item in items]
            positive_count = sum(1 for value in returns if value >= 0.02)
            negative_count = sum(1 for value in returns if value <= -0.02)
            flat_count = len(items) - positive_count - negative_count
            score_values = [item.selection_score for item in items if item.selection_score is not None]
            summaries.append(
                AttributionBucketSummary(
                    key=key,
                    trade_count=len(items),
                    positive_count=positive_count,
                    negative_count=negative_count,
                    flat_count=flat_count,
                    win_rate=round(positive_count / len(items), 6),
                    avg_next_day_close_pct=round(sum(returns) / len(returns), 6),
                    avg_selection_score=(
                        round(sum(score_values) / len(score_values), 4)
                        if score_values
                        else None
                    ),
                    sample_symbols=[item.symbol for item in items[:5]],
                )
            )
        return sorted(
            summaries,
            key=lambda item: (item.avg_next_day_close_pct, item.win_rate, item.trade_count),
            reverse=True,
        )

    @staticmethod
    def _summarize_review_tags(records: list[TradeAttributionRecord]) -> list[dict]:
        tag_counts: dict[str, int] = {}
        for item in records:
            for tag in item.review_tags:
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

    @staticmethod
    def _build_parameter_hints(exit_tag_counts: list[dict]) -> list[dict]:
        tag_count_map = {item["tag"]: int(item["count"]) for item in exit_tag_counts}
        hints: list[dict] = []
        trend_weak_count = tag_count_map.get("sector_relative_trend_weak", 0)
        if trend_weak_count > 0:
            hints.extend(
                [
                    {
                        "param_key": "execution_poll_seconds",
                        "direction": "decrease",
                        "sample_count": trend_weak_count,
                        "reason": "连续掉队样本已出现，缩短执行轮询可更早捕捉板块内失位。",
                    },
                    {
                        "param_key": "focus_poll_seconds",
                        "direction": "decrease",
                        "sample_count": trend_weak_count,
                        "reason": "连续掉队样本已出现，缩短重点池轮询可更早识别 leader 失速。",
                    },
                ]
            )
        intraday_fade_count = tag_count_map.get("intraday_fade", 0) + tag_count_map.get("negative_alert", 0)
        if intraday_fade_count > 0:
            hints.append(
                {
                    "param_key": "t_stop_loss_soft",
                    "direction": "increase",
                    "sample_count": intraday_fade_count,
                    "reason": "盘中弱化/预警样本偏多，可考虑收紧软止损，减少弱转强失败后的拖延。",
                }
            )
        sector_retreat_count = tag_count_map.get("sector_retreat", 0)
        if sector_retreat_count > 0:
            hints.extend(
                [
                    {
                        "param_key": "sector_exposure_limit",
                        "direction": "decrease",
                        "sample_count": sector_retreat_count,
                        "reason": "板块退潮退出样本出现，降低单板块暴露可减少联动回撤。",
                    },
                    {
                        "param_key": "sector_theme_rotation_weight",
                        "direction": "decrease",
                        "sample_count": sector_retreat_count,
                        "reason": "板块退潮退出样本出现，可适度下调主题轮动权重，减少尾段追逐。",
                    },
                ]
            )
        return hints

    @staticmethod
    def _matches_exit_context(snapshot: dict, key: str, raw_value: str | None) -> bool:
        if not isinstance(snapshot, dict) or key not in snapshot:
            return False
        if raw_value is None:
            return True
        actual = snapshot.get(key)
        expected_text = str(raw_value).strip().lower()
        if isinstance(actual, bool):
            return actual is (expected_text in {"1", "true", "yes", "y"})
        if isinstance(actual, int) and not isinstance(actual, bool):
            try:
                return actual == int(float(raw_value))
            except ValueError:
                return False
        if isinstance(actual, float):
            try:
                return abs(actual - float(raw_value)) < 1e-9
            except ValueError:
                return False
        return str(actual).lower() == expected_text

    @staticmethod
    def _record_key(trade_date: str, score_date: str, symbol: str) -> str:
        return f"{trade_date}|{score_date}|{symbol}"

    def _read_payload(self) -> dict:
        payload = read_json_with_backup(self._storage_path, default={"records": []})
        return payload if isinstance(payload, dict) else {"records": []}

    def _write_payload(self, payload: dict) -> None:
        atomic_write_json(self._storage_path, payload)

    @staticmethod
    def _resolve_report_return(item: TradeAttributionRecord, *, use_holding_return: bool) -> float:
        if use_holding_return and abs(float(item.holding_return_pct or 0.0)) > 1e-9:
            return float(item.holding_return_pct or 0.0)
        return float(item.next_day_close_pct or 0.0)

    def _apply_holding_outcomes(
        self,
        record_map: dict[str, TradeAttributionRecord],
        holding_outcomes: list[dict],
    ) -> None:
        for raw in holding_outcomes:
            symbol = str(raw.get("symbol") or "").strip()
            if not symbol:
                continue
            matched_key = ""
            if raw.get("trade_date") and raw.get("score_date"):
                candidate_key = self._record_key(str(raw.get("trade_date")), str(raw.get("score_date")), symbol)
                if candidate_key in record_map:
                    matched_key = candidate_key
            if not matched_key:
                matched_candidates = [
                    key
                    for key, record in record_map.items()
                    if record.symbol == symbol
                ]
                if not matched_candidates:
                    continue
                matched_candidates.sort()
                matched_key = matched_candidates[-1]
            record = record_map[matched_key]
            record.holding_return_pct = float(raw.get("holding_return_pct", record.holding_return_pct) or 0.0)
            record.max_drawdown_during_hold = float(
                raw.get("max_drawdown_during_hold", record.max_drawdown_during_hold) or 0.0
            )
            exit_price = raw.get("exit_price")
            if exit_price is not None:
                try:
                    record.exit_price = float(exit_price)
                except (TypeError, ValueError):
                    pass
