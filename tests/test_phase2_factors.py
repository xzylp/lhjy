"""Phase 2 因子引擎测试"""

import numpy as np
import pandas as pd
import pytest

from ashare_system.factors import registry, FactorEngine, FactorPipeline, FactorValidator


def make_ohlcv(n: int = 60) -> pd.DataFrame:
    np.random.seed(42)
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.2)
    return pd.DataFrame({
        "open": close * (1 + np.random.randn(n) * 0.005),
        "high": close * (1 + np.abs(np.random.randn(n) * 0.01)),
        "low": close * (1 - np.abs(np.random.randn(n) * 0.01)),
        "close": close,
        "volume": np.random.randint(100_000, 1_000_000, n).astype(float),
        "amount": close * np.random.randint(100_000, 1_000_000, n),
    })


class TestFactorRegistry:
    def test_registry_count(self):
        assert len(registry) >= 100

    def test_registry_categories(self):
        cats = {f.category for f in registry.list_all()}
        assert "technical" in cats
        assert "price_volume" in cats
        assert "momentum" in cats

    def test_get_factor(self):
        f = registry.get("ma20")
        assert f is not None
        assert f.name == "ma20"

    def test_list_by_category(self):
        tech = registry.list_by_category("technical")
        assert len(tech) >= 20


class TestFactorEngine:
    def test_compute_one(self):
        engine = FactorEngine()
        df = make_ohlcv()
        result = engine.compute_one("ma20", df, normalize=False)
        assert result is not None
        assert result.factor_name == "ma20"
        assert len(result.values) == len(df)

    def test_compute_all(self):
        engine = FactorEngine()
        df = make_ohlcv()
        results = engine.compute_all(df, normalize=False)
        assert len(results) >= 50

    def test_compute_category(self):
        engine = FactorEngine()
        df = make_ohlcv()
        results = engine.compute_category("technical", df, normalize=False)
        assert len(results) >= 10

    def test_to_dataframe(self):
        engine = FactorEngine()
        df = make_ohlcv()
        results = engine.compute_category("momentum", df, normalize=False)
        factor_df = engine.to_dataframe(results, use_normalized=False)
        assert isinstance(factor_df, pd.DataFrame)
        assert len(factor_df) == len(df)

    def test_invalid_factor_returns_none(self):
        engine = FactorEngine()
        df = make_ohlcv()
        result = engine.compute_one("nonexistent_factor", df)
        assert result is None


class TestFactorPipeline:
    def test_winsorize(self):
        pipeline = FactorPipeline()
        s = pd.Series([1, 2, 3, 4, 5, 100, -100])
        result = pipeline.winsorize(s)
        assert result.max() < 100
        assert result.min() > -100

    def test_zscore(self):
        pipeline = FactorPipeline()
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = pipeline.zscore(s)
        assert abs(result.mean()) < 1e-10
        assert abs(result.std() - 1.0) < 1e-10

    def test_neutralize(self):
        pipeline = FactorPipeline()
        factor = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        industry = pd.Series(["A", "A", "A", "B", "B", "B"])
        result = pipeline.neutralize(factor, industry)
        assert len(result) == len(factor)

    def test_run_full_pipeline(self):
        pipeline = FactorPipeline()
        s = pd.Series(np.random.randn(100))
        result = pipeline.run(s)
        assert len(result) == 100
        assert abs(result.mean()) < 0.5


class TestFactorValidator:
    def test_validate_single(self):
        validator = FactorValidator()
        factor = pd.Series(np.random.randn(50))
        returns = factor * 0.5 + pd.Series(np.random.randn(50) * 0.1)
        result = validator.validate(factor, returns, "test_factor")
        assert result.factor_name == "test_factor"
        assert isinstance(result.ic_mean, float)

    def test_validate_correlated_factor(self):
        """高相关因子应该通过有效性验证"""
        validator = FactorValidator()
        np.random.seed(0)
        factor = pd.Series(np.random.randn(100))
        returns = factor + pd.Series(np.random.randn(100) * 0.1)
        result = validator.validate(factor, returns, "corr_factor")
        assert abs(result.ic_mean) > 0.5


class TestTechnicalFactors:
    def test_ma_factors(self):
        df = make_ohlcv(60)
        engine = FactorEngine()
        for name in ["ma5", "ma10", "ma20"]:
            r = engine.compute_one(name, df, normalize=False)
            assert r is not None
            assert not r.values.iloc[-1:].isna().all()

    def test_rsi(self):
        df = make_ohlcv(60)
        engine = FactorEngine()
        r = engine.compute_one("rsi14", df, normalize=False)
        assert r is not None
        valid = r.values.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_atr(self):
        df = make_ohlcv(60)
        engine = FactorEngine()
        r = engine.compute_one("atr14", df, normalize=False)
        assert r is not None
        assert (r.values.dropna() > 0).all()

    def test_macd(self):
        df = make_ohlcv(60)
        engine = FactorEngine()
        for name in ["macd_dif", "macd_dea", "macd_bar"]:
            r = engine.compute_one(name, df, normalize=False)
            assert r is not None
