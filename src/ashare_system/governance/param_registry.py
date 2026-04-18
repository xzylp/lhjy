"""内置参数注册表。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ParamLayer = Literal["system_defaults", "market_regime_params", "agent_adjusted_params"]
ValueType = Literal["integer", "number", "percent", "time", "string", "boolean"]
EffectivePeriod = Literal["today_session", "next_trading_day", "until_revoked"]


class ParameterDefinition(BaseModel):
    param_key: str
    scope: str
    value_type: ValueType
    default_value: int | float | str | bool
    allowed_range: list[int | float | str] = Field(default_factory=list)
    effective_period_default: EffectivePeriod
    proposed_by: list[str] = Field(default_factory=list)
    approved_by: str = "ashare-audit"
    notes: str = ""


class ParameterRegistry:
    """系统动态参数注册表。"""

    def __init__(self, definitions: list[ParameterDefinition] | None = None) -> None:
        self._definitions = definitions or _default_definitions()
        self._by_key = {item.param_key: item for item in self._definitions}

    def list(self) -> list[ParameterDefinition]:
        return list(self._definitions)

    def get(self, param_key: str) -> ParameterDefinition | None:
        return self._by_key.get(param_key)


def _default_definitions() -> list[ParameterDefinition]:
    return [
        ParameterDefinition(
            param_key="base_pool_capacity",
            scope="runtime",
            value_type="integer",
            default_value=30,
            allowed_range=[10, 50],
            effective_period_default="next_trading_day",
            proposed_by=["ashare", "user"],
            notes="Daily base pool must always exist.",
        ),
        ParameterDefinition(
            param_key="focus_pool_capacity",
            scope="strategy",
            value_type="integer",
            default_value=15,
            allowed_range=[5, 20],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="watchlist_capacity",
            scope="strategy",
            value_type="integer",
            default_value=8,
            allowed_range=[3, 12],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy", "ashare", "user"],
        ),
        ParameterDefinition(
            param_key="execution_pool_capacity",
            scope="execution",
            value_type="integer",
            default_value=3,
            allowed_range=[1, 3],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk"],
            notes="Hard cap remains 3.",
        ),
        ParameterDefinition(
            param_key="monitor_heartbeat_save_seconds",
            scope="monitor",
            value_type="integer",
            default_value=600,
            allowed_range=[60, 3600],
            effective_period_default="today_session",
            proposed_by=["ashare", "ashare-risk", "user"],
            notes="盘中心跳保存频率，可通过自然语言调节。",
        ),
        ParameterDefinition(
            param_key="monitor_auction_heartbeat_save_seconds",
            scope="monitor",
            value_type="integer",
            default_value=300,
            allowed_range=[30, 1800],
            effective_period_default="today_session",
            proposed_by=["ashare", "ashare-risk", "user"],
            notes="开盘/尾盘心跳保存频率，可通过自然语言调节。",
        ),
        ParameterDefinition(
            param_key="candidate_poll_seconds",
            scope="monitor",
            value_type="integer",
            default_value=300,
            allowed_range=[30, 1800],
            effective_period_default="today_session",
            proposed_by=["ashare", "ashare-risk", "user"],
        ),
        ParameterDefinition(
            param_key="focus_poll_seconds",
            scope="monitor",
            value_type="integer",
            default_value=60,
            allowed_range=[10, 600],
            effective_period_default="today_session",
            proposed_by=["ashare", "ashare-risk", "user"],
        ),
        ParameterDefinition(
            param_key="execution_poll_seconds",
            scope="monitor",
            value_type="integer",
            default_value=30,
            allowed_range=[5, 300],
            effective_period_default="today_session",
            proposed_by=["ashare", "ashare-risk", "user"],
        ),
        ParameterDefinition(
            param_key="dossier_retention_trading_days",
            scope="storage",
            value_type="integer",
            default_value=5,
            allowed_range=[1, 20],
            effective_period_default="next_trading_day",
            proposed_by=["ashare", "ashare-audit", "user"],
        ),
        ParameterDefinition(
            param_key="archive_retention_trading_days",
            scope="storage",
            value_type="integer",
            default_value=20,
            allowed_range=[5, 90],
            effective_period_default="next_trading_day",
            proposed_by=["ashare", "ashare-audit", "user"],
        ),
        ParameterDefinition(
            param_key="trend_follow_through_ratio",
            scope="market",
            value_type="percent",
            default_value=0.55,
            allowed_range=[0.40, 0.75],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy", "ashare-risk"],
        ),
        ParameterDefinition(
            param_key="rotation_switch_frequency",
            scope="market",
            value_type="integer",
            default_value=2,
            allowed_range=[1, 5],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="risk_off_drawdown_threshold",
            scope="risk",
            value_type="percent",
            default_value=0.045,
            allowed_range=[0.02, 0.08],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="momentum_weight",
            scope="strategy",
            value_type="number",
            default_value=0.25,
            allowed_range=[0.0, 0.60],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="reversion_weight",
            scope="strategy",
            value_type="number",
            default_value=0.20,
            allowed_range=[0.0, 0.60],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="breakout_weight",
            scope="strategy",
            value_type="number",
            default_value=0.20,
            allowed_range=[0.0, 0.60],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="event_driven_weight",
            scope="strategy",
            value_type="number",
            default_value=0.15,
            allowed_range=[0.0, 0.50],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy", "ashare-research"],
        ),
        ParameterDefinition(
            param_key="sector_theme_rotation_weight",
            scope="strategy",
            value_type="number",
            default_value=0.20,
            allowed_range=[0.0, 0.60],
            effective_period_default="next_trading_day",
            proposed_by=["ashare-strategy", "ashare-research"],
        ),
        ParameterDefinition(
            param_key="excluded_theme_keywords",
            scope="strategy",
            value_type="string",
            default_value="",
            effective_period_default="until_revoked",
            proposed_by=["ashare-strategy", "ashare", "user"],
            notes="逗号分隔的排除方向关键词，如 银行,白酒；运行时选股与执行预检共同生效。",
        ),
        ParameterDefinition(
            param_key="t_min_amplitude",
            scope="intraday",
            value_type="percent",
            default_value=0.015,
            allowed_range=[0.005, 0.04],
            effective_period_default="today_session",
            proposed_by=["ashare-strategy", "ashare-risk"],
        ),
        ParameterDefinition(
            param_key="t_max_amplitude",
            scope="intraday",
            value_type="percent",
            default_value=0.08,
            allowed_range=[0.04, 0.15],
            effective_period_default="today_session",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="t_take_profit_1",
            scope="intraday",
            value_type="percent",
            default_value=0.025,
            allowed_range=[0.01, 0.05],
            effective_period_default="today_session",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="t_take_profit_2",
            scope="intraday",
            value_type="percent",
            default_value=0.04,
            allowed_range=[0.02, 0.08],
            effective_period_default="today_session",
            proposed_by=["ashare-strategy"],
        ),
        ParameterDefinition(
            param_key="t_stop_loss_soft",
            scope="intraday",
            value_type="percent",
            default_value=-0.015,
            allowed_range=[-0.05, -0.005],
            effective_period_default="today_session",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="t_stop_loss_hard",
            scope="intraday",
            value_type="percent",
            default_value=-0.02,
            allowed_range=[-0.08, -0.01],
            effective_period_default="today_session",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="tail_open_cutoff",
            scope="intraday",
            value_type="time",
            default_value="14:30",
            allowed_range=["14:00", "14:50"],
            effective_period_default="today_session",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="tail_exit_cutoff",
            scope="intraday",
            value_type="time",
            default_value="14:55",
            allowed_range=["14:30", "14:59"],
            effective_period_default="today_session",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="max_total_position",
            scope="risk",
            value_type="percent",
            default_value=0.80,
            allowed_range=[0.20, 1.00],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="equity_position_limit",
            scope="risk",
            value_type="percent",
            default_value=0.20,
            allowed_range=[0.05, 0.50],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk", "ashare", "user"],
            notes="测试阶段股票仓位上限，逆回购等类现金仓不计入该上限。",
        ),
        ParameterDefinition(
            param_key="max_single_position",
            scope="risk",
            value_type="percent",
            default_value=0.25,
            allowed_range=[0.05, 0.40],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="max_single_amount",
            scope="risk",
            value_type="number",
            default_value=50000.0,
            allowed_range=[5000, 500000],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk", "ashare", "user"],
        ),
        ParameterDefinition(
            param_key="daily_loss_limit",
            scope="risk",
            value_type="percent",
            default_value=0.05,
            allowed_range=[0.01, 0.10],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk"],
        ),
        ParameterDefinition(
            param_key="reverse_repo_target_ratio",
            scope="risk",
            value_type="percent",
            default_value=0.70,
            allowed_range=[0.00, 0.95],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk", "ashare", "user"],
            notes="逆回购目标占比，到期后优先补回该目标。",
        ),
        ParameterDefinition(
            param_key="minimum_total_invested_amount",
            scope="risk",
            value_type="number",
            default_value=100000.0,
            allowed_range=[10000, 5000000],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk", "ashare", "user"],
            notes="测试期总持仓基线，现阶段默认维持不低于10万元。",
        ),
        ParameterDefinition(
            param_key="reverse_repo_reserved_amount",
            scope="risk",
            value_type="number",
            default_value=70000.0,
            allowed_range=[0, 5000000],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk", "ashare", "user"],
            notes="测试期逆回购保留金额，现阶段默认保留7万元。",
        ),
        ParameterDefinition(
            param_key="sector_exposure_limit",
            scope="risk",
            value_type="percent",
            default_value=0.40,
            allowed_range=[0.10, 0.60],
            effective_period_default="until_revoked",
            proposed_by=["ashare-risk"],
        ),
    ]
