"""战法化退出引擎。"""

from __future__ import annotations

from datetime import datetime

from ..contracts import ExitContext, ExitSignal, MicroSignal, PositionSnapshot, QuoteSnapshot, SectorProfile


class ExitEngine:
    """优先用战法上下文判断退出，再回退到底层 ATR 卖出。"""

    def check(
        self,
        pos: PositionSnapshot,
        ctx: ExitContext,
        quote: QuoteSnapshot,
        sector: SectorProfile | None,
        micro_signal: MicroSignal | None = None,
    ) -> ExitSignal | None:
        open_failure_minutes = int(ctx.exit_params.get("open_failure_minutes", 5))
        if ctx.holding_minutes <= open_failure_minutes and ctx.relative_strength_5m < -0.02:
            return self._signal("entry_failure", pos, quote, "IMMEDIATE", 1.0, pos.cost_price)

        # v1.0: 微观节奏信号 — PEAK_FADE 快速退出
        if micro_signal is not None and micro_signal.signal_type == "PEAK_FADE" and ctx.holding_minutes < 60:
            return self._signal("entry_failure", pos, quote, "IMMEDIATE", 1.0, pos.cost_price)

        # v1.0: 微观节奏信号 — RHYTHM_BREAK 时间止损
        if micro_signal is not None and micro_signal.signal_type == "RHYTHM_BREAK" and ctx.holding_minutes < 120:
            return self._signal("time_stop", pos, quote, "HIGH", 1.0, pos.cost_price)

        # v1.0: 微观节奏信号 — VALLEY_HOLD 波谷企稳，抑制退出
        if micro_signal is not None and micro_signal.signal_type == "VALLEY_HOLD":
            if ctx.intraday_drawdown_pct >= 0.025 and ctx.rebound_from_low_pct >= 0.015:
                return None  # 假跌破回拉是加分信号，不退出

        if ctx.is_bomb:
            return self._signal("board_break", pos, quote, "IMMEDIATE", 1.0, pos.cost_price)

        if ctx.sector_retreat or (sector is not None and sector.life_cycle == "retreat"):
            return self._signal("sector_retreat", pos, quote, "HIGH", 1.0, pos.cost_price)

        if quote.last_price >= pos.cost_price * 1.03 and not ctx.is_limit_up and ctx.relative_strength_5m < 0:
            return self._signal("no_seal_on_surge", pos, quote, "HIGH", 0.5, quote.last_price)

        optimal_hold_days = max(int(ctx.optimal_hold_days or 1), 1)
        leader_like = (
            ctx.style_tag == "leader"
            or ctx.leader_frequency_30d >= 0.35
            or ctx.avg_sector_rank_30d <= 3.0
        )
        post_entry_grace_minutes = self._int_param(ctx.exit_params, "post_entry_grace_minutes", 8)
        if self._should_exit_on_sector_sync_weakness(
            ctx,
            leader_like=leader_like,
            post_entry_grace_minutes=post_entry_grace_minutes,
        ):
            urgency = "IMMEDIATE" if ctx.holding_minutes <= 90 else "HIGH"
            return self._signal("time_stop", pos, quote, urgency, 1.0, pos.cost_price)

        if self._should_exit_on_failed_micro_rebound(
            ctx,
            leader_like=leader_like,
            post_entry_grace_minutes=post_entry_grace_minutes,
        ):
            urgency = "IMMEDIATE" if ctx.holding_minutes <= 60 else "HIGH"
            return self._signal("time_stop", pos, quote, urgency, 1.0, pos.cost_price)

        if self._should_exit_on_rapid_distortion(
            ctx,
            leader_like=leader_like,
            post_entry_grace_minutes=post_entry_grace_minutes,
        ):
            return self._signal("time_stop", pos, quote, "IMMEDIATE", 1.0, pos.cost_price)

        leader_hold_reached = ctx.holding_days >= optimal_hold_days or (
            optimal_hold_days <= 1 and ctx.holding_minutes >= 60
        )
        if (
            leader_like
            and leader_hold_reached
            and not ctx.is_limit_up
            and ctx.relative_strength_5m <= -0.005
        ):
            return self._signal("time_stop", pos, quote, "HIGH", 1.0, pos.cost_price)

        if (
            leader_like
            and ctx.holding_minutes >= 30
            and not ctx.is_limit_up
            and ctx.sector_relative_strength_5m <= -0.015
        ):
            return self._signal("time_stop", pos, quote, "HIGH", 1.0, pos.cost_price)

        if (
            leader_like
            and ctx.holding_minutes >= 30
            and not ctx.is_limit_up
            and ctx.sector_underperform_bars_5m >= 2
            and ctx.sector_relative_trend_5m <= -0.01
        ):
            return self._signal("time_stop", pos, quote, "HIGH", 1.0, pos.cost_price)

        if (
            leader_like
            and ctx.holding_minutes >= 30
            and ctx.intraday_drawdown_pct >= 0.025
            and ctx.rebound_from_low_pct <= 0.01
        ):
            return self._signal("time_stop", pos, quote, "HIGH", 1.0, pos.cost_price)

        if (
            ctx.negative_alert_count > 0
            and ctx.holding_minutes >= 15
            and ctx.intraday_change_pct <= 0
        ):
            return self._signal("time_stop", pos, quote, "HIGH", 1.0, pos.cost_price)

        if (
            ctx.style_tag in {"defensive", "mixed"}
            and ctx.holding_days >= max(optimal_hold_days, 2)
            and ctx.relative_strength_5m <= -0.02
        ):
            return self._signal("time_stop", pos, quote, "NORMAL", 1.0, pos.cost_price)

        max_hold_minutes = int(ctx.exit_params.get("max_hold_minutes", 240))
        time_stop = str(ctx.exit_params.get("time_stop", "14:50"))
        if ctx.holding_minutes >= max_hold_minutes or self._past_time_stop(ctx.entry_time, time_stop):
            return self._signal("time_stop", pos, quote, "NORMAL", 1.0, pos.cost_price)

        return None

    @staticmethod
    def _signal(
        reason: str,
        pos: PositionSnapshot,
        quote: QuoteSnapshot,
        urgency: str,
        sell_ratio: float,
        reference_price: float,
    ) -> ExitSignal:
        return ExitSignal(
            symbol=pos.symbol,
            reason=reason,
            sell_ratio=sell_ratio,
            urgency=urgency,
            current_price=quote.last_price,
            reference_price=reference_price,
        )

    @staticmethod
    def _past_time_stop(entry_time: str, time_stop: str) -> bool:
        try:
            entry_dt = datetime.strptime(entry_time, "%H:%M")
            stop_dt = datetime.strptime(time_stop, "%H:%M")
        except ValueError:
            return False
        return entry_dt >= stop_dt

    def _should_exit_on_sector_sync_weakness(
        self,
        ctx: ExitContext,
        *,
        leader_like: bool,
        post_entry_grace_minutes: int,
    ) -> bool:
        if not leader_like or ctx.is_limit_up:
            return False
        min_hold_minutes = max(
            post_entry_grace_minutes,
            self._int_param(ctx.exit_params, "sector_sync_min_hold_minutes", 20),
        )
        max_hold_minutes = max(
            min_hold_minutes,
            self._int_param(ctx.exit_params, "sector_sync_window_minutes", 180),
        )
        if ctx.holding_minutes < min_hold_minutes or ctx.holding_minutes > max_hold_minutes:
            return False
        sector_intraday_threshold = self._float_param(
            ctx.exit_params,
            "sector_sync_intraday_change_threshold",
            -0.008,
        )
        stock_intraday_threshold = self._float_param(
            ctx.exit_params,
            "sector_sync_stock_change_threshold",
            -0.004,
        )
        trend_5m_threshold = self._float_param(
            ctx.exit_params,
            "sector_sync_relative_trend_5m_threshold",
            -0.004,
        )
        trend_1m_threshold = self._float_param(
            ctx.exit_params,
            "sector_sync_relative_trend_1m_threshold",
            -0.006,
        )
        micro_return_threshold = self._float_param(
            ctx.exit_params,
            "sector_sync_micro_1m_return_threshold",
            -0.008,
        )
        drawdown_threshold = self._float_param(
            ctx.exit_params,
            "sector_sync_micro_1m_drawdown_threshold",
            0.012,
        )
        micro_1m_return_sum = self._float_param(ctx.exit_params, "micro_1m_return_3_sum", 0.0)
        micro_1m_drawdown = self._float_param(ctx.exit_params, "micro_1m_drawdown_pct", 0.0)
        sector_relative_trend_1m = self._float_param(ctx.exit_params, "sector_relative_trend_1m", 0.0)
        return (
            ctx.sector_intraday_change_pct <= sector_intraday_threshold
            and ctx.intraday_change_pct <= stock_intraday_threshold
            and (
                ctx.sector_relative_trend_5m <= trend_5m_threshold
                or sector_relative_trend_1m <= trend_1m_threshold
            )
            and micro_1m_return_sum <= micro_return_threshold
            and micro_1m_drawdown >= drawdown_threshold
        )

    def _should_exit_on_rapid_distortion(
        self,
        ctx: ExitContext,
        *,
        leader_like: bool,
        post_entry_grace_minutes: int,
    ) -> bool:
        playbook_sensitive = ctx.playbook in {"leader_chase", "divergence_reseal"}
        if (not leader_like and not playbook_sensitive) or ctx.is_limit_up:
            return False
        min_hold_minutes = max(
            post_entry_grace_minutes,
            self._int_param(ctx.exit_params, "rapid_distortion_min_hold_minutes", 8),
        )
        max_hold_minutes = max(
            min_hold_minutes,
            self._int_param(ctx.exit_params, "rapid_distortion_window_minutes", 90),
        )
        if ctx.holding_minutes < min_hold_minutes or ctx.holding_minutes > max_hold_minutes:
            return False
        micro_1m_return_threshold = self._float_param(
            ctx.exit_params,
            "rapid_distortion_1m_return_threshold",
            -0.01,
        )
        micro_1m_drawdown_threshold = self._float_param(
            ctx.exit_params,
            "rapid_distortion_1m_drawdown_threshold",
            0.012,
        )
        micro_5m_return_threshold = self._float_param(
            ctx.exit_params,
            "rapid_distortion_5m_return_threshold",
            -0.008,
        )
        relative_strength_threshold = self._float_param(
            ctx.exit_params,
            "rapid_distortion_relative_strength_threshold",
            -0.005,
        )
        intraday_drawdown_threshold = self._float_param(
            ctx.exit_params,
            "rapid_distortion_intraday_drawdown_threshold",
            0.018,
        )
        micro_1m_negative_bars = self._int_param(ctx.exit_params, "micro_1m_negative_bars", 0)
        return (
            self._float_param(ctx.exit_params, "micro_1m_return_3_sum", 0.0) <= micro_1m_return_threshold
            and self._float_param(ctx.exit_params, "micro_1m_drawdown_pct", 0.0) >= micro_1m_drawdown_threshold
            and micro_1m_negative_bars >= 2
            and (
                self._float_param(ctx.exit_params, "micro_5m_return_2_sum", 0.0) <= micro_5m_return_threshold
                or ctx.relative_strength_5m <= relative_strength_threshold
                or ctx.intraday_drawdown_pct >= intraday_drawdown_threshold
            )
        )

    def _should_exit_on_failed_micro_rebound(
        self,
        ctx: ExitContext,
        *,
        leader_like: bool,
        post_entry_grace_minutes: int,
    ) -> bool:
        playbook_sensitive = ctx.playbook in {"leader_chase", "divergence_reseal", "sector_reflow"}
        if (not leader_like and not playbook_sensitive) or ctx.is_limit_up:
            return False
        min_hold_minutes = max(
            post_entry_grace_minutes,
            self._int_param(ctx.exit_params, "micro_rebound_min_hold_minutes", 12),
        )
        max_hold_minutes = max(
            min_hold_minutes,
            self._int_param(ctx.exit_params, "micro_rebound_window_minutes", 120),
        )
        if ctx.holding_minutes < min_hold_minutes or ctx.holding_minutes > max_hold_minutes:
            return False
        rebound_threshold = self._float_param(
            ctx.exit_params,
            "micro_rebound_from_low_pct_threshold",
            0.004,
        )
        latest_return_threshold = self._float_param(
            ctx.exit_params,
            "micro_rebound_latest_return_threshold",
            -0.003,
        )
        drawdown_threshold = self._float_param(
            ctx.exit_params,
            "micro_rebound_drawdown_threshold",
            0.01,
        )
        sector_trend_threshold = self._float_param(
            ctx.exit_params,
            "micro_rebound_sector_relative_trend_1m_threshold",
            -0.004,
        )
        relative_strength_threshold = self._float_param(
            ctx.exit_params,
            "micro_rebound_relative_strength_threshold",
            -0.004,
        )
        rebound_from_low_pct = self._float_param(ctx.exit_params, "micro_1m_rebound_from_low_pct", 0.0)
        latest_return_pct = self._float_param(ctx.exit_params, "micro_1m_latest_return_pct", 0.0)
        drawdown_pct = self._float_param(ctx.exit_params, "micro_1m_drawdown_pct", 0.0)
        sector_relative_trend_1m = self._float_param(ctx.exit_params, "sector_relative_trend_1m", 0.0)
        return (
            drawdown_pct >= drawdown_threshold
            and rebound_from_low_pct <= rebound_threshold
            and latest_return_pct <= latest_return_threshold
            and (
                ctx.negative_alert_count > 0
                or sector_relative_trend_1m <= sector_trend_threshold
                or ctx.relative_strength_5m <= relative_strength_threshold
            )
        )

    @staticmethod
    def _float_param(params: dict, key: str, default: float) -> float:
        try:
            return float(params.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int_param(params: dict, key: str, default: int) -> int:
        try:
            return int(params.get(key, default))
        except (TypeError, ValueError):
            return default
