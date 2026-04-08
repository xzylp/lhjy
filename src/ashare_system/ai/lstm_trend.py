"""LSTM 趋势预测模型 — 带 Attention 机制"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from pathlib import Path

from .contracts import ModelMetrics, PredictResult
from .trainer import BaseTrainer, TrainConfig
from ..logging_config import get_logger

logger = get_logger("ai.lstm_trend")


@dataclass
class LSTMConfig:
    hidden_size: int = 64
    num_layers: int = 2
    seq_len: int = 20
    dropout: float = 0.2
    learning_rate: float = 1e-3
    epochs: int = 50
    batch_size: int = 32
    use_attention: bool = True
    patience: int = 5


class LSTMTrendModel(BaseTrainer):
    """
    LSTM 趋势预测模型 (带 Attention)。
    适用于中期趋势判断 (5-20日)。
    """

    def __init__(self, config: LSTMConfig | None = None) -> None:
        self.config = config or LSTMConfig()
        self._model = None
        self._fitted = False

    def build_sequences(self, df: pd.DataFrame) -> np.ndarray:
        n = self.config.seq_len
        data = df.values.astype(float)
        return np.array([data[i:i + n] for i in range(len(data) - n)]) if len(data) > n else np.empty((0, n, data.shape[1]))

    def train(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None = None) -> ModelMetrics:
        try:
            import torch
            return self._train_torch(X, y, train_config)
        except ImportError:
            logger.warning("PyTorch 未安装，使用 sklearn 替代 LSTM")
            return self._train_fallback(X, y, train_config)

    def _train_fallback(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None) -> ModelMetrics:
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            cfg = train_config or TrainConfig()
            split = int(len(X) * (1 - cfg.test_size))
            model = GradientBoostingClassifier(n_estimators=50, max_depth=3)
            model.fit(X.iloc[:split], y.iloc[:split])
            y_pred = model.predict(X.iloc[split:])
            y_prob = model.predict_proba(X.iloc[split:])[:, 1]
            self._model = model
            self._fitted = True
            return self.calc_metrics(y.iloc[split:].values, y_pred, y_prob)
        except ImportError:
            self._fitted = True
            return ModelMetrics()

    def _train_torch(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None) -> ModelMetrics:
        import torch
        import torch.nn as nn

        cfg = train_config or TrainConfig()
        seqs = self.build_sequences(X)
        if len(seqs) == 0:
            return ModelMetrics()
        labels = y.values[self.config.seq_len:]
        split = int(len(seqs) * (1 - cfg.test_size))

        class AttentionLSTM(nn.Module):
            def __init__(self, n_feat: int, hidden: int, n_layers: int, dropout: float):
                super().__init__()
                self.lstm = nn.LSTM(n_feat, hidden, n_layers, batch_first=True, dropout=dropout if n_layers > 1 else 0)
                self.attn = nn.Linear(hidden, 1)
                self.head = nn.Linear(hidden, 1)

            def forward(self, x):
                out, _ = self.lstm(x)
                weights = torch.softmax(self.attn(out), dim=1)
                context = (weights * out).sum(dim=1)
                return torch.sigmoid(self.head(context))

        n_feat = seqs.shape[2]
        model = AttentionLSTM(n_feat, self.config.hidden_size, self.config.num_layers, self.config.dropout)
        optimizer = torch.optim.Adam(model.parameters(), lr=self.config.learning_rate)
        criterion = nn.BCELoss()

        X_train = torch.FloatTensor(seqs[:split])
        y_train = torch.FloatTensor(labels[:split]).unsqueeze(1)
        X_val = torch.FloatTensor(seqs[split:])
        y_val = torch.FloatTensor(labels[split:]).unsqueeze(1)

        best_val_loss = float("inf")
        patience_counter = 0
        best_state = None

        for _ in range(self.config.epochs):
            model.train()
            optimizer.zero_grad()
            loss = criterion(model(X_train), y_train)
            loss.backward()
            optimizer.step()

            model.eval()
            with torch.no_grad():
                val_loss = criterion(model(X_val), y_val).item()
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                patience_counter = 0
                best_state = {k: v.clone() for k, v in model.state_dict().items()}
            else:
                patience_counter += 1
                if patience_counter >= self.config.patience:
                    logger.info("LSTM 早停: epoch 提前结束")
                    break

        if best_state is not None:
            model.load_state_dict(best_state)

        model.eval()
        with torch.no_grad():
            probs = model(X_val).numpy().flatten()
            preds = (probs > 0.5).astype(int)

        self._model = model
        self._fitted = True
        return self.calc_metrics(labels[split:], preds, probs)

    def predict(self, X: pd.DataFrame) -> list[PredictResult]:
        if not self._fitted or self._model is None:
            return []
        try:
            if hasattr(self._model, "predict_proba"):
                probs = self._model.predict_proba(X)[:, 1]
                return [PredictResult(symbol=str(idx), score=float(p) * 100, confidence=float(p), model_name="lstm_trend") for idx, p in zip(X.index, probs)]
            import torch
            seqs = self.build_sequences(X)
            if len(seqs) == 0:
                return []
            self._model.eval()
            with torch.no_grad():
                probs = self._model(torch.FloatTensor(seqs)).numpy().flatten()
            return [PredictResult(symbol=str(idx), score=float(p) * 100, confidence=float(p), model_name="lstm_trend") for idx, p in zip(X.index[self.config.seq_len:], probs)]
        except Exception as e:
            logger.warning("LSTM 推理失败: %s", e)
            return []

    def save(self, path: str | Path) -> None:
        """保存模型权重"""
        import torch
        if self._model is None or hasattr(self._model, "predict_proba"):
            raise ValueError("无可保存的 PyTorch 模型")
        torch.save(self._model.state_dict(), Path(path))

    def load(self, path: str | Path, n_feat: int) -> None:
        """加载模型权重"""
        import torch
        import torch.nn as nn

        class AttentionLSTM(nn.Module):
            def __init__(self, n_feat: int, hidden: int, n_layers: int, dropout: float):
                super().__init__()
                self.lstm = nn.LSTM(n_feat, hidden, n_layers, batch_first=True, dropout=dropout if n_layers > 1 else 0)
                self.attn = nn.Linear(hidden, 1)
                self.head = nn.Linear(hidden, 1)

            def forward(self, x):
                out, _ = self.lstm(x)
                weights = torch.softmax(self.attn(out), dim=1)
                context = (weights * out).sum(dim=1)
                return torch.sigmoid(self.head(context))

        model = AttentionLSTM(n_feat, self.config.hidden_size, self.config.num_layers, self.config.dropout)
        model.load_state_dict(torch.load(Path(path)))
        model.eval()
        self._model = model
        self._fitted = True
