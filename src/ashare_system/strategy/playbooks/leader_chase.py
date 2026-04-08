"""龙头追板战法骨架。"""

NAME = "leader_chase"
ENTRY_WINDOW = "09:30-10:00"


def default_exit_params() -> dict:
    return {
        "max_hold_minutes": 240,
        "open_failure_minutes": 5,
        "time_stop": "14:50",
        "win_rate": 0.58,
        "pl_ratio": 2.0,
        "atr_pct": 0.015,
    }
