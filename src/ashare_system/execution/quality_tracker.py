"""执行质量跟踪与 Implementation Shortfall 归因。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class ExecutionQualityTracker:
    """跟踪信号价、提交价、成交价之间的偏差。"""

    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def record_submission(
        self,
        *,
        intent_id: str,
        symbol: str,
        side: str,
        signal_price: float,
        signal_time: str,
        submit_price: float,
        submit_time: str,
        trace_id: str | None = None,
        order_id: str | None = None,
        bid_price: float | None = None,
        ask_price: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._read_payload()
        items = payload.setdefault("items", [])
        item = self._find_or_create(items, intent_id=intent_id, order_id=order_id)
        item.update(
            {
                "intent_id": intent_id,
                "order_id": order_id or item.get("order_id"),
                "trace_id": trace_id or item.get("trace_id"),
                "symbol": symbol,
                "side": str(side or "").upper(),
                "trade_date": str(signal_time or submit_time or self._now_factory().date().isoformat())[:10],
                "signal_price": float(signal_price or 0.0),
                "signal_time": str(signal_time or ""),
                "submit_price": float(submit_price or 0.0),
                "submit_time": str(submit_time or ""),
                "bid_price": float(bid_price or 0.0) if bid_price is not None else item.get("bid_price"),
                "ask_price": float(ask_price or 0.0) if ask_price is not None else item.get("ask_price"),
                "metadata": {**dict(item.get("metadata") or {}), **dict(metadata or {})},
            }
        )
        self._refresh_cost_breakdown(item)
        self._write_payload(payload)
        return dict(item)

    def record_fill(
        self,
        *,
        intent_id: str | None = None,
        order_id: str | None = None,
        fill_price: float | None = None,
        fill_time: str | None = None,
        filled_quantity: int | None = None,
        filled_value: float | None = None,
        commission_cost: float | None = None,
        status: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._read_payload()
        items = payload.setdefault("items", [])
        item = self._find_or_create(items, intent_id=intent_id, order_id=order_id)
        if intent_id:
            item["intent_id"] = intent_id
        if order_id:
            item["order_id"] = order_id
        if trace_id:
            item["trace_id"] = trace_id
        if fill_price is not None:
            item["fill_price"] = float(fill_price or 0.0)
        if fill_time:
            item["fill_time"] = str(fill_time)
            item["trade_date"] = str(fill_time)[:10]
        if filled_quantity is not None:
            item["filled_quantity"] = int(filled_quantity or 0)
        if filled_value is not None:
            item["filled_value"] = float(filled_value or 0.0)
        if commission_cost is not None:
            item["commission_cost"] = float(commission_cost or 0.0)
        if status is not None:
            item["status"] = str(status or "")
        if metadata:
            item["metadata"] = {**dict(item.get("metadata") or {}), **dict(metadata or {})}
        self._refresh_cost_breakdown(item)
        report = self.summarize_day(str(item.get("trade_date") or ""))
        payload["latest_report"] = report
        self._write_payload(payload)
        return dict(item)

    def summarize_day(self, trade_date: str, *, persist: bool = False) -> dict[str, Any]:
        payload = self._read_payload()
        items = [
            dict(item)
            for item in list(payload.get("items") or [])
            if str(item.get("trade_date") or "") == str(trade_date or "")
        ]
        completed = [item for item in items if item.get("fill_price") is not None]
        slippages = [float(item.get("slippage_bps", 0.0) or 0.0) for item in completed]
        latencies = [float(item.get("latency_ms", 0.0) or 0.0) for item in completed if item.get("latency_ms") is not None]
        implementation_shortfall = [float(item.get("implementation_shortfall_bps", 0.0) or 0.0) for item in completed]
        report = {
            "trade_date": trade_date,
            "generated_at": self._now_factory().isoformat(),
            "available": bool(items),
            "record_count": len(items),
            "completed_count": len(completed),
            "avg_slippage_bps": round(sum(slippages) / len(slippages), 4) if slippages else 0.0,
            "p90_slippage_bps": round(self._percentile(slippages, 0.9), 4) if slippages else 0.0,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "avg_implementation_shortfall_bps": round(sum(implementation_shortfall) / len(implementation_shortfall), 4) if implementation_shortfall else 0.0,
            "items": completed[-100:],
            "summary_lines": [
                f"执行质量日报: records={len(items)} completed={len(completed)} avg_slippage={round(sum(slippages) / len(slippages), 2) if slippages else 0.0}bps avg_latency={round(sum(latencies) / len(latencies), 2) if latencies else 0.0}ms。"
            ],
        }
        if persist:
            payload["latest_report"] = report
            reports = payload.setdefault("reports", {})
            reports[trade_date] = report
            self._write_payload(payload)
        return report

    def latest_report(self) -> dict[str, Any]:
        payload = self._read_payload()
        report = payload.get("latest_report")
        return dict(report) if isinstance(report, dict) else {}

    def recent_avg_slippage_bps(self, lookback_days: int = 20) -> float | None:
        payload = self._read_payload()
        reports = dict(payload.get("reports") or {})
        today = self._now_factory().date()
        samples: list[float] = []
        for offset in range(max(int(lookback_days or 20), 1)):
            trade_date = (today - timedelta(days=offset)).isoformat()
            report = dict(reports.get(trade_date) or {})
            if not report:
                continue
            samples.append(float(report.get("avg_slippage_bps", 0.0) or 0.0))
        if not samples:
            return None
        return sum(samples) / len(samples)

    def _refresh_cost_breakdown(self, item: dict[str, Any]) -> None:
        side = str(item.get("side") or "BUY").upper()
        signal_price = float(item.get("signal_price", 0.0) or 0.0)
        submit_price = float(item.get("submit_price", 0.0) or 0.0)
        fill_price = item.get("fill_price")
        fill_price_value = float(fill_price or 0.0) if fill_price is not None else None
        signal_time = self._parse_time(str(item.get("signal_time") or ""))
        fill_time = self._parse_time(str(item.get("fill_time") or ""))
        submit_time = self._parse_time(str(item.get("submit_time") or ""))
        sign = 1.0 if side == "BUY" else -1.0
        if fill_price_value is not None and signal_price > 0:
            item["slippage_bps"] = ((fill_price_value - signal_price) / signal_price) * 10000.0 * sign
            item["implementation_shortfall_bps"] = item["slippage_bps"]
        if fill_time and signal_time:
            item["latency_ms"] = max((fill_time - signal_time).total_seconds() * 1000.0, 0.0)
        elif submit_time and signal_time:
            item["latency_ms"] = max((submit_time - signal_time).total_seconds() * 1000.0, 0.0)
        if signal_price > 0 and submit_price > 0:
            item["timing_cost_bps"] = ((submit_price - signal_price) / signal_price) * 10000.0 * sign
        if fill_price_value is not None and submit_price > 0:
            item["market_impact_bps"] = ((fill_price_value - submit_price) / submit_price) * 10000.0 * sign
        bid_price = float(item.get("bid_price", 0.0) or 0.0)
        ask_price = float(item.get("ask_price", 0.0) or 0.0)
        if bid_price > 0 and ask_price > 0:
            spread_mid = (bid_price + ask_price) / 2.0
            item["spread_cost_bps"] = ((ask_price - bid_price) / max(spread_mid, 1e-9)) * 10000.0 / 2.0
        filled_value = float(item.get("filled_value", 0.0) or 0.0)
        commission_cost = float(item.get("commission_cost", 0.0) or 0.0)
        if filled_value > 0 and commission_cost > 0:
            item["commission_cost_bps"] = commission_cost / filled_value * 10000.0

    @staticmethod
    def _percentile(values: list[float], ratio: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(max(int(round((len(ordered) - 1) * ratio)), 0), len(ordered) - 1)
        return float(ordered[index])

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _find_or_create(items: list[dict[str, Any]], *, intent_id: str | None, order_id: str | None) -> dict[str, Any]:
        for item in items:
            if intent_id and str(item.get("intent_id") or "") == str(intent_id):
                return item
            if order_id and str(item.get("order_id") or "") == str(order_id):
                return item
        created = {"intent_id": intent_id or "", "order_id": order_id or "", "status": "pending"}
        items.append(created)
        return created

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"items": [], "reports": {}})
        return payload if isinstance(payload, dict) else {"items": [], "reports": {}}

    def _write_payload(self, payload: dict[str, Any]) -> None:
        reports = dict(payload.get("reports") or {})
        latest_report = dict(payload.get("latest_report") or {})
        if latest_report.get("trade_date"):
            reports[str(latest_report["trade_date"])] = latest_report
        payload["reports"] = reports
        atomic_write_json(self._storage_path, payload)
