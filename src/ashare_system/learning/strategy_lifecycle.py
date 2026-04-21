"""策略生命周期管理。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class StrategyLifecycleService:
    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def refresh_from_attribution(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._read_payload()
        lifecycle_map: dict[str, Any] = {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in list(records or []):
            playbook = str(record.get("playbook") or "unassigned")
            factor_combo = self._resolve_factor_combo(record)
            grouped.setdefault(f"{playbook}|{factor_combo}", []).append(dict(record))
        for key, items in grouped.items():
            returns = [float(item.get("holding_return_pct", item.get("next_day_close_pct", 0.0)) or 0.0) for item in items]
            trade_count = len(items)
            avg_return = sum(returns) / max(trade_count, 1)
            volatility = (sum((value - avg_return) ** 2 for value in returns) / max(trade_count - 1, 1)) ** 0.5 if trade_count >= 2 else 0.0
            sharpe = avg_return / max(volatility, 1e-6) if trade_count >= 2 else 0.0
            cumulative = 1.0
            peak = 1.0
            max_drawdown = 0.0
            for value in returns:
                cumulative *= 1.0 + value
                peak = max(peak, cumulative)
                drawdown = cumulative / max(peak, 1e-9) - 1.0
                max_drawdown = min(max_drawdown, drawdown)
            state, max_position_fraction = self._resolve_state(trade_count, sharpe, max_drawdown, returns[-20:])
            playbook, factor_combo = key.split("|", 1)
            lifecycle_map[key] = {
                "playbook": playbook,
                "factor_combo": factor_combo,
                "trade_count": trade_count,
                "sharpe": round(sharpe, 6),
                "max_drawdown": round(max_drawdown, 6),
                "status": state,
                "max_position_fraction": max_position_fraction,
                "updated_at": self._now_factory().isoformat(),
            }
        payload["items"] = lifecycle_map
        atomic_write_json(self._storage_path, payload)
        return {"available": bool(lifecycle_map), "count": len(lifecycle_map)}

    def get_cap(self, *, playbook: str, factor_combo: str | None = None) -> dict[str, Any]:
        items = dict(self._read_payload().get("items") or {})
        if factor_combo:
            direct_key = f"{playbook}|{factor_combo}"
            if direct_key in items:
                return dict(items[direct_key])
        matches = [dict(value) for key, value in items.items() if key.startswith(f"{playbook}|")]
        if not matches:
            return {
                "playbook": playbook,
                "factor_combo": factor_combo or "default",
                "status": "incubation",
                "max_position_fraction": 0.25,
                "trade_count": 0,
            }
        matches.sort(key=lambda item: (-float(item.get("max_position_fraction", 0.0) or 0.0), -int(item.get("trade_count", 0) or 0)))
        return matches[0]

    @staticmethod
    def _resolve_factor_combo(record: dict[str, Any]) -> str:
        combo = record.get("factor_combo")
        if combo:
            return str(combo)
        factors = list(record.get("factor_ids") or [])
        if factors:
            return "+".join(sorted(str(item) for item in factors))
        return "default"

    @staticmethod
    def _resolve_state(trade_count: int, sharpe: float, max_drawdown: float, recent_returns: list[float]) -> tuple[str, float]:
        if len(recent_returns) >= 20:
            recent_avg = sum(recent_returns) / len(recent_returns)
            recent_vol = (sum((value - recent_avg) ** 2 for value in recent_returns) / max(len(recent_returns) - 1, 1)) ** 0.5
            recent_sharpe = recent_avg / max(recent_vol, 1e-6)
            if recent_sharpe < 0 or max_drawdown <= -0.15:
                return "sunset", 0.25
        if trade_count > 30 and sharpe > 1.0 and max_drawdown > -0.10:
            return "production", 1.0
        if 10 <= trade_count <= 30 and sharpe > 0.5:
            return "probation", 0.5
        return "incubation", 0.25

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"items": {}})
        return payload if isinstance(payload, dict) else {"items": {}}
