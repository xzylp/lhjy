"""实盘与回测偏差追踪。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class LiveBacktestDriftTracker:
    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def record(
        self,
        *,
        trade_date: str,
        live_pnl_pct: float,
        backtest_pnl_pct: float,
        cause_breakdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        drift_pct = float(live_pnl_pct or 0.0) - float(backtest_pnl_pct or 0.0)
        item = {
            "trade_date": trade_date,
            "generated_at": self._now_factory().isoformat(),
            "live_pnl_pct": round(float(live_pnl_pct or 0.0), 6),
            "backtest_pnl_pct": round(float(backtest_pnl_pct or 0.0), 6),
            "drift_pct": round(drift_pct, 6),
            "alert": abs(drift_pct) > 0.01,
            "cause_breakdown": dict(cause_breakdown or {}),
            "summary_lines": [
                f"实盘/回测偏差: live={float(live_pnl_pct or 0.0):+.2%} backtest={float(backtest_pnl_pct or 0.0):+.2%} drift={drift_pct:+.2%}。"
            ],
        }
        payload = self._read_payload()
        reports = list(payload.get("reports") or [])
        reports.append(item)
        payload["reports"] = reports[-120:]
        payload["latest_report"] = item
        atomic_write_json(self._storage_path, payload)
        return item

    def record_from_replay(
        self,
        *,
        trade_date: str,
        live_pnl_pct: float,
        evaluation_records: list[dict[str, Any]],
        price_data: dict[str, Any],
        execution_quality_report: dict[str, Any] | None = None,
        report_trade_date: str | None = None,
        report_score_date: str | None = None,
        config_overrides: dict[str, Any] | None = None,
        fallback_backtest_pnl_pct: float = 0.0,
        replay_window_days: int = 5,
    ) -> dict[str, Any]:
        replay_report = self._build_minimal_replay(
            trade_date=trade_date,
            evaluation_records=evaluation_records,
            price_data=price_data,
            report_trade_date=report_trade_date,
            report_score_date=report_score_date,
            config_overrides=config_overrides,
            replay_window_days=replay_window_days,
        )
        replay_mode = str(replay_report.get("mode") or "")
        if replay_mode == "proxy_fallback":
            replay_report.pop("backtest_pnl_pct", None)
            backtest_pnl_pct = float(fallback_backtest_pnl_pct or 0.0)
        else:
            backtest_pnl_pct = float(replay_report.pop("backtest_pnl_pct", 0.0) or 0.0)
        cause_breakdown = {
            **replay_report,
            "avg_slippage_bps": float((execution_quality_report or {}).get("avg_slippage_bps", 0.0) or 0.0),
            "avg_latency_ms": float((execution_quality_report or {}).get("avg_latency_ms", 0.0) or 0.0),
        }
        return self.record(
            trade_date=trade_date,
            live_pnl_pct=live_pnl_pct,
            backtest_pnl_pct=backtest_pnl_pct,
            cause_breakdown=cause_breakdown,
        )

    def latest(self) -> dict[str, Any]:
        payload = self._read_payload()
        latest = payload.get("latest_report")
        return dict(latest) if isinstance(latest, dict) else {}

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"reports": []})
        return payload if isinstance(payload, dict) else {"reports": []}

    def _build_minimal_replay(
        self,
        *,
        trade_date: str,
        evaluation_records: list[dict[str, Any]],
        price_data: dict[str, Any],
        report_trade_date: str | None,
        report_score_date: str | None,
        config_overrides: dict[str, Any] | None,
        replay_window_days: int,
    ) -> dict[str, Any]:
        import pandas as pd

        from .engine import BacktestConfig, BacktestEngine

        replay_returns: list[float] = []
        replayed_trace_ids: list[str] = []
        skipped_records: list[dict[str, Any]] = []
        replayed_trade_count = 0
        replayed_symbol_count = 0
        normalized_price = self._normalize_price_data(price_data)
        for record in evaluation_records:
            if not self._is_record_in_scope(
                record,
                trade_date=trade_date,
                report_trade_date=report_trade_date,
                report_score_date=report_score_date,
                replay_window_days=replay_window_days,
            ):
                continue
            trace_id = str(record.get("trace_id") or "")
            signal_date = self._resolve_signal_date(record)
            if not signal_date:
                skipped_records.append({"trace_id": trace_id, "reason": "missing_signal_date"})
                continue
            target_symbols = self._resolve_target_symbols(record)
            if not target_symbols:
                skipped_records.append({"trace_id": trace_id, "reason": "missing_target_symbols"})
                continue
            eligible_symbols: list[str] = []
            subset_price: dict[str, Any] = {}
            signal_index: set[str] = {signal_date}
            for symbol in target_symbols:
                frame = normalized_price.get(symbol)
                if frame is None or signal_date not in frame.index:
                    continue
                index = frame.index.get_loc(signal_date)
                if isinstance(index, slice):
                    index = index.start
                if int(index) + 1 >= len(frame.index):
                    continue
                eligible_symbols.append(symbol)
                subset_price[symbol] = frame
                signal_index.add(str(frame.index[int(index) + 1]))
            if not eligible_symbols:
                skipped_records.append({"trace_id": trace_id, "reason": "missing_forward_bar"})
                continue
            signals = pd.DataFrame(index=sorted(signal_index), columns=eligible_symbols)
            for symbol in eligible_symbols:
                signals.loc[signal_date, symbol] = "BUY"
            engine = BacktestEngine(BacktestConfig(**(config_overrides or {})))
            result = engine.run(signals, subset_price)
            replay_returns.append(float(result.metrics.total_return or 0.0))
            replayed_trace_ids.append(trace_id)
            replayed_trade_count += len(result.trades)
            replayed_symbol_count += len(eligible_symbols)
        if replay_returns:
            return {
                "mode": "minimal_signal_replay",
                "backtest_pnl_pct": round(sum(replay_returns) / len(replay_returns), 6),
                "replay_sample_count": len(replay_returns),
                "replayed_trade_count": replayed_trade_count,
                "replayed_symbol_count": replayed_symbol_count,
                "replayed_trace_ids": replayed_trace_ids[:20],
                "skipped_record_count": len(skipped_records),
                "skipped_records": skipped_records[:20],
                "report_trade_date": str(report_trade_date or ""),
                "report_score_date": str(report_score_date or ""),
                "replay_window_days": int(max(replay_window_days, 1)),
            }
        return {
            "mode": "proxy_fallback",
            "backtest_pnl_pct": 0.0,
            "replay_sample_count": 0,
            "replayed_trade_count": 0,
            "replayed_symbol_count": 0,
            "replayed_trace_ids": [],
            "skipped_record_count": len(skipped_records),
            "skipped_records": skipped_records[:20],
            "report_trade_date": str(report_trade_date or ""),
            "report_score_date": str(report_score_date or ""),
            "replay_window_days": int(max(replay_window_days, 1)),
            "fallback_reason": "no_replayable_records",
        }

    @staticmethod
    def _normalize_price_data(price_data: dict[str, Any]) -> dict[str, Any]:
        import pandas as pd

        normalized: dict[str, Any] = {}
        for symbol, frame in dict(price_data or {}).items():
            if frame is None:
                continue
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            copied = frame.copy()
            copied.index = pd.to_datetime(copied.index, errors="coerce").strftime("%Y-%m-%d")
            copied = copied[~copied.index.duplicated(keep="last")]
            normalized[str(symbol)] = copied.sort_index()
        return normalized

    @staticmethod
    def _resolve_signal_date(record: dict[str, Any]) -> str:
        adoption = dict(record.get("adoption") or {})
        generated_at = str(record.get("generated_at") or "").strip()
        adoption_trade_date = str(adoption.get("trade_date") or "").strip()
        return (generated_at[:10] if generated_at else adoption_trade_date[:10]) or adoption_trade_date[:10]

    @staticmethod
    def _resolve_target_symbols(record: dict[str, Any]) -> list[str]:
        adoption = dict(record.get("adoption") or {})
        values = []
        values.extend(list(record.get("selected_symbols") or []))
        values.extend(list(adoption.get("adopted_symbols") or []))
        values.extend(list(adoption.get("watchlist_symbols") or []))
        seen: set[str] = set()
        resolved: list[str] = []
        for item in values:
            symbol = str(item or "").strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            resolved.append(symbol)
        return resolved[:5]

    def _is_record_in_scope(
        self,
        record: dict[str, Any],
        *,
        trade_date: str,
        report_trade_date: str | None,
        report_score_date: str | None,
        replay_window_days: int,
    ) -> bool:
        reference_dates = {
            str(trade_date or "").strip(),
            str(report_trade_date or "").strip(),
            str(report_score_date or "").strip(),
        }
        reference_dates.discard("")
        signal_date = self._resolve_signal_date(record)
        adoption_trade_date = str((record.get("adoption") or {}).get("trade_date") or "").strip()[:10]
        candidate_dates = {signal_date, adoption_trade_date}
        candidate_dates.discard("")
        if reference_dates and candidate_dates.intersection(reference_dates):
            return True
        if not signal_date:
            return False
        try:
            current = datetime.fromisoformat(str(trade_date)[:10])
            signal = datetime.fromisoformat(signal_date[:10])
        except ValueError:
            return False
        day_delta = (current.date() - signal.date()).days
        return 0 <= day_delta <= max(int(replay_window_days or 5), 1)
