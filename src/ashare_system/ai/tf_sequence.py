"""Transformer 序列预测模型 — 板块轮动 + 序列预测 (目标 AUC > 0.81)"""

from __future__ import annotations

import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ModelMetrics, PredictResult
from .trainer import BaseTrainer, TrainConfig
from ..logging_config import get_logger

logger = get_logger("ai.tf_sequence")


@dataclass
class TransformerConfig:
    d_model: int = 64
    n_heads: int = 4
    n_layers: int = 2
    seq_len: int = 20       # 输入序列长度
    dropout: float = 0.1
    learning_rate: float = 1e-3
    epochs: int = 50
    batch_size: int = 32
    patience: int = 5


class TransformerSequenceModel(BaseTrainer):
    """
    Transformer 序列预测模型。
    输入: (batch, seq_len, n_features) 时序特征
    输出: 未来 N 日涨跌概率
    """

    def __init__(self, config: TransformerConfig | None = None) -> None:
        self.config = config or TransformerConfig()
        self._model = None
        self._fitted = False

    def build_sequences(self, df: pd.DataFrame, seq_len: int | None = None) -> np.ndarray:
        """将 DataFrame 转换为滑动窗口序列 (n_samples, seq_len, n_features)"""
        n = seq_len or self.config.seq_len
        data = df.values.astype(float)
        sequences = []
        for i in range(len(data) - n):
            sequences.append(data[i:i + n])
        return np.array(sequences) if sequences else np.empty((0, n, data.shape[1]))

    def train(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None = None) -> ModelMetrics:
        """训练 Transformer 模型"""
        try:
            import torch
            import torch.nn as nn
            return self._train_torch(X, y, train_config)
        except ImportError:
            logger.warning("PyTorch 未安装，使用简化线性模型替代 Transformer")
            return self._train_fallback(X, y, train_config)

    def _train_fallback(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None) -> ModelMetrics:
        """无 PyTorch 时的降级实现"""
        try:
            from sklearn.linear_model import LogisticRegression
            cfg = train_config or TrainConfig()
            split = int(len(X) * (1 - cfg.test_size))
            model = LogisticRegression(max_iter=200)
            model.fit(X.iloc[:split], y.iloc[:split])
            y_pred = model.predict(X.iloc[split:])
            y_prob = model.predict_proba(X.iloc[split:])[:, 1]
            self._model = model
            self._fitted = True
            return self.calc_metrics(y.iloc[split:].values, y_pred, y_prob)
        except ImportError:
            logger.warning("sklearn 也未安装，返回空指标")
            self._fitted = True
            return ModelMetrics()

    def _train_torch(self, X: pd.DataFrame, y: pd.Series, train_config: TrainConfig | None) -> ModelMetrics:
        """PyTorch Transformer 训练"""
        import torch
        import torch.nn as nn

        cfg = train_config or TrainConfig()
        seqs = self.build_sequences(X)
        if len(seqs) == 0:
            return ModelMetrics()

        labels = y.values[self.config.seq_len:]
        split = int(len(seqs) * (1 - cfg.test_size))

        d_model = self.config.d_model

        class TFModel(nn.Module):
            def __init__(self, n_feat: int, d_model: int, n_heads: int, n_layers: int, seq_len: int):
                super().__init__()
                self.proj = nn.Linear(n_feat, d_model)
                # sinusoidal positional encoding
                pe = torch.zeros(seq_len, d_model)
                pos = torch.arange(0, seq_len).unsqueeze(1).float()
                div = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
                pe[:, 0::2] = torch.sin(pos * div)
                pe[:, 1::2] = torch.cos(pos * div[:d_model // 2])
                self.register_buffer("pe", pe.unsqueeze(0))  # (1, seq_len, d_model)
                encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=n_heads, batch_first=True)
                self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
                self.head = nn.Linear(d_model, 1)

            def forward(self, x):
                x = self.proj(x) + self.pe
                x = self.encoder(x)
                return torch.sigmoid(self.head(x[:, -1, :]))

        n_feat = seqs.shape[2]
        model = TFModel(n_feat, d_model, self.config.n_heads, self.config.n_layers, self.config.seq_len)
        optimizer = torch.optim.Adam(model.parameters(), lr=self.config.learning_rate)
        criterion = nn.BCELoss()

        X_train = torch.FloatTensor(seqs[:split])
        y_train = torch.FloatTensor(labels[:split]).unsqueeze(1)
        X_val = torch.FloatTensor(seqs[split:])
        y_val = torch.FloatTensor(labels[split:]).unsqueeze(1)

        best_val_loss = float("inf")
        patience_counter = 0
        best_state = None

        for epoch in range(self.config.epochs):
            model.train()
            optimizer.zero_grad()
            out = model(X_train)
            loss = criterion(out, y_train)
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
                    logger.info("Transformer 早停: epoch %d 提前结束", epoch)
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
            else:
                import torch
                seqs = self.build_sequences(X)
                if len(seqs) == 0:
                    return []
                self._model.eval()
                with torch.no_grad():
                    probs = self._model(torch.FloatTensor(seqs)).numpy().flatten()
            return [
                PredictResult(symbol=str(idx), score=float(p) * 100, confidence=float(p), model_name="tf_sequence")
                for idx, p in zip(X.index[self.config.seq_len:], probs)
            ]
        except Exception as e:
            logger.warning("Transformer 推理失败: %s", e)
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

        d_model = self.config.d_model

        class TFModel(nn.Module):
            def __init__(self, n_feat: int, d_model: int, n_heads: int, n_layers: int, seq_len: int):
                super().__init__()
                self.proj = nn.Linear(n_feat, d_model)
                pe = torch.zeros(seq_len, d_model)
                pos = torch.arange(0, seq_len).unsqueeze(1).float()
                div = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
                pe[:, 0::2] = torch.sin(pos * div)
                pe[:, 1::2] = torch.cos(pos * div[:d_model // 2])
                self.register_buffer("pe", pe.unsqueeze(0))
                encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=n_heads, batch_first=True)
                self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
                self.head = nn.Linear(d_model, 1)

            def forward(self, x):
                x = self.proj(x) + self.pe
                x = self.encoder(x)
                return torch.sigmoid(self.head(x[:, -1, :]))

        model = TFModel(n_feat, d_model, self.config.n_heads, self.config.n_layers, self.config.seq_len)
        model.load_state_dict(torch.load(Path(path)))
        model.eval()
        self._model = model
        self._fitted = True
