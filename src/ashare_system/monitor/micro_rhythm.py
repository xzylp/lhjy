"""盘中微观节奏追踪器。

为每只持仓票维护一个 1 分钟 K 线滑动窗口，检测三种微观节奏信号：
- PEAK_FADE:  冲高回落（分钟高点创新高但收盘跌回前一分钟区间）
- VALLEY_HOLD: 探底企稳（分钟低点创新低但最后价格拉回 50% 以上）
- RHYTHM_BREAK: 节奏断裂（连续 3 根阴线 + 量能递减）
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

from ..contracts import MicroBarSnapshot, MicroSignal
from ..logging_config import get_logger

logger = get_logger("monitor.micro_rhythm")

# 滑动窗口大小
WINDOW_SIZE = 5

# 信号检测阈值
PEAK_FADE_MIN_NEW_HIGH_PCT = 0.002    # 新高幅度至少 0.2%
VALLEY_HOLD_REBOUND_RATIO = 0.50      # 从低点拉回至少 50%
RHYTHM_BREAK_MIN_BARS = 3             # 至少连续 3 根阴线


class MicroRhythmTracker:
    """分钟级微观节奏追踪器。"""

    def __init__(self) -> None:
        self._windows: dict[str, deque[MicroBarSnapshot]] = {}

    def push_bar(self, bar: MicroBarSnapshot) -> MicroSignal | None:
        """推入一根新的 1 分钟 K 线，返回检测到的微观信号（如有）。

        Args:
            bar: 1 分钟 K 线快照

        Returns:
            检测到的 MicroSignal，未检测到则返回 None
        """
        symbol = bar.symbol
        if symbol not in self._windows:
            self._windows[symbol] = deque(maxlen=WINDOW_SIZE)
        window = self._windows[symbol]
        window.append(bar)

        if len(window) < 2:
            return None

        # 按优先级依次检测
        signal = self._detect_peak_fade(symbol, window)
        if signal is not None:
            return signal

        signal = self._detect_valley_hold(symbol, window)
        if signal is not None:
            return signal

        signal = self._detect_rhythm_break(symbol, window)
        if signal is not None:
            return signal

        return None

    def check_positions(
        self,
        positions: list[dict[str, Any]],
        latest_bars: dict[str, MicroBarSnapshot],
    ) -> list[MicroSignal]:
        """批量检查所有持仓票的微观信号。

        Args:
            positions: 持仓列表 [{symbol, ...}, ...]
            latest_bars: {symbol: 最新 1 分钟 bar}

        Returns:
            产生的微观信号列表

        TODO:
            1. 从 QMT Gateway 或 akshare 拉取 1 分钟线数据
            2. 调用 push_bar() 并收集信号
        """
        signals: list[MicroSignal] = []
        for pos in positions:
            symbol = str(pos.get("symbol", ""))
            bar = latest_bars.get(symbol)
            if bar is None:
                continue
            signal = self.push_bar(bar)
            if signal is not None:
                signals.append(signal)
                logger.info("微观信号 [%s] %s: strength=%.2f",
                           signal.signal_type, symbol, signal.strength)
        return signals

    def clear(self, symbol: str | None = None) -> None:
        """清除指定标的或全部窗口。"""
        if symbol is None:
            self._windows.clear()
        else:
            self._windows.pop(symbol, None)

    def _detect_peak_fade(
        self, symbol: str, window: deque[MicroBarSnapshot]
    ) -> MicroSignal | None:
        """检测冲高回落信号。

        条件：
        - 当前 bar 的 high > 前一 bar 的 high（创新高）
        - 但 close < 前一 bar 的 close（回落）
        - 新高幅度至少 PEAK_FADE_MIN_NEW_HIGH_PCT
        """
        current = window[-1]
        prev = window[-2]

        if current.high <= prev.high:
            return None
        new_high_pct = (current.high - prev.high) / prev.high if prev.high > 0 else 0
        if new_high_pct < PEAK_FADE_MIN_NEW_HIGH_PCT:
            return None
        if current.close >= prev.close:
            return None

        fade_depth = (current.high - current.close) / current.high if current.high > 0 else 0
        strength = min(fade_depth / 0.02, 1.0)
        return MicroSignal(
            symbol=symbol,
            signal_type="PEAK_FADE",
            strength=round(strength, 4),
            timestamp=current.timestamp,
            bar_count=2,
            notes=[f"新高 {new_high_pct:.2%}，回落 {fade_depth:.2%}"],
        )

    def _detect_valley_hold(
        self, symbol: str, window: deque[MicroBarSnapshot]
    ) -> MicroSignal | None:
        """检测探底企稳信号。

        条件：
        - 当前 bar 的 low < 前一 bar 的 low（创新低）
        - 但 close > (high + low) / 2（从低点拉回 50% 以上）
        """
        current = window[-1]
        prev = window[-2]

        if current.low >= prev.low:
            return None
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None
        rebound_ratio = (current.close - current.low) / bar_range
        if rebound_ratio < VALLEY_HOLD_REBOUND_RATIO:
            return None

        strength = min(rebound_ratio, 1.0)
        return MicroSignal(
            symbol=symbol,
            signal_type="VALLEY_HOLD",
            strength=round(strength, 4),
            timestamp=current.timestamp,
            bar_count=2,
            notes=[f"探低后拉回 {rebound_ratio:.0%}"],
        )

    def _detect_rhythm_break(
        self, symbol: str, window: deque[MicroBarSnapshot]
    ) -> MicroSignal | None:
        """检测节奏断裂信号。

        条件：
        - 最近 N 根连续阴线（close < open）
        - 且成交量递减
        """
        if len(window) < RHYTHM_BREAK_MIN_BARS:
            return None

        recent = list(window)[-RHYTHM_BREAK_MIN_BARS:]

        # 检查是否全部阴线
        all_bearish = all(bar.close < bar.open for bar in recent)
        if not all_bearish:
            return None

        # 检查量能是否递减
        volumes = [bar.volume for bar in recent]
        volume_decreasing = all(
            volumes[i] >= volumes[i + 1]
            for i in range(len(volumes) - 1)
        )
        if not volume_decreasing:
            return None

        # 计算信号强度：基于累计跌幅
        total_drop = sum(
            (bar.open - bar.close) / bar.open if bar.open > 0 else 0
            for bar in recent
        )
        strength = min(total_drop / 0.03, 1.0)
        return MicroSignal(
            symbol=symbol,
            signal_type="RHYTHM_BREAK",
            strength=round(strength, 4),
            timestamp=recent[-1].timestamp,
            bar_count=RHYTHM_BREAK_MIN_BARS,
            notes=[f"连续 {RHYTHM_BREAK_MIN_BARS} 根阴线，量能递减"],
        )
