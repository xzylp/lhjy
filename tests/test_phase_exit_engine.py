from ashare_system.contracts import ExitContext, PositionSnapshot, QuoteSnapshot, SectorProfile
from ashare_system.strategy.exit_engine import ExitEngine
from ashare_system.strategy.sell_decision import PositionState, SellDecisionEngine, SellReason


class TestExitEngine:
    def test_entry_failure_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=9.7,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=3,
                relative_strength_5m=-0.03,
                exit_params={"open_failure_minutes": 5},
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=9.7,
                bid_price=9.69,
                ask_price=9.71,
                volume=1_000_000,
            ),
            sector=None,
        )
        assert signal is not None
        assert signal.reason == "entry_failure"
        assert signal.urgency == "IMMEDIATE"

    def test_sector_retreat_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(account_id="sim", symbol="300001.SZ", quantity=100, available=100, cost_price=10.0, last_price=10.2),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="divergence_reseal",
                entry_price=10.0,
                entry_time="10:10",
                holding_minutes=45,
            ),
            quote=QuoteSnapshot(symbol="300001.SZ", last_price=10.2, bid_price=10.19, ask_price=10.21, volume=800_000),
            sector=SectorProfile(sector_name="AI", life_cycle="retreat", strength_score=0.2),
        )
        assert signal is not None
        assert signal.reason == "sector_retreat"

    def test_behavior_profile_accelerates_leader_time_stop(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.01,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=80,
                holding_days=1,
                relative_strength_5m=-0.008,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.62,
                avg_sector_rank_30d=1.8,
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=10.01,
                bid_price=10.0,
                ask_price=10.02,
                volume=900_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.7),
        )
        assert signal is not None
        assert signal.reason == "time_stop"
        assert signal.urgency == "HIGH"

    def test_defensive_profile_does_not_trigger_same_early_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.01,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=80,
                holding_days=1,
                relative_strength_5m=-0.008,
                optimal_hold_days=3,
                style_tag="defensive",
                leader_frequency_30d=0.05,
                avg_sector_rank_30d=8.0,
                exit_params={"max_hold_minutes": 240},
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=10.01,
                bid_price=10.0,
                ask_price=10.02,
                volume=900_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.7),
        )
        assert signal is None

    def test_leader_intraday_fade_without_rebound_triggers_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.18,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=90,
                holding_days=0,
                relative_strength_5m=0.004,
                intraday_change_pct=0.018,
                intraday_drawdown_pct=0.031,
                rebound_from_low_pct=0.006,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.6,
                avg_sector_rank_30d=1.0,
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=10.18,
                bid_price=10.17,
                ask_price=10.19,
                volume=1_200_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.8),
        )
        assert signal is not None
        assert signal.reason == "time_stop"
        assert signal.urgency == "HIGH"

    def test_leader_sector_relative_weakness_triggers_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.03,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=55,
                holding_days=0,
                relative_strength_5m=0.003,
                intraday_change_pct=0.006,
                sector_intraday_change_pct=0.028,
                sector_relative_strength_5m=-0.022,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.58,
                avg_sector_rank_30d=1.5,
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=10.03,
                bid_price=10.02,
                ask_price=10.04,
                volume=1_000_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.9),
        )
        assert signal is not None
        assert signal.reason == "time_stop"
        assert signal.urgency == "HIGH"

    def test_leader_sector_relative_trend_weakness_triggers_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.10,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=45,
                holding_days=0,
                relative_strength_5m=0.01,
                intraday_change_pct=0.01,
                sector_intraday_change_pct=0.022,
                sector_relative_strength_5m=-0.012,
                sector_relative_trend_5m=-0.013,
                sector_underperform_bars_5m=3,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.6,
                avg_sector_rank_30d=1.2,
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=10.10,
                bid_price=10.09,
                ask_price=10.11,
                volume=1_050_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.9),
        )
        assert signal is not None
        assert signal.reason == "time_stop"
        assert signal.urgency == "HIGH"

    def test_leader_sector_sync_weakness_triggers_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=9.94,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=28,
                holding_days=0,
                relative_strength_5m=-0.004,
                intraday_change_pct=-0.007,
                sector_intraday_change_pct=-0.011,
                sector_relative_strength_5m=-0.002,
                sector_relative_trend_5m=-0.005,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.52,
                avg_sector_rank_30d=1.6,
                exit_params={
                    "micro_1m_return_3_sum": -0.009,
                    "micro_1m_drawdown_pct": 0.013,
                    "sector_relative_trend_1m": -0.007,
                },
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=9.94,
                bid_price=9.93,
                ask_price=9.95,
                volume=950_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.6),
        )
        assert signal is not None
        assert signal.reason == "time_stop"
        assert signal.urgency == "IMMEDIATE"

    def test_rapid_distortion_respects_post_entry_grace_window(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.02,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=6,
                holding_days=0,
                relative_strength_5m=0.002,
                intraday_change_pct=0.002,
                intraday_drawdown_pct=0.019,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.6,
                avg_sector_rank_30d=1.2,
                exit_params={
                    "post_entry_grace_minutes": 8,
                    "micro_1m_return_3_sum": -0.014,
                    "micro_1m_drawdown_pct": 0.016,
                    "micro_1m_negative_bars": 3,
                    "micro_5m_return_2_sum": -0.011,
                },
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=10.02,
                bid_price=10.01,
                ask_price=10.03,
                volume=1_100_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.8),
        )
        assert signal is None

    def test_failed_micro_rebound_triggers_exit(self):
        engine = ExitEngine()
        signal = engine.check(
            pos=PositionSnapshot(
                account_id="sim",
                symbol="300001.SZ",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=9.98,
            ),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=22,
                holding_days=0,
                relative_strength_5m=-0.006,
                negative_alert_count=1,
                optimal_hold_days=1,
                style_tag="leader",
                leader_frequency_30d=0.55,
                avg_sector_rank_30d=1.4,
                exit_params={
                    "micro_1m_drawdown_pct": 0.013,
                    "micro_1m_rebound_from_low_pct": 0.003,
                    "micro_1m_latest_return_pct": -0.004,
                    "sector_relative_trend_1m": -0.002,
                },
            ),
            quote=QuoteSnapshot(
                symbol="300001.SZ",
                last_price=9.98,
                bid_price=9.97,
                ask_price=9.99,
                volume=980_000,
            ),
            sector=SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.7),
        )
        assert signal is not None
        assert signal.reason == "time_stop"
        assert signal.urgency == "IMMEDIATE"


class TestSellDecisionWithExitContext:
    def test_sell_decision_uses_exit_engine_before_fallback(self):
        engine = SellDecisionEngine()
        signal = engine.evaluate_with_context(
            state=PositionState(symbol="300001.SZ", entry_price=10.0, atr=0.2, holding_days=1, current_price=9.7),
            position=PositionSnapshot(account_id="sim", symbol="300001.SZ", quantity=100, available=100, cost_price=10.0, last_price=9.7),
            ctx=ExitContext(
                symbol="300001.SZ",
                playbook="leader_chase",
                entry_price=10.0,
                entry_time="09:35",
                holding_minutes=2,
                relative_strength_5m=-0.04,
                exit_params={"open_failure_minutes": 5},
            ),
            quote=QuoteSnapshot(symbol="300001.SZ", last_price=9.7, bid_price=9.69, ask_price=9.71, volume=500_000),
        )
        assert signal is not None
        assert signal.reason == SellReason.ENTRY_FAILURE

    def test_sell_decision_falls_back_to_atr_logic(self):
        engine = SellDecisionEngine()
        signal = engine.evaluate_with_context(
            state=PositionState(symbol="600519.SH", entry_price=100.0, atr=2.0, holding_days=1, current_price=95.0),
            position=PositionSnapshot(account_id="sim", symbol="600519.SH", quantity=100, available=100, cost_price=100.0, last_price=95.0),
            ctx=ExitContext(
                symbol="600519.SH",
                playbook="leader_chase",
                entry_price=100.0,
                entry_time="09:40",
                holding_minutes=20,
                relative_strength_5m=0.01,
            ),
            quote=QuoteSnapshot(symbol="600519.SH", last_price=95.0, bid_price=94.9, ask_price=95.1, volume=200_000),
        )
        assert signal is not None
        assert signal.reason == SellReason.INITIAL_STOP
