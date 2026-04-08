"""高频盘口信号模型 (目标准确率 > 59%)"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field

from ..ai.contracts import ModelMetrics, PredictResult
from ..ai.trainer import BaseTrainer, TrainConfig
from ..logging_config import get_logger

logger = get_logger("ai_advanced.hft_signal")


@dataclass
class HFTConfig:
    window: int = 10          # 特征窗口 (tick 数)
    threshold: float = 0.002  # 价格变动阈值 (0.2%)
    min_samples: int = 100


@dataclass
class HFTFeatures:
    """高频特征集"""
    bid_ask_spread: float = 0.0
    order_imbalance: float = 0.0
    price_momentum: float = 0.0
    volume_acceleration: float = 0.0
    tick_direction: float = 0.0


class HFTSignalModel(BaseTrainer):
    """
    高频盘口信号模型。
    基于盘口深度、委比、Tick 方向等微观结构特征预测短期价格方向。
    """

    def __init__(self, config: HFTConfig | None = None) -> None:
        self.config = config or HFTConfig()
        self._model = None
        self._fitted = False

    def extract_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """从 OHLCV + 盘口数据提取高频特征"""
        features = pd.DataFrame(index=df.index)
        # 价格动量
        features["price_momentum"] = df["close"].pct_change(self.config.window)
        # 成交量加速度
        vol_ma = df["volume"].rolling(self.config.window).mean()
        features["volume_accel"] = df["volume"] / vol_ma.replace(0, np.nan) - 1
        # Tick 方向
        features["tick_dir"] = np.sign(df["close"].diff()).rolling(self.config.window).sum()
        # 振幅
        features["amplitude"] = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        # 买卖价差代理
        if "ask_price" in df.columns and "bid_price" in df.columns:
            mid = (df["ask_price"] + df["bid_price"]) / 2
            features["spread"] = (df["ask_price"] - df["bid_price"]) / mid.replace(0, np.nan)
        else:
            features["spread"] = features["amplitude"]
        return features.dropna()

    def train(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None = None) -> ModelMetrics:
        """训练高频信号模型"""
        try:
            from sklearn.ensemble import RandomForestClassifier
            cfg = train_config or TrainConfig()
            split = int(len(X) * (1 - cfg.test_size))
            if split < self.config.min_samples:
                logger.warning("训练样本不足 %d，跳过训练", self.config.min_samples)
                return ModelMetrics()
            model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
            model.fit(X.iloc[:split], y.iloc[:split])
            y_pred = model.predict(X.iloc[split:])
            y_prob = model.predict_proba(X.iloc[split:])[:, 1]
            self._model = model
            self._fitted = True
            metrics = self.calc_metrics(y.iloc[split:].values, y_pred, y_prob)
            logger.info("HFT 模型训练完成: Acc=%.3f, AUC=%.3f", metrics.accuracy, metrics.auc)
            return metrics
        except ImportError:
            logger.warning("sklearn 未安装，HFT 模型跳过训练")
            self._fitted = True
            return ModelMetrics()

    def predict(self, X: pd.DataFrame) -> list[PredictResult]:
        if not self._fitted or self._model is None:
            return []
        try:
            probs = self._model.predict_proba(X)[:, 1]
            return [
                PredictResult(symbol=str(idx), score=float(p) * 100, confidence=float(p), model_name="hft_signal")
                for idx, p in zip(X.index, probs)
            ]
        except Exception as e:
            logger.warning("HFT 推理失败: %s", e)
            return []

    def generate_signal(self, features: HFTFeatures) -> str:
        """基于规则的实时信号生成 (无需训练)"""
        score = (
            features.order_imbalance * 0.4
            + features.price_momentum * 0.3
            + features.tick_direction * 0.2
            + features.volume_acceleration * 0.1
        )
        if score > 0.3:
            return "BUY"
        elif score < -0.3:
            return "SELL"
        return "HOLD"
