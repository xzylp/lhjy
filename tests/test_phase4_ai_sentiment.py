"""Phase 4 AI + 情绪周期测试"""

import numpy as np
import pandas as pd
import pytest

from ashare_system.sentiment.cycle import SentimentCycle
from ashare_system.sentiment.indicators import SentimentIndicators, calc_sentiment_score
from ashare_system.sentiment.calculator import SentimentCalculator
from ashare_system.sentiment.position_map import PositionMapper
from ashare_system.sentiment.turning_point import TurningPointDetector
from ashare_system.ai.nlp_sentiment import NLPSentimentAnalyzer
from ashare_system.ai.registry import ModelRegistry
from ashare_system.ai.contracts import ModelVersion, ModelMetrics
from ashare_system.contracts import MarketProfile


class TestSentimentCycle:
    def test_phase_determination(self):
        cycle = SentimentCycle()
        assert cycle.determine_phase(10) == "冰点"
        assert cycle.determine_phase(35) == "回暖"
        assert cycle.determine_phase(60) == "主升"
        assert cycle.determine_phase(85) == "高潮"

    def test_boundary_values(self):
        cycle = SentimentCycle()
        assert cycle.determine_phase(0) == "冰点"
        assert cycle.determine_phase(24.9) == "冰点"
        assert cycle.determine_phase(25) == "回暖"
        assert cycle.determine_phase(74.9) == "主升"
        assert cycle.determine_phase(75) == "高潮"
        assert cycle.determine_phase(100) == "高潮"

    def test_transition_zone(self):
        cycle = SentimentCycle()
        assert cycle.is_transition_zone(24.0)   # 接近25边界
        assert cycle.is_transition_zone(51.5)   # 接近50边界
        assert cycle.is_transition_zone(50)     # 50本身就是边界
        assert not cycle.is_transition_zone(40) # 远离边界

    def test_describe(self):
        cycle = SentimentCycle()
        for phase in ["冰点", "回暖", "主升", "高潮"]:
            desc = cycle.describe(phase)
            assert len(desc) > 0


class TestSentimentIndicators:
    def test_score_range(self):
        ind = SentimentIndicators(date="2026-04-03", limit_up_count=50, limit_down_count=10, board_fail_rate=0.3, max_consecutive_up=5, up_down_ratio=2.0, total_amount_billion=7000)
        score = calc_sentiment_score(ind)
        assert 0 <= score <= 100

    def test_high_score(self):
        ind = SentimentIndicators(date="test", limit_up_count=150, limit_down_count=0, board_fail_rate=0.0, max_consecutive_up=10, up_down_ratio=5.0, total_amount_billion=15000)
        score = calc_sentiment_score(ind)
        assert score > 70

    def test_low_score(self):
        ind = SentimentIndicators(date="test", limit_up_count=0, limit_down_count=100, board_fail_rate=1.0, max_consecutive_up=0, up_down_ratio=0.1, total_amount_billion=1000)
        score = calc_sentiment_score(ind)
        assert score < 30


class TestSentimentCalculator:
    def test_calc_daily(self):
        calc = SentimentCalculator()
        ind = SentimentIndicators(date="2026-04-03", limit_up_count=80, limit_down_count=5, board_fail_rate=0.2, max_consecutive_up=6, up_down_ratio=2.5, total_amount_billion=8000)
        profile = calc.calc_daily(ind)
        assert isinstance(profile, MarketProfile)
        assert profile.sentiment_phase in ["冰点", "回暖", "主升", "高潮"]
        assert 0 <= profile.sentiment_score <= 100
        assert 0 < profile.position_ceiling <= 1.0


class TestPositionMapper:
    def test_all_phases(self):
        mapper = PositionMapper()
        assert mapper.get_ceiling("冰点") == 0.20
        assert mapper.get_ceiling("回暖") == 0.60
        assert mapper.get_ceiling("主升") == 0.80
        assert mapper.get_ceiling("高潮") == 0.30

    def test_apply_to_profile(self):
        mapper = PositionMapper()
        profile = MarketProfile(sentiment_phase="主升", position_ceiling=0.5)
        updated = mapper.apply_to_profile(profile)
        assert updated.position_ceiling == 0.80

    def test_strategy_hints(self):
        mapper = PositionMapper()
        for phase in ["冰点", "回暖", "主升", "高潮"]:
            hint = mapper.get_strategy_hint(phase)
            assert len(hint) > 0


class TestTurningPointDetector:
    def test_ice_to_warm(self):
        detector = TurningPointDetector()
        current = SentimentIndicators(date="today", limit_up_count=35, limit_down_count=3)
        # history[-1] 是前一天，需满足 limit_up > 30 且 limit_down < 5
        history = [
            SentimentIndicators(date="d1", limit_up_count=20, limit_down_count=8),
            SentimentIndicators(date="d2", limit_up_count=32, limit_down_count=4),
        ]
        signal = detector.detect(current, history, "冰点")
        assert signal.detected
        assert signal.to_phase == "回暖"

    def test_no_signal_stable(self):
        detector = TurningPointDetector()
        current = SentimentIndicators(date="today", limit_up_count=50, limit_down_count=10)
        history = [
            SentimentIndicators(date="d1", limit_up_count=48, limit_down_count=12),
            SentimentIndicators(date="d2", limit_up_count=45, limit_down_count=15),
        ]
        signal = detector.detect(current, history, "主升")
        assert not signal.detected

    def test_insufficient_history(self):
        detector = TurningPointDetector()
        current = SentimentIndicators(date="today", limit_up_count=50, limit_down_count=5)
        signal = detector.detect(current, [], "冰点")
        assert not signal.detected


class TestNLPSentiment:
    def test_positive_text(self):
        nlp = NLPSentimentAnalyzer()
        r = nlp.analyze("公司季度利润大幅超预期，增长显著")
        assert r.label == "positive"
        assert r.score > 0

    def test_negative_text(self):
        nlp = NLPSentimentAnalyzer()
        r = nlp.analyze("公司净亏损扩大，业绩大幅下滑")
        assert r.label == "negative"
        assert r.score < 0

    def test_neutral_text(self):
        nlp = NLPSentimentAnalyzer()
        r = nlp.analyze("公司发布公告，内容如下")
        assert r.label == "neutral"

    def test_negation_detection(self):
        nlp = NLPSentimentAnalyzer()
        r_pos = nlp.analyze("公司业绩增长")
        r_neg = nlp.analyze("公司业绩并非增长")
        assert r_pos.score > r_neg.score

    def test_batch_analyze(self):
        nlp = NLPSentimentAnalyzer()
        texts = ["利好消息", "净亏损", "正常经营"]
        results = nlp.analyze_batch(texts)
        assert len(results) == 3


class TestModelRegistry:
    def test_register_and_get(self):
        reg = ModelRegistry()
        v = ModelVersion(name="xgb_scorer", version="1.0", metrics=ModelMetrics(auc=0.85))
        reg.register(v)
        active = reg.get_active("xgb_scorer")
        assert active is not None
        assert active.version == "1.0"

    def test_deactivate(self):
        reg = ModelRegistry()
        v = ModelVersion(name="test_model", version="1.0")
        reg.register(v)
        reg.deactivate("test_model", "1.0")
        assert reg.get_active("test_model") is None

    def test_list_models(self):
        reg = ModelRegistry()
        reg.register(ModelVersion(name="model_a", version="1.0"))
        reg.register(ModelVersion(name="model_b", version="1.0"))
        names = reg.list_models()
        assert "model_a" in names
        assert "model_b" in names
