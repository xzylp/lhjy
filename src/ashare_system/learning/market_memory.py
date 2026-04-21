"""市场记忆库。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class MarketMemoryService:
    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def update_from_attribution(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._read_payload()
        items = dict(payload.get("items") or {})
        updated_keys: set[str] = set()
        for record in list(records or []):
            normalized = self._normalize_record(record)
            if not normalized["sample_id"]:
                continue
            keys = [
                self._memory_key(normalized["regime"], normalized["playbook"], normalized["sector"]),
                self._memory_key(normalized["regime"], normalized["playbook"], "*"),
            ]
            for key in keys:
                current = dict(items.get(key) or {})
                samples = dict(current.get("_samples") or {})
                samples[normalized["sample_id"]] = {
                    "sample_id": normalized["sample_id"],
                    "trade_date": normalized["trade_date"],
                    "score_date": normalized["score_date"],
                    "symbol": normalized["symbol"],
                    "return_pct": normalized["return_pct"],
                    "holding_days": normalized["holding_days"],
                }
                refreshed = self._rebuild_entry(
                    regime=normalized["regime"],
                    playbook=normalized["playbook"],
                    sector=("*" if key.endswith("|*") else normalized["sector"]),
                    samples=samples,
                )
                items[key] = refreshed
                updated_keys.add(key)
        payload["items"] = items
        payload["latest_summary"] = self._build_global_summary(items)
        payload["updated_at"] = self._now_factory().isoformat()
        atomic_write_json(self._storage_path, payload)
        return {
            "available": bool(items),
            "count": len(items),
            "updated_count": len(updated_keys),
            "latest_summary": dict(payload.get("latest_summary") or {}),
        }

    def build_compose_context(
        self,
        *,
        regime_label: str,
        playbooks: list[str],
        sectors: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._read_payload()
        items = dict(payload.get("items") or {})
        resolved_playbooks = [str(item).strip() for item in list(playbooks or []) if str(item).strip()]
        resolved_sectors = [str(item).strip() for item in list(sectors or []) if str(item).strip()] or ["*"]

        matches: list[dict[str, Any]] = []
        avoid_patterns: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for playbook in resolved_playbooks:
            candidate_keys = [
                self._memory_key(regime_label, playbook, sector)
                for sector in resolved_sectors
            ] + [self._memory_key(regime_label, playbook, "*")]
            for key in candidate_keys:
                if key in seen_keys:
                    continue
                entry = dict(items.get(key) or {})
                if not entry:
                    continue
                seen_keys.add(key)
                exported = self._export_entry(entry)
                exported["expectancy_score"] = self._expectancy_score(exported)
                exported["confidence_tier"] = self._confidence_tier(exported)
                matches.append(exported)
                if (
                    int(exported.get("sample_count", 0) or 0) >= 5
                    and float(exported.get("win_rate", 0.0) or 0.0) < 0.3
                ):
                    avoid_patterns.append(
                        {
                            "regime": exported.get("regime"),
                            "playbook": exported.get("playbook"),
                            "sector": exported.get("sector"),
                            "sample_count": exported.get("sample_count", 0),
                            "win_rate": exported.get("win_rate", 0.0),
                            "avg_return": exported.get("avg_return", 0.0),
                            "reason": "win_rate_below_0.3",
                            "severity": (
                                "critical"
                                if float(exported.get("avg_return", 0.0) or 0.0) <= -0.03
                                else "warning"
                            ),
                            "recommended_action": (
                                "暂停该组合，优先换 sector 或 playbook"
                                if exported.get("sector") != "*"
                                else "该 regime 下谨慎使用该 playbook"
                            ),
                        }
                    )

        matches.sort(
            key=lambda item: (
                float(item.get("expectancy_score", 0.0) or 0.0),
                float(item.get("win_rate", 0.0) or 0.0),
                float(item.get("avg_return", 0.0) or 0.0),
                int(item.get("sample_count", 0) or 0),
            ),
            reverse=True,
        )
        recommendations = [
            {
                "playbook": str(item.get("playbook") or ""),
                "sector": str(item.get("sector") or ""),
                "reason": (
                    f"历史样本 {int(item.get('sample_count', 0) or 0)} 笔，"
                    f"win_rate={float(item.get('win_rate', 0.0) or 0.0):.1%}，"
                    f"avg_return={float(item.get('avg_return', 0.0) or 0.0):+.2%}"
                ),
                "confidence_tier": item.get("confidence_tier"),
            }
            for item in matches[:3]
        ]
        summary_lines = [
            f"市场记忆命中 {len(matches)} 条，回避模式 {len(avoid_patterns)} 条。"
        ]
        if recommendations:
            summary_lines.append(
                "优先参考: "
                + "；".join(
                    f"{item['playbook']}@{item['sector']}({item['confidence_tier']})"
                    for item in recommendations
                )
            )
        if avoid_patterns:
            summary_lines.append(
                "回避提示: "
                + "；".join(
                    f"{item['playbook']}@{item['sector']}[{item['severity']}]"
                    for item in avoid_patterns[:3]
                )
            )
        return {
            "available": bool(matches),
            "items": matches,
            "avoid_pattern": avoid_patterns,
            "recommended_combinations": recommendations,
            "summary_lines": summary_lines,
        }

    def list_entries(
        self,
        *,
        regime_label: str | None = None,
        playbook: str | None = None,
        sector: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload = self._read_payload()
        entries = [self._export_entry(dict(item)) for item in dict(payload.get("items") or {}).values()]
        filtered = [
            item
            for item in entries
            if (not regime_label or str(item.get("regime") or "") == str(regime_label))
            and (not playbook or str(item.get("playbook") or "") == str(playbook))
            and (not sector or str(item.get("sector") or "") == str(sector))
        ]
        filtered.sort(
            key=lambda item: (
                int(item.get("sample_count", 0) or 0),
                float(item.get("win_rate", 0.0) or 0.0),
                float(item.get("avg_return", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return {
            "available": bool(filtered),
            "count": len(filtered),
            "items": filtered[: max(int(limit or 50), 1)],
            "summary": dict(payload.get("latest_summary") or {}),
        }

    def latest_summary(self) -> dict[str, Any]:
        payload = self._read_payload()
        return dict(payload.get("latest_summary") or {})

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        trade_date = str(record.get("trade_date") or self._now_factory().date().isoformat())
        score_date = str(record.get("score_date") or "")
        symbol = str(record.get("symbol") or "").strip()
        regime = str(record.get("regime") or "unknown").strip() or "unknown"
        playbook = str(record.get("playbook") or "unassigned").strip() or "unassigned"
        sector = str(record.get("sector") or record.get("resolved_sector") or "unknown").strip() or "unknown"
        raw_return = record.get("holding_return_pct")
        if raw_return is None:
            raw_return = record.get("next_day_close_pct")
        holding_days = int(record.get("holding_days", 1) or 1)
        sample_id = "|".join(
            [
                trade_date,
                score_date,
                symbol,
                regime,
                playbook,
                sector,
            ]
        ).strip("|")
        return {
            "sample_id": sample_id,
            "trade_date": trade_date,
            "score_date": score_date,
            "symbol": symbol,
            "regime": regime,
            "playbook": playbook,
            "sector": sector,
            "return_pct": round(float(raw_return or 0.0), 6),
            "holding_days": holding_days,
        }

    @staticmethod
    def _memory_key(regime: str, playbook: str, sector: str) -> str:
        return f"{str(regime or 'unknown')}|{str(playbook or 'unassigned')}|{str(sector or 'unknown')}"

    def _rebuild_entry(
        self,
        *,
        regime: str,
        playbook: str,
        sector: str,
        samples: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        ordered_samples = sorted(
            samples.values(),
            key=lambda item: (
                str(item.get("trade_date") or ""),
                str(item.get("symbol") or ""),
            ),
        )[-120:]
        returns = [float(item.get("return_pct", 0.0) or 0.0) for item in ordered_samples]
        holding_days = [int(item.get("holding_days", 1) or 1) for item in ordered_samples]
        sample_count = len(ordered_samples)
        win_count = sum(1 for value in returns if value > 0)
        loss_count = sum(1 for value in returns if value < 0)
        return {
            "regime": regime,
            "playbook": playbook,
            "sector": sector,
            "sample_count": sample_count,
            "avg_return": round(sum(returns) / sample_count, 6) if sample_count else 0.0,
            "win_rate": round(win_count / sample_count, 6) if sample_count else 0.0,
            "loss_rate": round(loss_count / sample_count, 6) if sample_count else 0.0,
            "avg_holding_days": round(sum(holding_days) / len(holding_days), 4) if holding_days else 0.0,
            "recent_20_avg_return": round(sum(returns[-20:]) / min(len(returns[-20:]), 20), 6) if returns else 0.0,
            "last_updated": self._now_factory().date().isoformat(),
            "_samples": {
                str(item.get("sample_id") or self._sample_storage_key(item)): item
                for item in ordered_samples
            },
        }

    @staticmethod
    def _sample_storage_key(sample: dict[str, Any]) -> str:
        return "|".join(
            [
                str(sample.get("trade_date") or ""),
                str(sample.get("score_date") or ""),
                str(sample.get("symbol") or ""),
            ]
        ).strip("|")

    def _build_global_summary(self, items: dict[str, Any]) -> dict[str, Any]:
        exported = [self._export_entry(dict(item)) for item in items.values()]
        exact_entries = [item for item in exported if str(item.get("sector") or "") != "*"]
        avoid_candidates = [
            item
            for item in exact_entries
            if int(item.get("sample_count", 0) or 0) >= 5 and float(item.get("win_rate", 0.0) or 0.0) < 0.3
        ]
        best = sorted(
            exact_entries,
            key=lambda item: self._expectancy_score(item),
            reverse=True,
        )[:5]
        return {
            "generated_at": self._now_factory().isoformat(),
            "entry_count": len(exact_entries),
            "avoid_pattern_count": len(avoid_candidates),
            "best_patterns": [
                {
                    "regime": item.get("regime"),
                    "playbook": item.get("playbook"),
                    "sector": item.get("sector"),
                    "sample_count": item.get("sample_count", 0),
                    "win_rate": item.get("win_rate", 0.0),
                    "avg_return": item.get("avg_return", 0.0),
                }
                for item in best
            ],
            "summary_lines": [
                f"市场记忆库当前有 {len(exact_entries)} 条精确模式，需回避 {len(avoid_candidates)} 条。"
            ],
        }

    @staticmethod
    def _expectancy_score(entry: dict[str, Any]) -> float:
        sample_count = int(entry.get("sample_count", 0) or 0)
        win_rate = float(entry.get("win_rate", 0.0) or 0.0)
        avg_return = float(entry.get("avg_return", 0.0) or 0.0)
        recent_avg = float(entry.get("recent_20_avg_return", 0.0) or 0.0)
        return round(win_rate * 60.0 + avg_return * 400.0 + recent_avg * 200.0 + min(sample_count, 20) * 0.5, 4)

    @staticmethod
    def _confidence_tier(entry: dict[str, Any]) -> str:
        sample_count = int(entry.get("sample_count", 0) or 0)
        win_rate = float(entry.get("win_rate", 0.0) or 0.0)
        if sample_count >= 15 and win_rate >= 0.6:
            return "high"
        if sample_count >= 8 and win_rate >= 0.45:
            return "medium"
        return "low"

    @staticmethod
    def _export_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "regime": str(entry.get("regime") or "unknown"),
            "playbook": str(entry.get("playbook") or "unassigned"),
            "sector": str(entry.get("sector") or "unknown"),
            "sample_count": int(entry.get("sample_count", 0) or 0),
            "avg_return": round(float(entry.get("avg_return", 0.0) or 0.0), 6),
            "win_rate": round(float(entry.get("win_rate", 0.0) or 0.0), 6),
            "loss_rate": round(float(entry.get("loss_rate", 0.0) or 0.0), 6),
            "avg_holding_days": round(float(entry.get("avg_holding_days", 0.0) or 0.0), 4),
            "recent_20_avg_return": round(float(entry.get("recent_20_avg_return", 0.0) or 0.0), 6),
            "last_updated": str(entry.get("last_updated") or ""),
        }

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(
            self._storage_path,
            default={"items": {}, "latest_summary": {}},
        )
        return payload if isinstance(payload, dict) else {"items": {}, "latest_summary": {}}
