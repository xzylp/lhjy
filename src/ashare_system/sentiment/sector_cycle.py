"""板块联动引擎：基于板块统计构建 SectorProfile。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import SectorLifeCycle, SectorProfile


@dataclass
class SectorData:
    zt_count: int = 0
    prev_zt_count: int = 0
    up_count: int = 0
    total_count: int = 0
    active_days: int = 0
    board_fail_rate: float = 0.0
    top_symbols: list[str] = field(default_factory=list)
    avg_return_pct: float = 0.0
    prev_avg_return_pct: float = 0.0


class SectorCycle:
    """输出按强度排序的板块画像。"""

    def build_profiles(self, sector_data: dict[str, SectorData]) -> list[SectorProfile]:
        profiles = [
            self._analyze_sector(sector_name, data)
            for sector_name, data in sector_data.items()
        ]
        return sorted(profiles, key=lambda item: item.strength_score, reverse=True)

    def _analyze_sector(self, name: str, data: SectorData) -> SectorProfile:
        up_ratio = data.up_count / max(data.total_count, 1)
        breadth = self._calc_breadth(data)
        reflow = self._calc_reflow_score(data)
        strength = self._calc_strength(data, up_ratio, breadth, reflow)
        life_cycle = self._classify_lifecycle(data, up_ratio, reflow)
        return SectorProfile(
            sector_name=name,
            life_cycle=life_cycle,
            strength_score=strength,
            zt_count=data.zt_count,
            up_ratio=up_ratio,
            breadth_score=breadth,
            reflow_score=reflow,
            leader_symbols=list(data.top_symbols[:3]),
            active_days=data.active_days,
            zt_count_delta=data.zt_count - data.prev_zt_count,
        )

    @staticmethod
    def _calc_breadth(data: SectorData) -> float:
        up_ratio = data.up_count / max(data.total_count, 1)
        return round(min(max(up_ratio, 0.0), 1.0), 3)

    @staticmethod
    def _calc_reflow_score(data: SectorData) -> float:
        delta = data.avg_return_pct - data.prev_avg_return_pct
        return round(max(-1.0, min(1.0, delta / 10.0)), 3)

    @staticmethod
    def _calc_strength(data: SectorData, up_ratio: float, breadth: float, reflow: float) -> float:
        zt_score = min(data.zt_count / 5.0, 1.0)
        active_score = min(data.active_days / 5.0, 1.0)
        raw = zt_score * 0.35 + up_ratio * 0.25 + breadth * 0.15 + max(reflow, 0.0) * 0.15 + active_score * 0.10
        return round(min(max(raw, 0.0), 1.0), 3)

    def _classify_lifecycle(self, data: SectorData, up_ratio: float, reflow: float) -> SectorLifeCycle:
        zt_delta = data.zt_count - data.prev_zt_count
        if zt_delta < -2 or (reflow < -0.05 and data.board_fail_rate > 0.30):
            return "retreat"
        if data.zt_count >= 3 and zt_delta < 0 and data.board_fail_rate > 0.30:
            return "climax"
        if data.zt_count >= 3 and up_ratio > 0.6 and zt_delta >= 0:
            return "ferment"
        if data.zt_count <= 1 and reflow > 0.05:
            return "start"
        return "start"
