"""买入决策引擎 — 评分 → 排序 → 仓位 → 下单"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import MarketProfile, PlaybookContext, PositionPlan, Signal
from ..logging_config import get_logger
from .position_mgr import PositionManager, PositionInput

logger = get_logger("strategy.buy_decision")

BUY_SCORE_THRESHOLD = 60.0  # 买入最低评分 (基准0，非v1的50)


def _ctx_get(ctx: PlaybookContext | dict, key: str, default=None):
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


def _ctx_symbol(ctx: PlaybookContext | dict) -> str:
    return str(_ctx_get(ctx, "symbol", "") or "")


def _ctx_exit_params(ctx: PlaybookContext | dict) -> dict:
    payload = _ctx_get(ctx, "exit_params", {}) or {}
    return payload if isinstance(payload, dict) else {}


def _resolve_playbook_match_payload(ctx: PlaybookContext | dict) -> dict[str, float | bool]:
    payload = _ctx_get(ctx, "playbook_match_score")
    if payload is None:
        return {"available": False, "qualified": False, "score": 0.0}
    if isinstance(payload, dict):
        score = float(payload.get("score", 0.0) or 0.0)
        qualified = bool(payload.get("qualified", score > 0.0))
        return {"available": True, "qualified": qualified, "score": score}
    score = float(getattr(payload, "score", payload) or 0.0)
    qualified = bool(getattr(payload, "qualified", score > 0.0))
    return {"available": True, "qualified": qualified, "score": score}


def _playbook_sort_key(ctx: PlaybookContext | dict, scores: dict[str, float]) -> tuple[float, float, float, int, float]:
    symbol = _ctx_symbol(ctx)
    match_payload = _resolve_playbook_match_payload(ctx)
    confidence = float(_ctx_get(ctx, "confidence", 0.0) or 0.0)
    leader_score = float(_ctx_get(ctx, "leader_score", 0.0) or 0.0)
    rank_in_sector = int(_ctx_get(ctx, "rank_in_sector", 99) or 99)
    selection_score = float(scores.get(symbol, 0.0) or 0.0)
    return (
        float(match_payload["score"]),
        confidence,
        leader_score,
        -rank_in_sector,
        selection_score,
    )


def _resolve_nightly_priority_boosts(
    nightly_priorities: list[str] | dict[str, float] | None,
) -> dict[str, float]:
    if nightly_priorities is None:
        return {}
    if isinstance(nightly_priorities, dict):
        boosts: dict[str, float] = {}
        for symbol, value in nightly_priorities.items():
            try:
                boosts[str(symbol)] = float(value)
            except (TypeError, ValueError):
                boosts[str(symbol)] = 12.0
        return boosts
    return {str(symbol): 12.0 for symbol in nightly_priorities if str(symbol)}


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
        auction_signals: dict | None = None,
        nightly_priorities: list[str] | dict[str, float] | None = None,
    ) -> list[BuyCandidate]:
        """生成买入候选列表。

        Args:
            auction_signals: {symbol: AuctionSignal} 竞价研判结果。
                PROMOTE 的票评分 +20，KILL 的票直接过滤。
                无此参数时向后兼容。
            nightly_priorities: 夜间沙盘输出的次日优先标的。
                若命中，则在正式排序前追加优先级加分。
        """
        nightly_boosts = _resolve_nightly_priority_boosts(nightly_priorities)
        for symbol, boost in nightly_boosts.items():
            if symbol in candidates:
                scores[symbol] = scores.get(symbol, 0.0) + boost
        if nightly_boosts:
            logger.info("夜间沙盘优先级加分: %s", nightly_boosts)

        # v1.0: 竞价淘汰 — 先过滤 KILL 信号
        effective_candidates = list(candidates)
        if auction_signals:
            killed = {s for s, sig in auction_signals.items()
                      if getattr(sig, "action", None) == "KILL" or (isinstance(sig, dict) and sig.get("action") == "KILL")}
            if killed:
                effective_candidates = [s for s in effective_candidates if s not in killed]
                logger.info("竞价淘汰: %s", killed)

        playbook_map = {_ctx_symbol(ctx): ctx for ctx in playbook_contexts or [] if _ctx_symbol(ctx)}
        if playbook_map:
            contexts = list(playbook_map.values())
            matched_contexts = [ctx for ctx in contexts if _resolve_playbook_match_payload(ctx)["available"]]
            qualified_contexts = [ctx for ctx in matched_contexts if _resolve_playbook_match_payload(ctx)["qualified"]]
            preferred_contexts = qualified_contexts or matched_contexts or contexts
            qualified = [_ctx_symbol(ctx) for ctx in sorted(preferred_contexts, key=lambda item: _playbook_sort_key(item, scores), reverse=True)][:top_n]
            # v1.0: 过滤 KILL
            qualified = [s for s in qualified if s not in (killed if auction_signals else set())]
        else:
            qualified = [s for s in effective_candidates if scores.get(s, 0) >= BUY_SCORE_THRESHOLD]
            qualified.sort(key=lambda s: scores.get(s, 0), reverse=True)
            qualified = qualified[:top_n]

        # v1.0: 竞价晋升 — PROMOTE 的票评分 +20
        if auction_signals:
            for symbol in qualified:
                sig = auction_signals.get(symbol)
                action = getattr(sig, "action", None) if sig else None
                if action is None and isinstance(sig, dict):
                    action = sig.get("action")
                if action == "PROMOTE":
                    scores[symbol] = scores.get(symbol, 0) + 20.0

        results: list[BuyCandidate] = []
        for symbol in qualified:
            price = prices.get(symbol, 0)
            atr = atrs.get(symbol, price * 0.02)
            if price <= 0:
                continue
            ctx = playbook_map.get(symbol)
            confidence = float(_ctx_get(ctx, "confidence", 0.0) or 0.0) if ctx is not None else 0.0
            default_score = round(confidence * 100, 2) if ctx is not None else 0.0
            match_payload = _resolve_playbook_match_payload(ctx) if ctx is not None else {"available": False, "qualified": False, "score": 0.0}
            if match_payload["available"]:
                score = float(match_payload["score"] or 0.0) or scores.get(symbol, default_score)
            else:
                score = scores.get(symbol, default_score)
            exit_params = _ctx_exit_params(ctx) if ctx is not None else {}
            inp = PositionInput(
                symbol=symbol,
                win_rate=(
                    win_rates.get(symbol, 0.55)
                    if win_rates else (
                        float(exit_params.get("win_rate", 0.55)) if ctx is not None else 0.55
                    )
                ),
                profit_loss_ratio=(
                    pl_ratios.get(symbol, 1.6)
                    if pl_ratios else (
                        float(exit_params.get("pl_ratio", 1.6)) if ctx is not None else 1.6
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
                        confidence=confidence,
                        source_strategy=str(_ctx_get(ctx, "playbook", "")),
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
