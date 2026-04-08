"""离线回测 attribution 聚合。

注意：
- 本模块只用于离线回测样本聚合与统计，不是线上事实归因。
- 请勿与 ``ashare_system.learning.attribution`` 的成交后学习闭环语义混用。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field


OFFLINE_BACKTEST_ATTRIBUTION_NOTE = (
    "这是离线回测 attribution，用于样本统计与策略复盘，不代表线上真实成交后的事实归因。"
)
SUPPORTED_COMPARE_DIMENSIONS = {"playbook", "regime", "exit_reason"}
SUPPORTED_BUCKET_SORT_FIELDS = {"avg_return_pct", "total_return_pct", "trade_count", "win_rate", "key"}


class BacktestTradeRecord(BaseModel):
    """离线回测使用的最小成交样本。"""

    trade_id: str | None = None
    symbol: str
    playbook: str = "unassigned"
    regime: str = "unknown"
    exit_reason: str = "unlabeled"
    return_pct: float = 0.0
    holding_days: int = 0
    trade_date: str = ""
    note: str = ""


class BacktestBucketSummary(BaseModel):
    """离线回测单一维度聚合桶。"""

    key: str
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    flat_count: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_return_pct: float = 0.0
    sample_symbols: list[str] = Field(default_factory=list)


class BacktestWeakBucket(BaseModel):
    """离线回测弱点分桶。"""

    dimension: str
    key: str
    trade_count: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_return_pct: float = 0.0
    sample_symbols: list[str] = Field(default_factory=list)


class BacktestCompareView(BaseModel):
    """离线回测最小对比视图。"""

    dimension: str
    bucket_count: int = 0
    compared_keys: list[str] = Field(default_factory=list)
    best_bucket: dict = Field(default_factory=dict)
    weakest_bucket: dict = Field(default_factory=dict)
    spread_return_pct: float = 0.0
    buckets: list[dict] = Field(default_factory=list)


class BacktestAttributionReport(BaseModel):
    """离线回测 attribution 报告。"""

    available: bool = False
    attribution_scope: str = "offline_backtest"
    semantics_note: str = OFFLINE_BACKTEST_ATTRIBUTION_NOTE
    generated_at: str
    filters: dict = Field(default_factory=dict)
    trade_count: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_return_pct: float = 0.0
    overview: dict = Field(default_factory=dict)
    by_playbook: list[BacktestBucketSummary] = Field(default_factory=list)
    by_regime: list[BacktestBucketSummary] = Field(default_factory=list)
    by_exit_reason: list[BacktestBucketSummary] = Field(default_factory=list)
    weakest_buckets: list[BacktestWeakBucket] = Field(default_factory=list)
    compare_views: dict[str, BacktestCompareView] = Field(default_factory=dict)
    selected_weakest_bucket: dict = Field(default_factory=dict)
    selected_compare_view: dict = Field(default_factory=dict)
    self_improvement_inputs: dict = Field(default_factory=dict)
    export_payload: dict = Field(default_factory=dict)
    items: list[BacktestTradeRecord] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


class OfflineBacktestAttributionService:
    """构建离线回测 attribution 报告。"""

    def __init__(self, now_factory: Callable[[], datetime] | None = None) -> None:
        self._now_factory = now_factory or datetime.now

    def build_report(
        self,
        trades: list[BacktestTradeRecord | dict],
        *,
        playbook: str | None = None,
        regime: str | None = None,
        exit_reason: str | None = None,
        weakest_bucket_dimension: str | None = None,
        compare_view_dimension: str | None = None,
        weakest_bucket_sort_by: str = "avg_return_pct",
        weakest_bucket_sort_order: str = "asc",
        compare_bucket_sort_by: str = "trade_count",
        compare_bucket_sort_order: str = "desc",
    ) -> BacktestAttributionReport:
        normalized = [
            item if isinstance(item, BacktestTradeRecord) else BacktestTradeRecord.model_validate(item)
            for item in trades
        ]
        filters: dict[str, str] = {}
        if playbook:
            normalized = [item for item in normalized if item.playbook == playbook]
            filters["playbook"] = playbook
        if regime:
            normalized = [item for item in normalized if item.regime == regime]
            filters["regime"] = regime
        if exit_reason:
            normalized = [item for item in normalized if item.exit_reason == exit_reason]
            filters["exit_reason"] = exit_reason
        weakest_bucket_dimension = self._normalize_dimension(weakest_bucket_dimension)
        compare_view_dimension = self._normalize_dimension(compare_view_dimension)
        weakest_bucket_sort_by = self._normalize_sort_field(weakest_bucket_sort_by)
        compare_bucket_sort_by = self._normalize_sort_field(compare_bucket_sort_by)
        weakest_bucket_sort_order = self._normalize_sort_order(weakest_bucket_sort_order)
        compare_bucket_sort_order = self._normalize_sort_order(compare_bucket_sort_order)
        if weakest_bucket_dimension:
            filters["weakest_bucket_dimension"] = weakest_bucket_dimension
        if compare_view_dimension:
            filters["compare_view_dimension"] = compare_view_dimension
        filters["weakest_bucket_sort_by"] = weakest_bucket_sort_by
        filters["weakest_bucket_sort_order"] = weakest_bucket_sort_order
        filters["compare_bucket_sort_by"] = compare_bucket_sort_by
        filters["compare_bucket_sort_order"] = compare_bucket_sort_order

        normalized.sort(key=lambda item: (item.trade_date, item.symbol, item.playbook, item.exit_reason))
        generated_at = self._now_factory().isoformat()
        if not normalized:
            return BacktestAttributionReport(
                available=False,
                generated_at=generated_at,
                filters=filters,
            )

        trade_count = len(normalized)
        total_return_pct = round(sum(item.return_pct for item in normalized), 6)
        win_count = sum(1 for item in normalized if item.return_pct > 0)
        avg_return_pct = round(total_return_pct / trade_count, 6)
        win_rate = round(win_count / trade_count, 6)
        by_playbook = self._group_by_dimension(normalized, "playbook")
        by_regime = self._group_by_dimension(normalized, "regime")
        by_exit_reason = self._group_by_dimension(normalized, "exit_reason")
        weakest_buckets = self._build_weakest_buckets(
            by_playbook=by_playbook,
            by_regime=by_regime,
            by_exit_reason=by_exit_reason,
            sort_by=weakest_bucket_sort_by,
            sort_order=weakest_bucket_sort_order,
        )
        compare_views = {
            "playbook": self._build_compare_view(
                "playbook",
                by_playbook,
                sort_by=compare_bucket_sort_by,
                sort_order=compare_bucket_sort_order,
            ),
            "regime": self._build_compare_view(
                "regime",
                by_regime,
                sort_by=compare_bucket_sort_by,
                sort_order=compare_bucket_sort_order,
            ),
            "exit_reason": self._build_compare_view(
                "exit_reason",
                by_exit_reason,
                sort_by=compare_bucket_sort_by,
                sort_order=compare_bucket_sort_order,
            ),
        }
        selected_weakest_bucket = self._select_weakest_bucket(
            weakest_buckets,
            dimension=weakest_bucket_dimension,
        )
        selected_compare_view = self._select_compare_view(
            compare_views,
            dimension=compare_view_dimension,
        )
        self_improvement_inputs = self._build_self_improvement_inputs(
            weakest_buckets=weakest_buckets,
            selected_weakest_bucket=selected_weakest_bucket,
            compare_views=compare_views,
            selected_compare_view=selected_compare_view,
        )
        summary_lines = [
            f"离线回测样本 {trade_count} 笔，胜率 {win_rate:.1%}，平均收益 {avg_return_pct:.2%}。",
        ]
        if by_playbook:
            summary_lines.append(
                f"战法维度样本最多的是 {by_playbook[0].key}，共 {by_playbook[0].trade_count} 笔。"
            )
        if by_regime:
            summary_lines.append(
                f"市场状态中表现最弱的是 {min(by_regime, key=lambda item: item.avg_return_pct).key}。"
            )
        if by_exit_reason:
            summary_lines.append(
                f"退出原因中样本最多的是 {by_exit_reason[0].key}，共 {by_exit_reason[0].trade_count} 笔。"
            )
        if weakest_buckets:
            weakest = weakest_buckets[0]
            summary_lines.append(
                f"当前最弱分桶是 {weakest.dimension}:{weakest.key}，平均收益 {weakest.avg_return_pct:.2%}。"
            )
        if selected_weakest_bucket:
            summary_lines.append(
                f"已选最弱分桶 {selected_weakest_bucket['dimension']}:{selected_weakest_bucket['key']}。"
            )
        if selected_compare_view:
            summary_lines.append(
                f"已选对比视图 {selected_compare_view['dimension']}，分桶数 {selected_compare_view['bucket_count']}。"
            )
        overview = {
            "attribution_scope": "offline_backtest",
            "semantics_note": OFFLINE_BACKTEST_ATTRIBUTION_NOTE,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "avg_return_pct": avg_return_pct,
            "total_return_pct": total_return_pct,
            "filters": filters,
            "weakest_bucket_count": len(weakest_buckets),
            "compare_view_dimensions": list(compare_views.keys()),
            "selected_weakest_bucket": selected_weakest_bucket,
            "selected_compare_view": selected_compare_view,
            "self_improvement_inputs": self_improvement_inputs,
        }
        export_payload = {
            "overview": overview,
            "summary_lines": summary_lines,
            "by_playbook": [item.model_dump() for item in by_playbook],
            "by_regime": [item.model_dump() for item in by_regime],
            "by_exit_reason": [item.model_dump() for item in by_exit_reason],
            "weakest_buckets": [item.model_dump() for item in weakest_buckets],
            "selected_weakest_bucket": selected_weakest_bucket,
            "compare_views": {
                key: value.model_dump()
                for key, value in compare_views.items()
            },
            "selected_compare_view": selected_compare_view,
            "self_improvement_inputs": self_improvement_inputs,
            "items": [item.model_dump() for item in normalized],
        }

        return BacktestAttributionReport(
            available=True,
            generated_at=generated_at,
            filters=filters,
            trade_count=trade_count,
            win_rate=win_rate,
            avg_return_pct=avg_return_pct,
            total_return_pct=total_return_pct,
            overview=overview,
            by_playbook=by_playbook,
            by_regime=by_regime,
            by_exit_reason=by_exit_reason,
            weakest_buckets=weakest_buckets,
            compare_views=compare_views,
            selected_weakest_bucket=selected_weakest_bucket,
            selected_compare_view=selected_compare_view,
            self_improvement_inputs=self_improvement_inputs,
            export_payload=export_payload,
            items=normalized,
            summary_lines=summary_lines,
        )

    @staticmethod
    def _build_self_improvement_inputs(
        *,
        weakest_buckets: list[BacktestWeakBucket],
        selected_weakest_bucket: dict,
        compare_views: dict[str, BacktestCompareView],
        selected_compare_view: dict,
    ) -> dict:
        weakest_bucket = selected_weakest_bucket or (weakest_buckets[0].model_dump() if weakest_buckets else {})
        spreads = [
            {
                "dimension": view.dimension,
                "spread_return_pct": view.spread_return_pct,
                "bucket_count": view.bucket_count,
            }
            for view in compare_views.values()
        ]
        spreads.sort(key=lambda item: item["spread_return_pct"], reverse=True)
        return {
            "proposal_scope": "offline_self_improvement_inputs",
            "attribution_scope": "offline_backtest",
            "live_promotion_allowed": False,
            "weakest_bucket": weakest_bucket,
            "weakest_buckets": [item.model_dump() for item in weakest_buckets],
            "selected_compare_view": selected_compare_view,
            "dimension_spreads": spreads,
        }

    def _group_by_dimension(
        self,
        trades: list[BacktestTradeRecord],
        field_name: str,
    ) -> list[BacktestBucketSummary]:
        buckets: dict[str, list[BacktestTradeRecord]] = {}
        for item in trades:
            key = str(getattr(item, field_name) or "unknown")
            buckets.setdefault(key, []).append(item)
        summaries = [
            self._build_bucket_summary(key, items)
            for key, items in buckets.items()
        ]
        summaries.sort(
            key=lambda item: (-item.trade_count, -item.avg_return_pct, item.key),
        )
        return summaries

    @staticmethod
    def _build_bucket_summary(
        key: str,
        trades: list[BacktestTradeRecord],
    ) -> BacktestBucketSummary:
        trade_count = len(trades)
        win_count = sum(1 for item in trades if item.return_pct > 0)
        loss_count = sum(1 for item in trades if item.return_pct < 0)
        flat_count = trade_count - win_count - loss_count
        total_return_pct = round(sum(item.return_pct for item in trades), 6)
        avg_return_pct = round(total_return_pct / trade_count, 6) if trade_count else 0.0
        win_rate = round(win_count / trade_count, 6) if trade_count else 0.0
        sample_symbols = list(dict.fromkeys(item.symbol for item in trades))[:5]
        return BacktestBucketSummary(
            key=key,
            trade_count=trade_count,
            win_count=win_count,
            loss_count=loss_count,
            flat_count=flat_count,
            win_rate=win_rate,
            avg_return_pct=avg_return_pct,
            total_return_pct=total_return_pct,
            sample_symbols=sample_symbols,
        )

    def _build_weakest_buckets(
        self,
        *,
        by_playbook: list[BacktestBucketSummary],
        by_regime: list[BacktestBucketSummary],
        by_exit_reason: list[BacktestBucketSummary],
        sort_by: str,
        sort_order: str,
    ) -> list[BacktestWeakBucket]:
        candidates: list[BacktestWeakBucket] = []
        for dimension, buckets in (
            ("playbook", by_playbook),
            ("regime", by_regime),
            ("exit_reason", by_exit_reason),
        ):
            if not buckets:
                continue
            weakest = min(
                buckets,
                key=lambda item: (item.avg_return_pct, item.total_return_pct, -item.trade_count, item.key),
            )
            candidates.append(
                BacktestWeakBucket(
                    dimension=dimension,
                    key=weakest.key,
                    trade_count=weakest.trade_count,
                    win_rate=weakest.win_rate,
                    avg_return_pct=weakest.avg_return_pct,
                    total_return_pct=weakest.total_return_pct,
                    sample_symbols=weakest.sample_symbols,
                )
            )
        return self._sort_weak_buckets(candidates, sort_by=sort_by, sort_order=sort_order)

    def _build_compare_view(
        self,
        dimension: str,
        buckets: list[BacktestBucketSummary],
        *,
        sort_by: str,
        sort_order: str,
    ) -> BacktestCompareView:
        if not buckets:
            return BacktestCompareView(dimension=dimension)
        sorted_buckets = self._sort_bucket_summaries(buckets, sort_by=sort_by, sort_order=sort_order)
        best_bucket = max(
            buckets,
            key=lambda item: (item.avg_return_pct, item.total_return_pct, item.trade_count, item.key),
        )
        weakest_bucket = min(
            buckets,
            key=lambda item: (item.avg_return_pct, item.total_return_pct, -item.trade_count, item.key),
        )
        spread_return_pct = round(best_bucket.avg_return_pct - weakest_bucket.avg_return_pct, 6)
        return BacktestCompareView(
            dimension=dimension,
            bucket_count=len(sorted_buckets),
            compared_keys=[item.key for item in sorted_buckets],
            best_bucket=best_bucket.model_dump(),
            weakest_bucket=weakest_bucket.model_dump(),
            spread_return_pct=spread_return_pct,
            buckets=[item.model_dump() for item in sorted_buckets],
        )

    @staticmethod
    def _normalize_dimension(value: str | None) -> str | None:
        if value in SUPPORTED_COMPARE_DIMENSIONS:
            return value
        return None

    @staticmethod
    def _normalize_sort_field(value: str | None) -> str:
        if value in SUPPORTED_BUCKET_SORT_FIELDS:
            return str(value)
        return "avg_return_pct"

    @staticmethod
    def _normalize_sort_order(value: str | None) -> str:
        if str(value).lower() == "desc":
            return "desc"
        return "asc"

    def _sort_weak_buckets(
        self,
        buckets: list[BacktestWeakBucket],
        *,
        sort_by: str,
        sort_order: str,
    ) -> list[BacktestWeakBucket]:
        reverse = sort_order == "desc"
        return sorted(
            buckets,
            key=lambda item: self._bucket_sort_key(item, sort_by),
            reverse=reverse,
        )

    def _sort_bucket_summaries(
        self,
        buckets: list[BacktestBucketSummary],
        *,
        sort_by: str,
        sort_order: str,
    ) -> list[BacktestBucketSummary]:
        reverse = sort_order == "desc"
        return sorted(
            buckets,
            key=lambda item: self._bucket_sort_key(item, sort_by),
            reverse=reverse,
        )

    @staticmethod
    def _bucket_sort_key(item: BacktestBucketSummary | BacktestWeakBucket, sort_by: str) -> tuple:
        if sort_by == "key":
            return (str(item.key),)
        return (
            float(getattr(item, sort_by, 0.0) or 0.0),
            float(getattr(item, "avg_return_pct", 0.0) or 0.0),
            float(getattr(item, "total_return_pct", 0.0) or 0.0),
            int(getattr(item, "trade_count", 0) or 0),
            str(item.key),
        )

    @staticmethod
    def _select_weakest_bucket(
        weakest_buckets: list[BacktestWeakBucket],
        *,
        dimension: str | None,
    ) -> dict:
        if dimension:
            for item in weakest_buckets:
                if item.dimension == dimension:
                    return item.model_dump()
            return {}
        return weakest_buckets[0].model_dump() if weakest_buckets else {}

    def _select_compare_view(
        self,
        compare_views: dict[str, BacktestCompareView],
        *,
        dimension: str | None,
    ) -> dict:
        if dimension:
            selected = compare_views.get(dimension)
            return selected.model_dump() if selected is not None else {}
        for key in ("playbook", "regime", "exit_reason"):
            selected = compare_views.get(key)
            if selected is not None and selected.bucket_count > 0:
                return selected.model_dump()
        return {}
