"""持仓压力测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json
from typing import Any

from ..contracts import PositionSnapshot


@dataclass
class StressScenario:
    id: str
    label: str
    shock_pct: float
    liquidity_haircut_pct: float = 0.0
    notes: list[str] = field(default_factory=list)


DEFAULT_SCENARIOS = [
    StressScenario(id="index_gap_down_5", label="指数低开 5%", shock_pct=-0.05, liquidity_haircut_pct=0.01),
    StressScenario(id="sector_meltdown_8", label="主线板块退潮 8%", shock_pct=-0.08, liquidity_haircut_pct=0.015),
    StressScenario(id="black_swan_12", label="黑天鹅下跌 12%", shock_pct=-0.12, liquidity_haircut_pct=0.03),
]


class StressTestService:
    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        positions: list[PositionSnapshot],
        *,
        total_asset: float = 0.0,
        scenarios: list[StressScenario] | None = None,
    ) -> dict[str, Any]:
        scenarios = scenarios or DEFAULT_SCENARIOS
        items: list[dict[str, Any]] = []
        worst_loss_pct = 0.0
        worst_scenario = ""
        gross_exposure = sum(float(pos.last_price or 0.0) * int(pos.quantity or 0) for pos in positions)
        for scenario in scenarios:
            stressed_value = 0.0
            for pos in positions:
                current_value = float(pos.last_price or 0.0) * int(pos.quantity or 0)
                shocked_price = max(float(pos.last_price or 0.0) * (1 + scenario.shock_pct - scenario.liquidity_haircut_pct), 0.0)
                stressed_value += shocked_price * int(pos.quantity or 0)
            loss_amount = gross_exposure - stressed_value
            loss_pct = (loss_amount / max(total_asset or gross_exposure or 1.0, 1.0)) if positions else 0.0
            items.append(
                {
                    "scenario_id": scenario.id,
                    "label": scenario.label,
                    "shock_pct": scenario.shock_pct,
                    "liquidity_haircut_pct": scenario.liquidity_haircut_pct,
                    "loss_amount": round(loss_amount, 2),
                    "loss_pct": round(loss_pct, 6),
                    "notes": list(scenario.notes),
                }
            )
            if loss_pct < worst_loss_pct:
                worst_loss_pct = loss_pct
                worst_scenario = scenario.label
        recommendations: list[str] = []
        if worst_loss_pct <= -0.08:
            recommendations.append("最差情景下组合回撤超过 8%，建议盘前降低高弹性仓位。")
        if worst_loss_pct <= -0.12:
            recommendations.append("最差情景已接近黑天鹅阈值，建议准备对冲或严格缩减仓位。")
        payload = {
            "generated_at": datetime.now().isoformat(),
            "position_count": len(positions),
            "gross_exposure": round(gross_exposure, 2),
            "total_asset": round(total_asset, 2),
            "worst_loss_pct": round(worst_loss_pct, 6),
            "worst_scenario": worst_scenario,
            "items": items,
            "recommendations": recommendations,
        }
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def latest(self) -> dict[str, Any]:
        if not self._storage_path.exists():
            return {}
        try:
            return json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
