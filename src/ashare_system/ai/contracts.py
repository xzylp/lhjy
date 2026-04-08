"""AI 模型契约"""

from typing import Literal
from pydantic import BaseModel, Field


class ModelMetrics(BaseModel):
    auc: float = 0.0
    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    feature_importance: dict[str, float] = Field(default_factory=dict)


class ModelVersion(BaseModel):
    name: str
    version: str
    trained_at: str = ""
    metrics: ModelMetrics = Field(default_factory=ModelMetrics)
    is_active: bool = True


class PredictRequest(BaseModel):
    symbols: list[str]
    features: dict[str, list[float]]  # {factor_name: [values per symbol]}


class PredictResult(BaseModel):
    symbol: str
    score: float          # 0-100
    confidence: float     # 0-1
    model_name: str
