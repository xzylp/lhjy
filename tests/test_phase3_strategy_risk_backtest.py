"""Phase 3 策略 + 风控 + 回测测试"""

import numpy as np
import pandas as pd
import pytest

from ashare_system.contracts import BalanceSnapshot, MarketProfile, PositionSnapshot
from ashare_system.strategy.position_mgr import PositionManager, PositionInput
from ashare_system.strategy.screener import StockScreener
from ashare_system.strategy.buy_decision import BuyDecisionEngine
from ashare_system.strategy.sell_decision import SellDecisionEngine, PositionState, SellReason
from ashare_system.risk.rules import RiskRules, RuleResult
from ashare_system.risk.guard import ExecutionGuard
from ashare_system.risk.emotion_shield import EmotionShield
from ashare_system.backtest.metrics import MetricsCalculator, BacktestMetrics
from ashare_system.backtest.slippage import CostModel
from ashare_system.backtest.curve import CurveBuilder
from ashare_system.contracts import PlaceOrderRequest


class TestPositionManager:
    def test_kelly_basic(self):
        pm = PositionManager()
        inp = PositionInput(symbol="600519.SH", win_rate=0.6, profit_loss_ratio=2.0, atr=30.0, price=1600.0, account_equity=1_000_000)
        plan = pm.calc(inp)
        assert plan.symbol == "600519.SH"
        assert 0 <= plan.final_ratio <= 0.25

    def test_kelly_with_emotion(self):
        pm = PositionManager()
        inp = PositionInput(symbol="000001.SZ", win_rate=0.55, profit_loss_ratio=1.5, atr=0.3, price=10.0, account_equity=500_000)
        profile_ice = MarketProfile(sentiment_phase="冰点", position_ceiling=0.20)
        profile_bull = MarketProfile(sentiment_phase="主升", position_ceiling=0.80)
        plan_ice = pm.calc(inp, profile_ice)
        plan_bull = pm.calc(inp, profile_bull)
        assert plan_ice.final_ratio <= plan_bull.final_ratio

    def test_zero_win_rate(self):
        pm = PositionManager()
        inp = PositionInput(symbol="test", win_rate=0.0, profit_loss_ratio=2.0, atr=1.0, price=10.0, account_equity=100_000)
        plan = pm.calc(inp)
        assert plan.target_shares == 0

    def test_max_position_cap(self):
        pm = PositionManager()
        inp = PositionInput(symbol="test", win_rate=0.9, profit_loss_ratio=10.0, atr=0.1, price=10.0, account_equity=1_000_000)
        plan = pm.calc(inp)
        assert plan.final_ratio <= 0.25


class TestScreener:
    def test_basic_screen(self):
        screener = StockScreener()
        candidates = ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH"]
        result = screener.run(candidates)
        assert isinstance(result.passed, list)
        assert len(result.passed) <= len(candidates)

    def test_factor_filter(self):
        screener = StockScreener()
        candidates = ["A", "B", "C"]
        scores = {"A": 0.8, "B": 0.3, "C": 0.7}
        result = screener.run(candidates, factor_scores=scores, min_factor_score=0.5)
        assert "B" not in result.passed
        assert "A" in result.passed

    def test_ice_phase_blocks_all(self):
        screener = StockScreener()
        profile = MarketProfile(sentiment_phase="冰点", sentiment_score=5.0, position_ceiling=0.20)
        result = screener.run(["A", "B", "C"], profile=profile)
        assert result.passed == []

    def test_top_n_limit(self):
        screener = StockScreener()
        candidates = [f"stock_{i}" for i in range(20)]
        result = screener.run(candidates, top_n=5)
        assert len(result.passed) <= 5


class TestSellDecision:
    def test_initial_stop_loss(self):
        engine = SellDecisionEngine()
        state = PositionState(symbol="600519.SH", entry_price=100.0, atr=2.0, holding_days=1, current_price=95.0)
        signal = engine.evaluate(state)
        assert signal is not None
        assert signal.reason == SellReason.INITIAL_STOP

    def test_take_profit_1(self):
        engine = SellDecisionEngine()
        state = PositionState(symbol="600519.SH", entry_price=100.0, atr=2.0, holding_days=1, current_price=107.0)
        signal = engine.evaluate(state)
        assert signal is not None
        assert signal.reason == SellReason.TAKE_PROFIT_1
        assert signal.sell_ratio == 0.5

    def test_time_stop(self):
        engine = SellDecisionEngine()
        state = PositionState(symbol="test", entry_price=100.0, atr=2.0, holding_days=4, current_price=99.0)
        signal = engine.evaluate(state)
        assert signal is not None
        assert signal.reason == SellReason.TIME_STOP

    def test_no_signal_in_normal_range(self):
        engine = SellDecisionEngine()
        state = PositionState(symbol="test", entry_price=100.0, atr=2.0, holding_days=1, current_price=101.0)
        signal = engine.evaluate(state)
        assert signal is None

    def test_trailing_stop_update(self):
        engine = SellDecisionEngine()
        state = PositionState(symbol="test", entry_price=100.0, atr=2.0, holding_days=2, current_price=110.0, trailing_stop=0.0)
        new_stop = engine.update_trailing_stop(state)
        assert new_stop > 0
        assert new_stop < 110.0


class TestRiskRules:
    def test_single_position_limit(self):
        rules = RiskRules()
        bal = BalanceSnapshot(account_id="test", total_asset=1_000_000, cash=500_000)
        check = rules.check_single_position(300_000, 1_000_000)
        assert check.result == RuleResult.LIMIT

    def test_single_position_pass(self):
        rules = RiskRules()
        check = rules.check_single_position(100_000, 1_000_000)
        assert check.result == RuleResult.PASS

    def test_consecutive_loss_reject(self):
        rules = RiskRules()
        check = rules.check_consecutive_loss(3)
        assert check.result == RuleResult.REJECT

    def test_emotion_shield_limit(self):
        rules = RiskRules()
        profile = MarketProfile(sentiment_phase="冰点", position_ceiling=0.20)
        check = rules.check_emotion_shield(300_000, 1_000_000, profile)
        assert check.result == RuleResult.LIMIT
        assert check.adjusted_value == 200_000

    def test_stop_loss_reject(self):
        rules = RiskRules()
        pos = PositionSnapshot(account_id="test", symbol="test", quantity=100, available=100, cost_price=100.0, last_price=93.0)
        check = rules.check_stop_loss(pos)
        assert check.result == RuleResult.REJECT


class TestExecutionGuard:
    def test_approve_buy(self):
        guard = ExecutionGuard()
        req = PlaceOrderRequest(account_id="sim-001", symbol="600519.SH", side="BUY", quantity=100, price=10.0, request_id="req-001")
        bal = BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=500_000)
        approved, reason, _ = guard.approve(req, bal)
        assert approved

    def test_reject_consecutive_loss(self):
        guard = ExecutionGuard()
        req = PlaceOrderRequest(account_id="sim-001", symbol="test", side="BUY", quantity=100, price=10.0, request_id="req-002")
        bal = BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=500_000)
        approved, reason, _ = guard.approve(req, bal, consecutive_losses=3)
        assert not approved

    def test_sell_always_approved(self):
        guard = ExecutionGuard()
        req = PlaceOrderRequest(account_id="sim-001", symbol="test", side="SELL", quantity=100, price=10.0, request_id="req-003")
        bal = BalanceSnapshot(account_id="sim-001", total_asset=100, cash=0)
        approved, _, _ = guard.approve(req, bal, consecutive_losses=10)
        assert approved


class TestEmotionShield:
    def test_phase_ceilings(self):
        shield = EmotionShield()
        assert shield.get_position_ceiling(MarketProfile(sentiment_phase="冰点")) == 0.20
        assert shield.get_position_ceiling(MarketProfile(sentiment_phase="回暖")) == 0.60
        assert shield.get_position_ceiling(MarketProfile(sentiment_phase="主升")) == 0.80
        assert shield.get_position_ceiling(MarketProfile(sentiment_phase="高潮")) == 0.30

    def test_chase_permissions(self):
        shield = EmotionShield()
        assert not shield.can_chase_high(MarketProfile(sentiment_phase="冰点"))
        assert shield.can_chase_high(MarketProfile(sentiment_phase="主升"))

    def test_board_permissions(self):
        shield = EmotionShield()
        assert not shield.can_buy_limit_up(MarketProfile(sentiment_phase="高潮"))
        assert shield.can_buy_limit_up(MarketProfile(sentiment_phase="主升"))


class TestBacktestMetrics:
    def test_basic_metrics(self):
        calc = MetricsCalculator()
        equity = pd.Series([1_000_000, 1_010_000, 1_005_000, 1_020_000, 1_015_000])
        trades = [{"pnl": 10000}, {"pnl": -5000}, {"pnl": 15000}]
        m = calc.calc(equity, trades)
        assert isinstance(m.total_return, float)
        assert isinstance(m.sharpe_ratio, float)
        assert m.max_drawdown <= 0
        assert m.win_rate == pytest.approx(2 / 3, abs=0.01)

    def test_empty_equity(self):
        calc = MetricsCalculator()
        m = calc.calc(pd.Series([], dtype=float), [])
        assert m.total_return == 0.0

    def test_drawdown_calculation(self):
        calc = MetricsCalculator()
        equity = pd.Series([100, 110, 90, 95, 105])
        m = calc.calc(equity, [])
        assert m.max_drawdown < 0


class TestCostModel:
    def test_buy_cost(self):
        model = CostModel()
        cost = model.calc(10.0, 1000, "BUY")
        assert cost.stamp_duty == 0.0
        assert cost.commission >= 5.0
        assert cost.total > 0

    def test_sell_cost(self):
        model = CostModel()
        cost = model.calc(10.0, 1000, "SELL")
        assert cost.stamp_duty > 0

    def test_effective_price(self):
        model = CostModel()
        buy_price = model.effective_price(100.0, "BUY")
        sell_price = model.effective_price(100.0, "SELL")
        assert buy_price > 100.0
        assert sell_price < 100.0


class TestCurveBuilder:
    def test_build_from_trades(self):
        builder = CurveBuilder()
        trades = [
            {"date": "2026-01-01", "pnl": 1000},
            {"date": "2026-01-02", "pnl": -500},
            {"date": "2026-01-03", "pnl": 2000},
        ]
        curve = builder.build(1_000_000, trades)
        assert not curve.equity.empty
        assert len(curve.equity) == 3

    def test_normalized_curve(self):
        builder = CurveBuilder()
        equity = pd.Series([1_000_000, 1_100_000, 1_050_000])
        norm = builder.to_normalized(equity)
        assert norm.iloc[0] == pytest.approx(1.0)
