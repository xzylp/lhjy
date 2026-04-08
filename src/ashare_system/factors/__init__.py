"""因子引擎包 — 自动导入所有因子模块以触发注册"""

# 导入所有因子模块，触发 @registry.register 装饰器
from .base import price_volume, technical, financial, momentum  # noqa: F401
from .behavior import board_behavior, herd, overreaction, sector_linkage  # noqa: F401
from .macro import indicators  # noqa: F401
from .micro import orderbook, tick_features  # noqa: F401
from .alt import sentiment  # noqa: F401
from .chain import supply_demand  # noqa: F401

from .registry import registry, FactorRegistry
from .engine import FactorEngine
from .pipeline import FactorPipeline
from .validator import FactorValidator
from .selector import FactorSelector

__all__ = [
    "registry",
    "FactorRegistry",
    "FactorEngine",
    "FactorPipeline",
    "FactorValidator",
    "FactorSelector",
]
