"""回测引擎核心 — 事件驱动，T+1执行消除前视偏差"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from .slippage import CostModel
from .metrics import BacktestMetrics, MetricsCalculator
from ..execution.quality_tracker import ExecutionQualityTracker
from ..logging_config import get_logger

logger = get_logger("backtest.engine")


@dataclass
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.001
    min_commission: float = 5.0
    position_pct: float = 0.10  # 默认单票仓位比例 (无 PositionManager 时)
    max_positions: int = 5
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.12
    trailing_stop_pct: float = 0.06
    max_holding_days: int = 10
    rebalance_mode: str = "volatility_scaled"
    execution_quality_report_path: str | None = None
    slippage_lookback_days: int = 20
    benchmark_curve: pd.Series | None = None


@dataclass
class BacktestPosition:
    symbol: str
    quantity: int = 0
    cost_price: float = 0.0
    entry_date: str = ""
    highest_price: float = 0.0


@dataclass
class BacktestResult:
    metrics: BacktestMetrics
    equity_curve: pd.Series
    trades: list[dict] = field(default_factory=list)
    positions: dict[str, BacktestPosition] = field(default_factory=dict)


class BacktestEngine:
    """事件驱动回测引擎 — 信号日T生成，日T+1开盘执行"""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()
        self.calc = MetricsCalculator()
        self.cost_model = CostModel(slippage_rate=self._resolve_slippage_rate())

    def _resolve_slippage_rate(self) -> float:
        configured = float(self.config.slippage_rate or 0.0)
        report_path = str(self.config.execution_quality_report_path or "").strip()
        if not report_path:
            return configured
        try:
            tracker = ExecutionQualityTracker(Path(report_path))
            avg_bps = tracker.recent_avg_slippage_bps(self.config.slippage_lookback_days)
        except Exception:
            avg_bps = None
        if avg_bps is None:
            return configured
        dynamic_rate = max(float(avg_bps) / 10000.0, 0.0)
        if abs(dynamic_rate - configured) > 0.0005:
            logger.warning(
                "backtest_assumption_drift: slippage_rate %.4f%% -> %.4f%%",
                configured * 100,
                dynamic_rate * 100,
            )
        return dynamic_rate

    def run(self, signals: pd.DataFrame, price_data: dict[str, pd.DataFrame]) -> BacktestResult:
        """
        signals: DataFrame, index=date, columns=symbol, values=action (BUY/SELL/HOLD)
        price_data: {symbol: OHLCV DataFrame with 'open','close' columns}
        信号在日T收盘后生成，在日T+1开盘执行（消除前视偏差）
        """
        cash = self.config.initial_cash
        positions: dict[str, BacktestPosition] = {}
        equity_log: dict[str, float] = {}
        trades: list[dict] = []

        # 统一 price_data 的 index 为 YYYY-MM-DD 格式
        normalized_price: dict[str, pd.DataFrame] = {}
        for sym, df in price_data.items():
            idx = pd.to_datetime(df.index, errors="coerce")
            df2 = df.copy()
            df2.index = idx.strftime("%Y-%m-%d")
            normalized_price[sym] = df2

        dates = list(signals.index)
        pending_signals: dict[str, str] = {}  # 上一日信号，待今日执行

        for i, date in enumerate(dates):
            date_str = pd.Timestamp(date).strftime("%Y-%m-%d")

            # ── 先用当日区间检查止损/止盈/跟踪止盈 ──
            risk_exit_symbols: dict[str, tuple[str, float]] = {}
            for symbol, pos in list(positions.items()):
                df = normalized_price.get(symbol)
                if df is None or date_str not in df.index:
                    continue
                row = df.loc[date_str]
                high_price = float(row["high"] if "high" in df.columns else row["close"])
                low_price = float(row["low"] if "low" in df.columns else row["close"])
                close_price = float(row["close"])
                pos.highest_price = max(float(pos.highest_price or pos.cost_price or close_price), high_price)
                stop_loss_price = pos.cost_price * (1 - self.config.stop_loss_pct)
                take_profit_price = pos.cost_price * (1 + self.config.take_profit_pct)
                trailing_stop_price = pos.highest_price * (1 - self.config.trailing_stop_pct)
                holding_days = max((pd.Timestamp(date_str) - pd.Timestamp(pos.entry_date)).days, 0) if pos.entry_date else 0
                if low_price <= stop_loss_price:
                    risk_exit_symbols[symbol] = ("STOP_LOSS", stop_loss_price)
                elif high_price >= take_profit_price:
                    risk_exit_symbols[symbol] = ("TAKE_PROFIT", take_profit_price)
                elif close_price <= trailing_stop_price and pos.highest_price > pos.cost_price:
                    risk_exit_symbols[symbol] = ("TRAILING_STOP", trailing_stop_price)
                elif holding_days >= self.config.max_holding_days:
                    risk_exit_symbols[symbol] = ("MAX_HOLDING_DAYS", close_price)

            # ── 执行上一日的信号（用今日开盘价） ──
            executable_signals = dict(pending_signals)
            for symbol, (exit_reason, _exit_price) in risk_exit_symbols.items():
                executable_signals[symbol] = "SELL"
            for symbol, action in executable_signals.items():
                df = normalized_price.get(symbol)
                if df is None or date_str not in df.index:
                    continue
                # 用开盘价执行，若无 open 列则用 close
                exec_price = float(df.loc[date_str, "open"]) if "open" in df.columns else float(df.loc[date_str, "close"])
                if exec_price <= 0:
                    continue

                if action == "BUY" and symbol not in positions:
                    allocation = self._resolve_position_allocation(
                        cash=cash,
                        price_frame=df.loc[:date_str],
                        open_slots=max(self.config.max_positions - len(positions), 1),
                    )
                    qty = int(allocation / exec_price / 100) * 100
                    if qty >= 100:
                        cost = self.cost_model.calc(exec_price, qty, "BUY")
                        total = exec_price * qty + cost.total
                        if cash >= total:
                            cash -= total
                            positions[symbol] = BacktestPosition(
                                symbol=symbol,
                                quantity=qty,
                                cost_price=exec_price,
                                entry_date=date_str,
                                highest_price=exec_price,
                            )
                            trades.append({"date": date_str, "symbol": symbol, "side": "BUY", "price": exec_price, "qty": qty, "pnl": 0, "reason": "SIGNAL"})

                elif action == "SELL" and symbol in positions:
                    pos = positions.pop(symbol)
                    cost = self.cost_model.calc(exec_price, pos.quantity, "SELL")
                    proceeds = exec_price * pos.quantity - cost.total
                    pnl = proceeds - pos.cost_price * pos.quantity
                    cash += proceeds
                    exit_reason = risk_exit_symbols.get(symbol, ("SIGNAL", exec_price))[0]
                    trades.append(
                        {
                            "date": date_str,
                            "symbol": symbol,
                            "side": "SELL",
                            "price": exec_price,
                            "qty": pos.quantity,
                            "pnl": pnl,
                            "reason": exit_reason,
                        }
                    )

            # ── 收集今日信号（明日执行） ──
            pending_signals = {}
            for symbol in signals.columns:
                action = signals.loc[date, symbol]
                if action in ("BUY", "SELL"):
                    pending_signals[symbol] = action

            # ── 计算当日净值（用收盘价） ──
            pos_value = 0.0
            for s, p in positions.items():
                df = normalized_price.get(s)
                if df is not None and date_str in df.index:
                    pos_value += float(df.loc[date_str, "close"]) * p.quantity
            equity_log[date_str] = cash + pos_value

        equity_curve = pd.Series(equity_log)
        metrics = self.calc.calc(equity_curve, trades, benchmark_curve=self.config.benchmark_curve)
        logger.info("回测完成: 总收益=%.1f%%, 夏普=%.2f, 最大回撤=%.1f%%", metrics.total_return * 100, metrics.sharpe_ratio, metrics.max_drawdown * 100)
        return BacktestResult(metrics=metrics, equity_curve=equity_curve, trades=trades, positions=positions)

    def _resolve_position_allocation(self, *, cash: float, price_frame: pd.DataFrame, open_slots: int) -> float:
        base_allocation = cash * self.config.position_pct
        if self.config.rebalance_mode != "volatility_scaled" or price_frame.empty or len(price_frame) < 10:
            return min(base_allocation, cash / max(open_slots, 1))
        returns = price_frame["close"].pct_change().dropna().tail(20)
        realized_vol = float(returns.std() or 0.0)
        vol_scale = 1.0 / max(realized_vol * 100.0, 1.0)
        adjusted = base_allocation * min(max(vol_scale, 0.45), 1.35)
        return min(adjusted, cash / max(open_slots, 1))


def build_trade_records_from_backtest_result(
    result: BacktestResult,
    metadata_by_symbol: dict[str, dict] | None = None,
    *,
    record_factory: Callable[..., dict] | None = None,
) -> list[dict]:
    """把底层回测成交输出转换成离线 attribution 可消费的最小 trade records。

    注意：
    - 这里只做离线回测结果适配，不是线上成交事实归因。
    - 默认按 symbol 合并元数据，适合最小回测样本与文档导出场景。
    """

    metadata_map = metadata_by_symbol or {}
    entry_map: dict[str, dict] = {}
    records: list[dict] = []

    for trade in result.trades:
        symbol = str(trade.get("symbol") or "")
        side = str(trade.get("side") or "").upper()
        if not symbol or side not in {"BUY", "SELL"}:
            continue
        if side == "BUY":
            entry_map[symbol] = {
                "date": str(trade.get("date") or ""),
                "price": float(trade.get("price") or 0.0),
                "qty": int(trade.get("qty") or 0),
            }
            continue

        entry = entry_map.get(symbol, {})
        quantity = max(int(trade.get("qty") or entry.get("qty") or 0), 0)
        entry_price = float(entry.get("price") or 0.0)
        pnl = float(trade.get("pnl") or 0.0)
        base_value = entry_price * quantity
        return_pct = round(pnl / base_value, 6) if base_value > 0 else 0.0
        entry_date = str(entry.get("date") or "")
        exit_date = str(trade.get("date") or "")
        metadata = metadata_map.get(symbol, {})
        record = {
            "trade_id": f"{symbol}-{exit_date}",
            "symbol": symbol,
            "playbook": str(metadata.get("playbook") or "unassigned"),
            "regime": str(metadata.get("regime") or "unknown"),
            "exit_reason": str(metadata.get("exit_reason") or "unlabeled"),
            "return_pct": return_pct,
            "holding_days": _estimate_holding_days(entry_date, exit_date),
            "trade_date": exit_date,
            "note": str(metadata.get("note") or "from_backtest_engine"),
        }
        records.append(record_factory(**record) if record_factory is not None else record)
        entry_map.pop(symbol, None)
    return records


def _estimate_holding_days(entry_date: str, exit_date: str) -> int:
    if not entry_date or not exit_date:
        return 0
    try:
        return max((pd.Timestamp(exit_date) - pd.Timestamp(entry_date)).days, 0)
    except Exception:
        return 0
