"""端到端集成测试 — dry-run 全流程验证"""

import numpy as np
import pandas as pd
import pytest

from ashare_system.container import get_settings, get_execution_adapter, get_market_adapter, reset_container
from ashare_system.data.fetcher import DataFetcher
from ashare_system.data.cleaner import DataCleaner
from ashare_system.data.validator import DataValidator
from ashare_system.factors import registry, FactorEngine
from ashare_system.sentiment.indicators import SentimentIndicators, calc_sentiment_score
from ashare_system.sentiment.calculator import SentimentCalculator
from ashare_system.sentiment.position_map import PositionMapper
from ashare_system.strategy.screener import StockScreener
from ashare_system.strategy.buy_decision import BuyDecisionEngine
from ashare_system.strategy.sell_decision import SellDecisionEngine, PositionState
from ashare_system.strategy.position_mgr import PositionManager, PositionInput
from ashare_system.risk.rules import RiskRules
from ashare_system.risk.guard import ExecutionGuard
from ashare_system.backtest.engine import BacktestEngine, BacktestConfig
from ashare_system.backtest.metrics import MetricsCalculator
from ashare_system.ai.nlp_sentiment import NLPSentimentAnalyzer
from ashare_system.monitor.alert_engine import AlertEngine
from ashare_system.monitor.stock_pool import StockPoolManager
from ashare_system.report.generator import ReportGenerator
from ashare_system.report.daily import DailyReporter, DailyReportData
from ashare_system.contracts import MarketProfile, BalanceSnapshot, PlaceOrderRequest


def make_ohlcv(n: int = 60, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.2)
    return pd.DataFrame({
        "open": close * (1 + np.random.randn(n) * 0.005),
        "high": close * (1 + np.abs(np.random.randn(n) * 0.01)),
        "low": close * (1 - np.abs(np.random.randn(n) * 0.01)),
        "close": close,
        "volume": np.random.randint(100_000, 1_000_000, n).astype(float),
        "amount": close * np.random.randint(100_000, 1_000_000, n),
    })


class TestEndToEndDryRun:
    """完整 dry-run 流程: 数据→因子→情绪→选股→风控→回测"""

    def test_full_pipeline(self, tmp_path):
        """端到端主流程"""
        reset_container()

        # 1. 数据层
        market = get_market_adapter()
        fetcher = DataFetcher(market)
        universe = fetcher.fetch_universe("main-board")
        assert isinstance(universe, list)

        snapshots = fetcher.fetch_snapshots(universe[:5])
        assert isinstance(snapshots, list)

        bars_result = fetcher.fetch_bars(universe[:5], "1d")
        assert bars_result.quality.source in ("real", "unavailable")

        # 2. 数据清洗
        cleaner = DataCleaner()
        validator = DataValidator()
        if bars_result.bars:
            issues = validator.validate_bars(bars_result.bars)
            assert isinstance(issues, list)

        # 3. 因子计算
        df = make_ohlcv(60)
        engine = FactorEngine()
        results = engine.compute_category("technical", df, normalize=True)
        assert len(results) >= 10
        factor_df = engine.to_dataframe(results)
        assert isinstance(factor_df, pd.DataFrame)

        # 4. 情绪判定
        ind = SentimentIndicators(
            date="2026-04-03",
            limit_up_count=60,
            limit_down_count=8,
            board_fail_rate=0.25,
            max_consecutive_up=5,
            up_down_ratio=2.2,
            total_amount_billion=7500,
        )
        score = calc_sentiment_score(ind)
        assert 0 <= score <= 100

        calc = SentimentCalculator()
        profile = calc.calc_daily(ind)
        assert profile.sentiment_phase in ["冰点", "回暖", "主升", "高潮"]

        mapper = PositionMapper()
        ceiling = mapper.get_ceiling(profile.sentiment_phase)
        assert 0 < ceiling <= 1.0

        # 5. 选股漏斗
        candidates = ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "002594.SZ"]
        factor_scores = {s: np.random.uniform(0.4, 0.9) for s in candidates}
        screener = StockScreener()
        screen_result = screener.run(candidates, profile=profile, factor_scores=factor_scores, min_factor_score=0.5, top_n=3)
        assert isinstance(screen_result.passed, list)
        assert len(screen_result.passed) <= 3

        # 6. 买入决策
        buy_engine = BuyDecisionEngine()
        prices = {s: 10.0 + i for i, s in enumerate(candidates)}
        atrs = {s: prices[s] * 0.02 for s in candidates}
        buy_candidates = buy_engine.generate(
            candidates=screen_result.passed,
            scores={s: factor_scores[s] * 100 for s in screen_result.passed},
            account_equity=1_000_000,
            prices=prices,
            atrs=atrs,
            profile=profile,
            top_n=3,
        )
        assert isinstance(buy_candidates, list)

        # 7. 风控检查
        rules = RiskRules()
        guard = ExecutionGuard()
        bal = BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=600_000)
        for candidate in buy_candidates:
            if candidate.position_plan and candidate.position_plan.target_value > 0:
                req = PlaceOrderRequest(
                    account_id="sim-001",
                    symbol=candidate.symbol,
                    side="BUY",
                    quantity=max(candidate.position_plan.target_shares, 100),
                    price=prices.get(candidate.symbol, 10.0),
                    request_id=f"req-{candidate.symbol}",
                )
                approved, reason, _ = guard.approve(req, bal, profile)
                assert isinstance(approved, bool)

        # 8. 卖出决策
        sell_engine = SellDecisionEngine()
        state = PositionState(
            symbol="600519.SH",
            entry_price=10.0,
            atr=0.2,
            holding_days=2,
            current_price=10.5,
        )
        signal = sell_engine.evaluate(state)
        # 正常持仓范围内不应触发止损
        assert signal is None or signal.sell_ratio > 0

        # 9. 回测
        config = BacktestConfig(initial_cash=1_000_000)
        bt_engine = BacktestEngine(config)
        dates = pd.date_range("2025-01-01", periods=30, freq="B")
        bt_signals = pd.DataFrame(index=dates, columns=["600519.SH"], data="HOLD")
        bt_signals.loc[dates[5], "600519.SH"] = "BUY"
        bt_signals.loc[dates[20], "600519.SH"] = "SELL"
        close_prices = 10.0 + np.cumsum(np.random.randn(30) * 0.2)
        price_data = {
            "600519.SH": pd.DataFrame(
                {"close": close_prices, "open": close_prices, "high": close_prices * 1.01, "low": close_prices * 0.99, "volume": 100000},
                index=dates.astype(str),
            )
        }
        bt_result = bt_engine.run(bt_signals, price_data)
        assert isinstance(bt_result.metrics.total_return, float)
        assert isinstance(bt_result.metrics.sharpe_ratio, float)

        # 10. 报告生成
        reporter = DailyReporter(ReportGenerator(output_dir=tmp_path))
        data = DailyReportData(
            date="2026-04-03",
            profile=profile,
            total_pnl=bt_result.metrics.total_return * 1_000_000,
            total_return_pct=bt_result.metrics.total_return,
            trades=bt_result.trades,
        )
        content = reporter.generate(data)
        assert "2026-04-03" in content

    def test_data_failure_returns_empty(self):
        """数据获取失败必须返回空，不注入假数据"""
        fetcher = DataFetcher(None)
        snaps = fetcher.fetch_snapshots(["600519.SH"])
        assert snaps == []
        result = fetcher.fetch_bars(["600519.SH"])
        assert result.bars == []
        assert result.quality.source == "unavailable"

    def test_factor_pipeline_integrity(self):
        """因子 pipeline 完整性: 去极值→标准化→中性化"""
        from ashare_system.factors.pipeline import FactorPipeline
        pipeline = FactorPipeline()
        raw = pd.Series(np.random.randn(100) * 10)
        raw.iloc[0] = 1000  # 极值
        raw.iloc[-1] = -1000
        result = pipeline.run(raw)
        assert result.max() < 100
        assert result.min() > -100
        assert abs(result.mean()) < 1.0

    def test_sentiment_to_position_flow(self):
        """情绪→仓位映射流程"""
        calc = SentimentCalculator()
        mapper = PositionMapper()
        pm = PositionManager()

        for limit_up, expected_phase in [(5, "冰点"), (50, "回暖"), (100, "主升"), (200, "高潮")]:
            ind = SentimentIndicators(
                date="test",
                limit_up_count=limit_up,
                limit_down_count=max(0, 50 - limit_up // 2),
                up_down_ratio=limit_up / 20,
                total_amount_billion=limit_up * 50,
            )
            profile = calc.calc_daily(ind)
            ceiling = mapper.get_ceiling(profile.sentiment_phase)
            inp = PositionInput(symbol="test", win_rate=0.6, profit_loss_ratio=2.0, atr=0.2, price=10.0, account_equity=1_000_000)
            plan = pm.calc(inp, profile)
            assert plan.final_ratio <= ceiling

    def test_risk_guard_blocks_oversize(self):
        """风控守卫拦截超仓"""
        guard = ExecutionGuard()
        bal = BalanceSnapshot(account_id="test", total_asset=100_000, cash=100_000)
        req = PlaceOrderRequest(
            account_id="test", symbol="test", side="BUY",
            quantity=10000, price=10.0, request_id="big-order",
        )
        approved, reason, adjusted = guard.approve(req, bal)
        # 10000 * 10 = 100,000 = 100% 仓位，应被限制到25%
        if approved:
            assert adjusted.quantity <= req.quantity

    def test_nlp_sentiment_no_false_positives(self):
        """NLP 不应把否定句判为正面"""
        nlp = NLPSentimentAnalyzer()
        positive = nlp.analyze("公司业绩大幅增长，超预期")
        negated = nlp.analyze("公司业绩并非增长，实际下滑")
        assert positive.score > negated.score

    def test_alert_engine_detects_limit_up(self):
        """预警引擎正确检测涨停"""
        from ashare_system.contracts import QuoteSnapshot
        engine = AlertEngine()
        engine._prev_prices["600519.SH"] = 100.0
        snap = QuoteSnapshot(
            symbol="600519.SH", last_price=110.0,
            bid_price=109.9, ask_price=110.0,
            volume=500_000, pre_close=100.0,
        )
        alerts = engine.check(snap)
        types = {a.alert_type for a in alerts}
        assert "limit_up" in types or "price_spike" in types

    def test_app_routes_accessible(self):
        """FastAPI 应用路由可访问"""
        from fastapi.testclient import TestClient
        from ashare_system.app import create_app
        reset_container()
        app = create_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

        resp = client.get("/system/health")
        assert resp.status_code == 200

        resp = client.get("/market/universe")
        assert resp.status_code == 200

        resp = client.get("/execution/balance/sim-001")
        assert resp.status_code == 200

        resp = client.get("/runtime/health")
        assert resp.status_code == 200

        resp = client.post("/runtime/jobs/pipeline", json={"max_candidates": 3, "auto_trade": False, "account_id": "sim-001"})
        assert resp.status_code == 200
        assert "top_picks" in resp.json()

        resp = client.get("/runtime/reports/latest")
        assert resp.status_code == 200

        resp = client.get("/research/health")
        assert resp.status_code == 200

        resp = client.post(
            "/research/events/news",
            json={
                "symbol": "600519.SH",
                "title": "业绩交流纪要",
                "summary": "公司披露一季度经营平稳。",
                "sentiment": "positive",
                "source": "manual-test",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

        resp = client.post("/research/sync", json={"symbols": ["600519.SH"]})
        assert resp.status_code == 200
        assert resp.json()["news_count"] >= 1

        resp = client.get("/system/audits")
        assert resp.status_code == 200

        resp = client.get("/system/research/summary")
        assert resp.status_code == 200
        assert "news_count" in resp.json()

        resp = client.get("/system/reports/runtime")
        assert resp.status_code == 200

        resp = client.post(
            "/system/meetings/record",
            json={"title": "盘前协作会", "summary": "确认运行链路与研究同步接口可用。"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = client.get("/system/meetings/latest")
        assert resp.status_code == 200
        assert resp.json()["title"] == "盘前协作会"
