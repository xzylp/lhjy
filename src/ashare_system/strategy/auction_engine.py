"""集合竞价研判引擎。

基于 09:24 竞价快照对候选票进行 PROMOTE / HOLD / DEMOTE / KILL 筛选。
不同 playbook 有不同的竞价准入标准。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contracts import AuctionSignal, AuctionSnapshot
from ..logging_config import get_logger

logger = get_logger("strategy.auction_engine")

# ── 各 playbook 的竞价阈值 ───────────────────────────────

AUCTION_THRESHOLDS: dict[str, dict[str, float]] = {
    "leader_chase": {
        "promote_volume_ratio": 0.50,   # 竞价量/5日均量 >= 50% → PROMOTE
        "promote_change_pct": 0.03,     # 竞价涨幅 >= 3% → PROMOTE
        "kill_change_pct": -0.02,       # 竞价低开 > 2% → KILL
        "kill_volume_ratio": 0.20,      # 竞价量/5日均量 < 20% → KILL
        "limit_up_pct": 0.098,          # 竞价封死涨停 → DEMOTE（来不及买）
    },
    "divergence_reseal": {
        "promote_volume_ratio": 0.30,
        "promote_change_pct": 0.01,
        "kill_change_pct": -0.03,
        "kill_volume_ratio": 0.15,
        "limit_up_pct": 0.098,
    },
    "sector_reflow_first_board": {
        "promote_volume_ratio": 0.40,
        "promote_change_pct": 0.02,
        "kill_change_pct": -0.02,
        "kill_volume_ratio": 0.20,
        "limit_up_pct": 0.098,
    },
}


class AuctionEngine:
    """集合竞价监控与预判引擎。"""

    def evaluate_auction_snapshot(
        self,
        snapshot: AuctionSnapshot,
        assigned_playbook: str,
    ) -> AuctionSignal:
        """对单个标的的竞价快照进行研判。

        Args:
            snapshot: 竞价快照
            assigned_playbook: 该标的被路由到的战法

        Returns:
            PROMOTE / HOLD / DEMOTE / KILL 信号

        TODO:
            1. 补充更多边缘条件：如竞价量在 09:20→09:24 的变化趋势
            2. 加入板块级别的竞价共振判断（同板块多只票同时高开）
        """
        thresholds = AUCTION_THRESHOLDS.get(assigned_playbook, AUCTION_THRESHOLDS["leader_chase"])
        volume_ratio = (
            snapshot.volume / snapshot.prev_volume_5d_avg
            if snapshot.prev_volume_5d_avg > 0 else 0.0
        )
        change_pct = snapshot.open_change_pct

        # KILL: 低开过多 或 无量
        if change_pct <= thresholds["kill_change_pct"]:
            return AuctionSignal(
                symbol=snapshot.symbol,
                action="KILL",
                reason=f"竞价低开 {change_pct:.1%}，低于阈值 {thresholds['kill_change_pct']:.1%}",
                auction_volume_ratio=round(volume_ratio, 4),
                open_change_pct=round(change_pct, 6),
                playbook=assigned_playbook,
            )
        if volume_ratio < thresholds["kill_volume_ratio"] and volume_ratio > 0:
            return AuctionSignal(
                symbol=snapshot.symbol,
                action="KILL",
                reason=f"竞价量比 {volume_ratio:.2f}，低于阈值 {thresholds['kill_volume_ratio']:.2f}",
                auction_volume_ratio=round(volume_ratio, 4),
                open_change_pct=round(change_pct, 6),
                playbook=assigned_playbook,
            )

        # DEMOTE: 封死涨停，来不及买
        if change_pct >= thresholds["limit_up_pct"]:
            return AuctionSignal(
                symbol=snapshot.symbol,
                action="DEMOTE",
                reason=f"竞价封死涨停 {change_pct:.1%}，排板追入风险高",
                auction_volume_ratio=round(volume_ratio, 4),
                open_change_pct=round(change_pct, 6),
                playbook=assigned_playbook,
            )

        # PROMOTE: 量价齐升
        if (volume_ratio >= thresholds["promote_volume_ratio"]
                and change_pct >= thresholds["promote_change_pct"]):
            confidence = min(volume_ratio, 1.0) * 0.6 + min(change_pct / 0.05, 1.0) * 0.4
            return AuctionSignal(
                symbol=snapshot.symbol,
                action="PROMOTE",
                reason=f"竞价量比 {volume_ratio:.2f} 涨幅 {change_pct:.1%}，达标",
                auction_volume_ratio=round(volume_ratio, 4),
                open_change_pct=round(change_pct, 6),
                playbook=assigned_playbook,
                confidence=round(confidence, 4),
            )

        # 默认 HOLD
        return AuctionSignal(
            symbol=snapshot.symbol,
            action="HOLD",
            reason="竞价指标未触发显著信号",
            auction_volume_ratio=round(volume_ratio, 4),
            open_change_pct=round(change_pct, 6),
            playbook=assigned_playbook,
        )

    def evaluate_all(
        self,
        snapshots: list[AuctionSnapshot],
        playbook_map: dict[str, str] | None = None,
        sector_map: dict[str, str] | None = None,
        prev_signal_map: dict[str, AuctionSignal | dict[str, Any]] | None = None,
    ) -> dict[str, AuctionSignal]:
        """批量研判所有候选的竞价快照。

        Args:
            snapshots: 竞价快照列表
            playbook_map: {symbol: playbook_name}

        Returns:
            {symbol: AuctionSignal} 字典

        TODO:
            1. 加入板块级竞价共振逻辑
            2. 加入与 T-1 日竞价数据的对比
        """
        playbook_map = playbook_map or {}
        sector_map = sector_map or {}
        prev_signal_map = prev_signal_map or {}
        sector_stats = self._build_sector_resonance_stats(snapshots, playbook_map, sector_map)
        results: dict[str, AuctionSignal] = {}
        for snap in snapshots:
            playbook = playbook_map.get(snap.symbol, "leader_chase")
            signal = self.evaluate_auction_snapshot(snap, playbook)
            signal = self._apply_sector_resonance(
                signal=signal,
                snapshot=snap,
                playbook=playbook,
                sector_stats=sector_stats,
                sector_map=sector_map,
            )
            signal = self._apply_prev_day_comparison(
                signal=signal,
                snapshot=snap,
                previous_signal=prev_signal_map.get(snap.symbol),
            )
            results[snap.symbol] = signal
            if signal.action in {"PROMOTE", "KILL"}:
                logger.info("竞价信号 [%s] %s: %s — %s",
                           signal.action, snap.symbol, signal.reason, playbook)
        promoted = sum(1 for s in results.values() if s.action == "PROMOTE")
        killed = sum(1 for s in results.values() if s.action == "KILL")
        logger.info("竞价研判完成: total=%d PROMOTE=%d KILL=%d",
                     len(results), promoted, killed)
        return results

    def _build_sector_resonance_stats(
        self,
        snapshots: list[AuctionSnapshot],
        playbook_map: dict[str, str],
        sector_map: dict[str, str],
    ) -> dict[str, dict[str, float]]:
        stats: dict[str, dict[str, float]] = {}
        for item in snapshots:
            sector = str(sector_map.get(item.symbol, "") or "")
            if not sector:
                continue
            playbook = playbook_map.get(item.symbol, "leader_chase")
            thresholds = AUCTION_THRESHOLDS.get(playbook, AUCTION_THRESHOLDS["leader_chase"])
            volume_ratio = item.volume / item.prev_volume_5d_avg if item.prev_volume_5d_avg > 0 else 0.0
            change_pct = item.open_change_pct
            bucket = stats.setdefault(sector, {"promote_like_count": 0, "symbol_count": 0, "avg_change_pct": 0.0})
            bucket["symbol_count"] += 1
            bucket["avg_change_pct"] += change_pct
            if (
                change_pct >= thresholds["promote_change_pct"]
                and volume_ratio >= thresholds["promote_volume_ratio"] * 0.8
            ):
                bucket["promote_like_count"] += 1
        for sector, payload in stats.items():
            symbol_count = max(int(payload["symbol_count"]), 1)
            payload["avg_change_pct"] = round(float(payload["avg_change_pct"]) / symbol_count, 6)
        return stats

    def _apply_sector_resonance(
        self,
        *,
        signal: AuctionSignal,
        snapshot: AuctionSnapshot,
        playbook: str,
        sector_stats: dict[str, dict[str, float]],
        sector_map: dict[str, str],
    ) -> AuctionSignal:
        sector = str(sector_map.get(snapshot.symbol, "") or "")
        if not sector:
            return signal
        stats = sector_stats.get(sector) or {}
        promote_like_count = int(stats.get("promote_like_count", 0) or 0)
        avg_change_pct = float(stats.get("avg_change_pct", 0.0) or 0.0)
        if promote_like_count < 2 or signal.action in {"KILL", "DEMOTE"}:
            return signal

        reason = signal.reason
        confidence = float(signal.confidence or 0.0)
        if signal.action == "HOLD":
            return signal.model_copy(
                update={
                    "action": "PROMOTE",
                    "reason": f"{reason}；板块竞价共振 {sector} 同步走强({promote_like_count}只, 均涨 {avg_change_pct:.1%})",
                    "confidence": round(min(confidence + 0.12, 1.0), 4),
                }
            )
        return signal.model_copy(
            update={
                "reason": f"{reason}；板块竞价共振 {sector} 同步走强({promote_like_count}只)",
                "confidence": round(min(confidence + 0.08, 1.0), 4),
            }
        )

    def _apply_prev_day_comparison(
        self,
        *,
        signal: AuctionSignal,
        snapshot: AuctionSnapshot,
        previous_signal: AuctionSignal | dict[str, Any] | None,
    ) -> AuctionSignal:
        if previous_signal is None or signal.action in {"KILL", "DEMOTE"}:
            return signal
        previous_open = self._safe_float(
            previous_signal.open_change_pct if isinstance(previous_signal, AuctionSignal) else previous_signal.get("open_change_pct")
        )
        previous_volume_ratio = self._safe_float(
            previous_signal.auction_volume_ratio if isinstance(previous_signal, AuctionSignal) else previous_signal.get("auction_volume_ratio")
        )
        current_volume_ratio = float(signal.auction_volume_ratio or 0.0)
        current_open = float(snapshot.open_change_pct or 0.0)
        open_delta = current_open - previous_open
        volume_delta = current_volume_ratio - previous_volume_ratio
        if signal.action == "HOLD" and open_delta >= 0.015 and volume_delta >= 0.10:
            return signal.model_copy(
                update={
                    "action": "PROMOTE",
                    "reason": (
                        f"{signal.reason}；较前次竞价改善"
                        f"(涨幅 {open_delta:+.1%}, 量比 {volume_delta:+.2f})"
                    ),
                    "confidence": round(min(float(signal.confidence or 0.0) + 0.10, 1.0), 4),
                }
            )
        if signal.action == "PROMOTE" and open_delta <= -0.02 and volume_delta <= -0.15:
            return signal.model_copy(
                update={
                    "action": "HOLD",
                    "reason": (
                        f"{signal.reason}；较前次竞价走弱"
                        f"(涨幅 {open_delta:+.1%}, 量比 {volume_delta:+.2f})"
                    ),
                    "confidence": round(max(float(signal.confidence or 0.0) - 0.15, 0.0), 4),
                }
            )
        return signal

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
