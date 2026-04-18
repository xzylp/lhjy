import math
import numpy as np
from typing import List, Dict, Any

def calc_seal_quality(bid1_volume: float, last_price: float, avg_turnover_5d: float) -> float:
    """
    计算封单质量 (Limit-Up Seal Quality)
    
    逻辑：封单金额 / (日均成交额 * 0.1)
    A股特性：封单强度 > 10% 为强力封板，< 3% 为弱板风险。
    """
    if avg_turnover_5d <= 0:
        return 0.0
    seal_amount = bid1_volume * last_price
    # 假设 10% 的日均成交额作为基准对比
    quality = seal_amount / (avg_turnover_5d * 0.1)
    return round(float(quality), 4)

def calc_seal_velocity_decay(bid1_volumes: List[float], dt: int = 60) -> float:
    """
    计算封单动量衰减导数 (Seal Velocity Decay)
    
    逻辑：计算封单量的一阶导数。如果导数为负且斜率变陡，预警炸板。
    """
    if len(bid1_volumes) < 2:
        return 0.0
    # 计算一阶导数 (速度)
    velocity = (bid1_volumes[-1] - bid1_volumes[0]) / dt
    return round(float(velocity), 4)

def calc_sector_entropy(returns: List[float]) -> float:
    """
    计算板块共振熵 (Sector Resonance Entropy)
    
    逻辑：计算涨幅分布的香农熵。熵值越低，资金越集中，共振越强。
    """
    if not returns:
        return 1.0
    # 归一化处理，将涨幅映射到正数区间进行概率分布模拟
    # 假设 A股涨幅区间为 [-11%, 11%]
    shifted = [r + 0.11 for r in returns]
    total = sum(shifted)
    if total <= 0:
        return 1.0
    probs = [s / total for s in shifted]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    # 归一化到 [0, 1] 区间
    max_entropy = math.log2(len(returns)) if len(returns) > 1 else 1.0
    normalized_entropy = entropy / max_entropy
    return round(float(normalized_entropy), 4)

def calc_rank_distance(symbol_rank: int, sector_count: int, leader_status: Dict[str, Any]) -> float:
    """
    计算身位压制系数 (Rank Distance Suppression)
    
    逻辑：计算个股与板块龙一的身位距离及其风险系数。
    """
    if symbol_rank <= 1:
        return 0.0
    # 基础压制：距离越远，压制越强
    base_dist = (symbol_rank - 1) / max(sector_count, 1)
    # 龙一状态补偿：如果龙一炸板或走弱，风险系数非线性上升
    leader_weakness = 1.0 - float(leader_status.get("seal_quality", 1.0))
    suppression = base_dist + (leader_weakness * 2.0)
    return round(float(suppression), 4)

def calc_next_day_premium_expect(seal_time_minutes: int, seal_quality: float, regime: str) -> float:
    """
    计算 T+1 隔夜溢价期望 (Next Day Premium Expectation)
    
    逻辑：结合封板时间、质量和市场环境，计算次日开盘期望收益。
    A股特性：早封板（早于10:00）+ 高质量（>8%）在趋势市场（trend）下有极高正向溢价。
    """
    # 基础得分
    base_expect = 0.015  # 1.5% 基础溢价
    # 时间补偿：早封加分 (9:30 - 15:00 映射到 0 - 240分钟)
    time_factor = max(0, (120 - seal_time_minutes) / 120.0) * 0.02
    # 质量补偿
    quality_factor = (seal_quality - 1.0) * 0.01
    # 环境修正
    regime_map = {"trend": 1.2, "rotation": 0.8, "chaos": 0.2, "defensive": 0.5}
    modifier = regime_map.get(regime, 0.7)
    
    expect = (base_expect + time_factor + quality_factor) * modifier
    return round(float(expect), 4)
