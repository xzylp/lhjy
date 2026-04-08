"""第一批量化改造测试：contracts / regime / sector / router / buy decision。"""

from ashare_system.contracts import MarketProfile, PlaybookContext, SectorProfile
from ashare_system.risk.emotion_shield import EmotionShield
from ashare_system.sentiment.calculator import SentimentCalculator
from ashare_system.sentiment.indicators import SentimentIndicators
from ashare_system.sentiment.regime import classify_regime, enrich_market_profile
from ashare_system.sentiment.sector_cycle import SectorCycle, SectorData
from ashare_system.strategy.buy_decision import BuyDecisionEngine
from ashare_system.strategy.router import StrategyRouter


class TestQuantContracts:
    def test_market_profile_extended_defaults(self):
        profile = MarketProfile(sentiment_phase="回暖")
        assert profile.regime == "defensive"
        assert profile.allowed_playbooks == []
        assert profile.market_risk_flags == []
        assert profile.sector_profiles == []

    def test_playbook_context_and_sector_profile(self):
        sector = SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.8)
        ctx = PlaybookContext(
            playbook="leader_chase",
            symbol="300001.SZ",
            sector="AI",
            confidence=0.75,
            rank_in_sector=1,
        )
        assert sector.life_cycle == "ferment"
        assert ctx.playbook == "leader_chase"


class TestRegime:
    def test_classify_regime_trend(self):
        regime, playbooks, flags = classify_regime(
            zt_count=55,
            board_fail_rate=0.12,
            seal_rate=0.82,
            max_consecutive=6,
            up_down_ratio=2.4,
            prev_day_premium=0.03,
            theme_concentration=0.72,
        )
        assert regime == "trend"
        assert playbooks == ["leader_chase", "divergence_reseal"]
        assert flags == []

    def test_classify_regime_rotation(self):
        regime, playbooks, flags = classify_regime(
            zt_count=28,
            board_fail_rate=0.18,
            seal_rate=0.7,
            max_consecutive=3,
            up_down_ratio=1.4,
            prev_day_premium=0.01,
            theme_concentration=0.45,
        )
        assert regime == "rotation"
        assert "sector_reflow_first_board" in playbooks
        assert flags == []

    def test_enrich_market_profile(self):
        base = MarketProfile(sentiment_phase="主升", sentiment_score=72.0, position_ceiling=0.8)
        indicators = SentimentIndicators(
            date="2026-04-06",
            limit_up_count=48,
            board_fail_rate=0.15,
            max_consecutive_up=5,
            up_down_ratio=2.1,
        )
        enriched = enrich_market_profile(base, indicators, prev_day_premium=0.02, theme_concentration=0.7)
        assert enriched.regime == "trend"
        assert enriched.allowed_playbooks == ["leader_chase", "divergence_reseal"]
        assert enriched.regime_score > 0.8

    def test_sentiment_calculator_sets_regime(self):
        calc = SentimentCalculator()
        profile = calc.calc_daily(
            SentimentIndicators(
                date="2026-04-06",
                limit_up_count=10,
                board_fail_rate=0.1,
                up_down_ratio=0.4,
            )
        )
        assert profile.regime == "chaos"
        assert profile.allowed_playbooks == []


class TestSectorCycle:
    def test_build_profiles_sorted_by_strength(self):
        cycle = SectorCycle()
        profiles = cycle.build_profiles(
            {
                "AI": SectorData(
                    zt_count=4,
                    prev_zt_count=2,
                    up_count=8,
                    total_count=10,
                    active_days=2,
                    board_fail_rate=0.12,
                    top_symbols=["300001.SZ", "300002.SZ"],
                    avg_return_pct=4.0,
                    prev_avg_return_pct=1.5,
                ),
                "消费": SectorData(
                    zt_count=1,
                    prev_zt_count=2,
                    up_count=4,
                    total_count=10,
                    active_days=1,
                    board_fail_rate=0.35,
                    top_symbols=["600001.SH"],
                    avg_return_pct=-1.0,
                    prev_avg_return_pct=2.0,
                ),
            }
        )
        assert profiles[0].sector_name == "AI"
        assert profiles[0].life_cycle == "ferment"
        assert profiles[1].life_cycle == "retreat"


class TestEmotionShield:
    def test_get_allowed_playbooks(self):
        shield = EmotionShield()
        ice = MarketProfile(sentiment_phase="冰点", allowed_playbooks=["leader_chase"], regime="trend")
        climax = MarketProfile(
            sentiment_phase="高潮",
            allowed_playbooks=["leader_chase", "divergence_reseal"],
            regime="trend",
        )
        assert shield.get_allowed_playbooks(ice) == []
        assert shield.get_allowed_playbooks(climax) == ["divergence_reseal"]


class TestRouterAndBuyDecision:
    def test_router_assigns_playbook(self):
        router = StrategyRouter()
        profile = MarketProfile(
            sentiment_phase="主升",
            regime="trend",
            regime_score=0.9,
            allowed_playbooks=["leader_chase", "divergence_reseal"],
        )
        sectors = [
            SectorProfile(
                sector_name="AI",
                life_cycle="ferment",
                strength_score=0.82,
                leader_symbols=["300001.SZ", "300002.SZ"],
            )
        ]
        results = router.route(
            profile=profile,
            sector_profiles=sectors,
            candidates=["300001.SZ", "300003.SZ"],
            stock_info={
                "300001.SZ": {"sector": "AI"},
                "300003.SZ": {"sector": "AI"},
            },
        )
        assert len(results) == 2
        assert results[0].playbook == "leader_chase"
        assert results[0].symbol == "300001.SZ"
        assert results[0].rank_in_sector == 1

    def test_buy_decision_accepts_playbook_contexts(self):
        engine = BuyDecisionEngine()
        profile = MarketProfile(sentiment_phase="主升", position_ceiling=0.8)
        contexts = [
            PlaybookContext(
                playbook="leader_chase",
                symbol="300001.SZ",
                sector="AI",
                confidence=0.84,
                rank_in_sector=1,
                exit_params={"win_rate": 0.61, "pl_ratio": 2.1},
            ),
            PlaybookContext(
                playbook="divergence_reseal",
                symbol="300002.SZ",
                sector="AI",
                confidence=0.71,
                rank_in_sector=2,
                exit_params={"win_rate": 0.55, "pl_ratio": 1.8},
            ),
        ]
        result = engine.generate(
            candidates=[],
            scores={},
            account_equity=1_000_000,
            prices={"300001.SZ": 10.0, "300002.SZ": 20.0},
            atrs={"300001.SZ": 0.2, "300002.SZ": 0.4},
            profile=profile,
            playbook_contexts=contexts,
        )
        assert [item.symbol for item in result] == ["300001.SZ", "300002.SZ"]
        assert result[0].signals[0].source_strategy == "leader_chase"
        assert result[0].score == 84.0
        assert result[0].position_plan is not None
