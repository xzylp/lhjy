"""买入决策引擎 — 评分 → 排序 → 仓位 → 下单"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import MarketProfile, PlaybookContext, PositionPlan, Signal
from ..logging_config import get_logger
from .position_mgr import PositionManager, PositionInput

logger = get_logger("strategy.buy_decision")

BUY_SCORE_THRESHOLD = 60.0  # 买入最低评分 (基准0，非v1的50)


@dataclass
class BuyCandidate:
    symbol: str
    score: float
    position_plan: PositionPlan | None = None
    signals: list[Signal] = field(default_factory=list)


class BuyDecisionEngine:
    """买入决策引擎"""

    def __init__(self) -> None:
        self.position_mgr = PositionManager()

    def generate(
        self,
        candidates: list[str],
        scores: dict[str, float],
        account_equity: float,
        prices: dict[str, float],
        atrs: dict[str, float],
        profile: MarketProfile | None = None,
        win_rates: dict[str, float] | None = None,
        pl_ratios: dict[str, float] | None = None,
        top_n: int = 5,
        playbook_contexts: list[PlaybookContext] | None = None,
    ) -> list[BuyCandidate]:
        """生成买入候选列表"""
        playbook_map = {ctx.symbol: ctx for ctx in playbook_contexts or []}
        if playbook_map:
            qualified = [
                ctx.symbol
                for ctx in sorted(playbook_map.values(), key=lambda item: (item.confidence, -item.rank_in_sector), reverse=True)
            ][:top_n]
        else:
            qualified = [s for s in candidates if scores.get(s, 0) >= BUY_SCORE_THRESHOLD]
            qualified.sort(key=lambda s: scores.get(s, 0), reverse=True)
            qualified = qualified[:top_n]

        results: list[BuyCandidate] = []
        for symbol in qualified:
            price = prices.get(symbol, 0)
            atr = atrs.get(symbol, price * 0.02)
            if price <= 0:
                continue
            ctx = playbook_map.get(symbol)
            default_score = round(ctx.confidence * 100, 2) if ctx is not None else 0.0
            score = scores.get(symbol, default_score)
            inp = PositionInput(
                symbol=symbol,
                win_rate=(
                    win_rates.get(symbol, 0.55)
                    if win_rates else (
                        float(ctx.exit_params.get("win_rate", 0.55)) if ctx is not None else 0.55
                    )
                ),
                profit_loss_ratio=(
                    pl_ratios.get(symbol, 1.6)
                    if pl_ratios else (
                        float(ctx.exit_params.get("pl_ratio", 1.6)) if ctx is not None else 1.6
                    )
                ),
                atr=atr,
                price=price,
                account_equity=account_equity,
            )
            plan = self.position_mgr.calc(inp, profile)
            signals = []
            if ctx is not None:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action="BUY",
                        strength=score,
                        confidence=ctx.confidence,
                        source_strategy=ctx.playbook,
                    )
                )
            results.append(
                BuyCandidate(
                    symbol=symbol,
                    score=score,
                    position_plan=plan,
                    signals=signals,
                )
            )
            logger.info("买入候选: %s 评分=%.1f 目标仓位=%.1f%%", symbol, score, plan.final_ratio * 100)
        return results
