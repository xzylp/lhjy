"""持仓分类与资金性质识别。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import PositionSnapshot

_REVERSE_REPO_PREFIXES = ("204", "1318")
_SH_REVERSE_REPO_PREFIXES = ("204",)
_SZ_REVERSE_REPO_PREFIXES = ("1318",)
REVERSE_REPO_UNIT_NOTIONAL = 1000.0


def _position_market_value(item: PositionSnapshot) -> float:
    return float(item.quantity) * float(item.last_price)


def is_reverse_repo_symbol(symbol: str) -> bool:
    code = str(symbol or "").split(".", 1)[0]
    return any(code.startswith(prefix) for prefix in _REVERSE_REPO_PREFIXES)


def is_sh_reverse_repo_symbol(symbol: str) -> bool:
    code = str(symbol or "").split(".", 1)[0]
    return any(code.startswith(prefix) for prefix in _SH_REVERSE_REPO_PREFIXES)


def is_sz_reverse_repo_symbol(symbol: str) -> bool:
    code = str(symbol or "").split(".", 1)[0]
    return any(code.startswith(prefix) for prefix in _SZ_REVERSE_REPO_PREFIXES)


def reverse_repo_volume_step(symbol: str) -> int:
    if is_sh_reverse_repo_symbol(symbol):
        return 100
    if is_sz_reverse_repo_symbol(symbol):
        return 1
    raise ValueError(f"unsupported reverse repo symbol: {symbol}")


def reverse_repo_min_volume(symbol: str) -> int:
    return reverse_repo_volume_step(symbol)


def reverse_repo_order_amount(symbol: str, quantity: int) -> float:
    if not is_reverse_repo_symbol(symbol):
        raise ValueError(f"unsupported reverse repo symbol: {symbol}")
    return float(quantity) * REVERSE_REPO_UNIT_NOTIONAL


def reverse_repo_volume_for_amount(symbol: str, amount: float) -> int:
    if not is_reverse_repo_symbol(symbol):
        raise ValueError(f"unsupported reverse repo symbol: {symbol}")
    step = reverse_repo_volume_step(symbol)
    raw_volume = int(float(amount) // REVERSE_REPO_UNIT_NOTIONAL)
    return (raw_volume // step) * step


@dataclass
class PositionBucketSummary:
    equity_positions: list[PositionSnapshot] = field(default_factory=list)
    reverse_repo_positions: list[PositionSnapshot] = field(default_factory=list)
    equity_value: float = 0.0
    reverse_repo_value: float = 0.0

    @property
    def total_value(self) -> float:
        return self.equity_value + self.reverse_repo_value


@dataclass
class TestTradingBudget:
    minimum_total_invested_amount: float
    reverse_repo_reserved_amount: float
    stock_test_budget_amount: float
    stock_test_budget_remaining: float
    reverse_repo_gap_value: float

    @property
    def has_reverse_repo_gap(self) -> bool:
        return self.reverse_repo_gap_value > 1e-6


def summarize_position_buckets(positions: list[PositionSnapshot]) -> PositionBucketSummary:
    summary = PositionBucketSummary()
    for item in positions:
        market_value = _position_market_value(item)
        if is_reverse_repo_symbol(item.symbol):
            summary.reverse_repo_positions.append(item)
            summary.reverse_repo_value += market_value
        else:
            summary.equity_positions.append(item)
            summary.equity_value += market_value
    return summary


def build_test_trading_budget(
    equity_value: float,
    reverse_repo_value: float,
    minimum_total_invested_amount: float,
    reverse_repo_reserved_amount: float,
) -> TestTradingBudget:
    stock_test_budget_amount = max(float(minimum_total_invested_amount) - float(reverse_repo_reserved_amount), 0.0)
    stock_test_budget_remaining = max(stock_test_budget_amount - float(equity_value), 0.0)
    reverse_repo_gap_value = max(float(reverse_repo_reserved_amount) - float(reverse_repo_value), 0.0)
    return TestTradingBudget(
        minimum_total_invested_amount=float(minimum_total_invested_amount),
        reverse_repo_reserved_amount=float(reverse_repo_reserved_amount),
        stock_test_budget_amount=round(stock_test_budget_amount, 4),
        stock_test_budget_remaining=round(stock_test_budget_remaining, 4),
        reverse_repo_gap_value=round(reverse_repo_gap_value, 4),
    )
