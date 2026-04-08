from ashare_system.contracts import LeaderRankResult, MarketProfile, SectorProfile, StockBehaviorProfile
from ashare_system.strategy.leader_rank import LeaderRanker
from ashare_system.strategy.router import StrategyRouter


class TestLeaderRanker:
    def test_rank_prefers_front_leader(self):
        ranker = LeaderRanker()
        profiles = {
            "300001.SZ": StockBehaviorProfile(symbol="300001.SZ", board_success_rate_20d=0.8, leader_frequency_30d=0.6),
            "300002.SZ": StockBehaviorProfile(symbol="300002.SZ", board_success_rate_20d=0.4, leader_frequency_30d=0.1),
        }
        sector_data = {
            "300001.SZ": {"sector": "AI", "zt_order_rank": 1, "seal_ratio": 0.12, "diffusion_count": 8, "turnover_rate": 6.0},
            "300002.SZ": {"sector": "AI", "zt_order_rank": 4, "seal_ratio": 0.05, "diffusion_count": 2, "turnover_rate": 4.0},
        }
        results = ranker.rank(["300001.SZ", "300002.SZ"], sector_data, profiles)
        assert results[0].symbol == "300001.SZ"
        assert results[0].is_core_leader
        assert results[0].leader_score > results[1].leader_score


class TestRouterWithLeaderRank:
    def test_router_uses_leader_rank_and_behavior(self):
        router = StrategyRouter()
        profile = MarketProfile(
            sentiment_phase="主升",
            regime="trend",
            regime_score=0.9,
            allowed_playbooks=["leader_chase", "divergence_reseal"],
        )
        sectors = [SectorProfile(sector_name="AI", life_cycle="ferment", strength_score=0.8)]
        behavior_profiles = {
            "300001.SZ": StockBehaviorProfile(symbol="300001.SZ", board_success_rate_20d=0.8, style_tag="leader"),
            "300002.SZ": StockBehaviorProfile(symbol="300002.SZ", board_success_rate_20d=0.3, bomb_rate_20d=0.4),
        }
        leader_ranks = {
            "300001.SZ": LeaderRankResult(symbol="300001.SZ", sector="AI", leader_score=0.85, zt_order_rank=1, is_core_leader=True),
            "300002.SZ": LeaderRankResult(symbol="300002.SZ", sector="AI", leader_score=0.35, zt_order_rank=3, is_core_leader=False),
        }
        results = router.route(
            profile=profile,
            sector_profiles=sectors,
            candidates=["300001.SZ", "300002.SZ"],
            stock_info={"300001.SZ": {"sector": "AI"}, "300002.SZ": {"sector": "AI"}},
            behavior_profiles=behavior_profiles,
            leader_ranks=leader_ranks,
        )
        assert results[0].symbol == "300001.SZ"
        assert results[0].leader_score == 0.85
        assert results[0].style_tag == "leader"
        assert results[0].rank_in_sector == 1
