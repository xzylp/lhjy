"""失败日志与模式复发检测。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class FailureJournalService:
    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def record_failures(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._read_payload()
        items = [dict(item) for item in list(payload.get("items") or [])]
        item_map = {str(item.get("sample_id") or ""): item for item in items if str(item.get("sample_id") or "").strip()}
        for record in list(records or []):
            normalized = self._normalize_failure_record(record)
            if not normalized:
                continue
            item_map[normalized["sample_id"]] = normalized
        persisted_items = sorted(
            item_map.values(),
            key=lambda item: (
                str(item.get("trade_date") or ""),
                str(item.get("symbol") or ""),
                str(item.get("playbook") or ""),
            ),
        )[-500:]
        payload["items"] = persisted_items
        payload["monthly_summary"] = self._build_monthly_summary_map(persisted_items)
        payload["updated_at"] = self._now_factory().isoformat()
        atomic_write_json(self._storage_path, payload)
        return {
            "available": bool(persisted_items),
            "count": len(persisted_items),
            "monthly_summary": dict(payload.get("monthly_summary") or {}),
        }

    def build_pattern_warning(
        self,
        *,
        playbooks: list[str],
        regime_label: str,
        sectors: list[str] | None = None,
        review_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        resolved_playbooks = {str(item).strip() for item in list(playbooks or []) if str(item).strip()}
        resolved_sectors = {str(item).strip() for item in list(sectors or []) if str(item).strip()}
        if not resolved_sectors:
            resolved_sectors = {"unknown"}
        input_tags = {str(item).strip().lower() for item in list(review_tags or []) if str(item).strip()}
        items = [dict(item) for item in list(self._read_payload().get("items") or [])]

        matched: list[dict[str, Any]] = []
        for item in items:
            if str(item.get("regime_label") or "") != str(regime_label):
                continue
            if str(item.get("playbook") or "") not in resolved_playbooks:
                continue
            similarity_score = 0.55
            if str(item.get("sector") or "") in resolved_sectors:
                similarity_score += 0.2
            elif "*" not in resolved_sectors:
                similarity_score -= 0.1
            stored_tags = {
                str(tag).strip().lower()
                for tag in list(item.get("review_tags") or [])
                if str(tag).strip()
            }
            overlap_count = len(stored_tags & input_tags)
            if overlap_count > 0:
                similarity_score += min(overlap_count * 0.08, 0.16)
            if float(item.get("loss_return_pct", 0.0) or 0.0) <= -0.05:
                similarity_score += 0.05
            similarity_score = max(min(similarity_score, 0.99), 0.0)
            similarity_level = self._similarity_level(similarity_score)
            if similarity_level == "low":
                continue
            matched.append(
                {
                    **self._export_item(item),
                    "similarity_score": round(similarity_score, 4),
                    "similarity_level": similarity_level,
                    "tag_overlap_count": overlap_count,
                }
            )

        matched.sort(
            key=lambda item: (
                float(item.get("similarity_score", 0.0) or 0.0),
                -abs(float(item.get("loss_return_pct", 0.0) or 0.0)),
                str(item.get("trade_date") or ""),
            ),
            reverse=True,
        )
        dominant_patterns = self._build_dominant_patterns(matched)
        top_match = matched[0] if matched else {}
        warning_active = bool(
            top_match
            and (
                str(top_match.get("similarity_level") or "") == "high"
                or len([item for item in matched if str(item.get("similarity_level") or "") in {"high", "medium"}]) >= 2
            )
        )
        summary_lines = [
            f"失败日志匹配 {len(matched)} 条历史样本。"
        ]
        if top_match:
            summary_lines.append(
                "最高相似: "
                + f"{top_match.get('playbook')}@{top_match.get('sector')} "
                + f"{top_match.get('failure_tag')} score={float(top_match.get('similarity_score', 0.0) or 0.0):.2f}"
            )
        if dominant_patterns:
            summary_lines.append(
                "主要失败型: "
                + "；".join(f"{item['tag']}({item['count']})" for item in dominant_patterns[:3])
            )
        return {
            "available": bool(matched),
            "matched_count": len(matched),
            "pattern_recurrence_warning": warning_active,
            "warning_level": (str(top_match.get("similarity_level") or "none") if warning_active else "none"),
            "dominant_failure_patterns": dominant_patterns,
            "matches": matched[:10],
            "latest_monthly_summary": self.latest_monthly_summary(),
            "summary_lines": summary_lines,
        }

    def list_entries(
        self,
        *,
        playbook: str | None = None,
        regime_label: str | None = None,
        sector: str | None = None,
        month: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        items = [self._export_item(dict(item)) for item in list(self._read_payload().get("items") or [])]
        filtered = [
            item
            for item in items
            if (not playbook or str(item.get("playbook") or "") == str(playbook))
            and (not regime_label or str(item.get("regime_label") or "") == str(regime_label))
            and (not sector or str(item.get("sector") or "") == str(sector))
            and (not month or str(item.get("trade_date") or "").startswith(str(month)))
        ]
        filtered.sort(
            key=lambda item: (
                str(item.get("trade_date") or ""),
                abs(float(item.get("loss_return_pct", 0.0) or 0.0)),
            ),
            reverse=True,
        )
        return {
            "available": bool(filtered),
            "count": len(filtered),
            "items": filtered[: max(int(limit or 50), 1)],
            "latest_monthly_summary": self.latest_monthly_summary(month=month),
        }

    def latest_monthly_summary(self, *, month: str | None = None) -> dict[str, Any]:
        payload = self._read_payload()
        monthly = dict(payload.get("monthly_summary") or {})
        if month:
            return dict(monthly.get(month) or {})
        if not monthly:
            return {}
        latest_month = sorted(monthly.keys())[-1]
        return dict(monthly.get(latest_month) or {})

    def _normalize_failure_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        pnl = record.get("holding_return_pct")
        if pnl is None:
            pnl = record.get("next_day_close_pct")
        pnl_value = float(pnl or 0.0)
        if pnl_value >= 0:
            return None

        trade_date = str(record.get("trade_date") or self._now_factory().date().isoformat())
        score_date = str(record.get("score_date") or "")
        symbol = str(record.get("symbol") or "").strip()
        playbook = str(record.get("playbook") or "unassigned").strip() or "unassigned"
        regime_label = str(record.get("regime") or "unknown").strip() or "unknown"
        sector = str(record.get("sector") or record.get("resolved_sector") or "unknown").strip() or "unknown"
        review_tags = [
            str(item).strip()
            for item in list(record.get("review_tags") or [])
            if str(item).strip()
        ]
        failure_tag = self._infer_failure_tag(record, pnl_value)
        if failure_tag not in review_tags:
            review_tags.append(failure_tag)

        sample_id = "|".join(
            [
                trade_date,
                score_date,
                symbol,
                playbook,
                regime_label,
                sector,
                str(record.get("exit_reason") or ""),
            ]
        ).strip("|")
        return {
            "sample_id": sample_id,
            "trade_date": trade_date,
            "score_date": score_date,
            "symbol": symbol,
            "playbook": playbook,
            "regime_label": regime_label,
            "sector": sector,
            "entry_reason": str(
                record.get("entry_reason")
                or record.get("headline_reason")
                or record.get("note")
                or record.get("selected_reason")
                or ""
            ),
            "market_hypothesis": str(
                record.get("market_hypothesis")
                or record.get("note")
                or ""
            ),
            "stop_reason": str(record.get("exit_reason") or ""),
            "loss_return_pct": round(pnl_value, 6),
            "review_tags": review_tags,
            "failure_tag": failure_tag,
            "final_status": str(record.get("final_status") or ""),
            "risk_gate": str(record.get("risk_gate") or ""),
            "audit_gate": str(record.get("audit_gate") or ""),
            "context_signature": f"{regime_label}|{playbook}|{sector}|{failure_tag}",
            "recorded_at": self._now_factory().isoformat(),
        }

    def _build_monthly_summary_map(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        monthly_buckets: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            month = str(item.get("trade_date") or "")[:7]
            if not month:
                continue
            monthly_buckets.setdefault(month, []).append(item)

        summaries: dict[str, Any] = {}
        for month, month_items in monthly_buckets.items():
            tag_counter = Counter(str(item.get("failure_tag") or "unknown") for item in month_items)
            playbook_counter = Counter(str(item.get("playbook") or "unassigned") for item in month_items)
            regime_counter = Counter(str(item.get("regime_label") or "unknown") for item in month_items)
            avg_loss = (
                sum(float(item.get("loss_return_pct", 0.0) or 0.0) for item in month_items) / len(month_items)
                if month_items
                else 0.0
            )
            summaries[month] = {
                "month": month,
                "sample_count": len(month_items),
                "avg_loss_return_pct": round(avg_loss, 6),
                "dominant_failure_tags": [
                    {"tag": key, "count": value}
                    for key, value in tag_counter.most_common(5)
                ],
                "dominant_playbooks": [
                    {"playbook": key, "count": value}
                    for key, value in playbook_counter.most_common(5)
                ],
                "dominant_regimes": [
                    {"regime": key, "count": value}
                    for key, value in regime_counter.most_common(5)
                ],
                "summary_lines": [
                    f"{month} 失败样本 {len(month_items)} 笔，平均亏损 {avg_loss:.2%}。"
                ],
            }
        return summaries

    @staticmethod
    def _build_dominant_patterns(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counter: Counter[str] = Counter()
        for item in items:
            tag = str(item.get("failure_tag") or "unknown")
            level = str(item.get("similarity_level") or "low")
            counter[f"{tag}|{level}"] += 1
        results: list[dict[str, Any]] = []
        for key, value in counter.most_common(5):
            tag, level = key.split("|", 1)
            results.append({"tag": tag, "similarity_level": level, "count": value})
        return results

    @staticmethod
    def _similarity_level(score: float) -> str:
        if score >= 0.8:
            return "high"
        if score >= 0.6:
            return "medium"
        return "low"

    @staticmethod
    def _infer_failure_tag(record: dict[str, Any], pnl_value: float) -> str:
        exit_reason = str(record.get("exit_reason") or "").lower()
        review_tags = ",".join(str(item).lower() for item in list(record.get("review_tags") or []))
        if "delay" in review_tags or "latency" in review_tags:
            return "execution_delay"
        if "regime" in review_tags:
            return "regime_shift"
        if "sector" in review_tags:
            return "wrong_sector"
        if pnl_value < -0.07 or "black_swan" in review_tags:
            return "black_swan"
        if "timing" in review_tags or "entry_failure" in exit_reason:
            return "bad_timing"
        return "bad_timing"

    @staticmethod
    def _export_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "sample_id": str(item.get("sample_id") or ""),
            "trade_date": str(item.get("trade_date") or ""),
            "score_date": str(item.get("score_date") or ""),
            "symbol": str(item.get("symbol") or ""),
            "playbook": str(item.get("playbook") or ""),
            "regime_label": str(item.get("regime_label") or ""),
            "sector": str(item.get("sector") or ""),
            "entry_reason": str(item.get("entry_reason") or ""),
            "market_hypothesis": str(item.get("market_hypothesis") or ""),
            "stop_reason": str(item.get("stop_reason") or ""),
            "loss_return_pct": round(float(item.get("loss_return_pct", 0.0) or 0.0), 6),
            "review_tags": list(item.get("review_tags") or []),
            "failure_tag": str(item.get("failure_tag") or ""),
            "final_status": str(item.get("final_status") or ""),
            "risk_gate": str(item.get("risk_gate") or ""),
            "audit_gate": str(item.get("audit_gate") or ""),
            "context_signature": str(item.get("context_signature") or ""),
            "recorded_at": str(item.get("recorded_at") or ""),
        }

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(
            self._storage_path,
            default={"items": [], "monthly_summary": {}},
        )
        return payload if isinstance(payload, dict) else {"items": [], "monthly_summary": {}}
