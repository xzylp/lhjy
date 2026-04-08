"""板块回流首板战法骨架。"""

NAME = "sector_reflow_first_board"
ENTRY_WINDOW = "09:45-10:30"


def default_exit_params() -> dict:
    return {
        "max_hold_minutes": 240,
        "open_failure_minutes": 10,
        "time_stop": "14:50",
        "win_rate": 0.52,
        "pl_ratio": 1.6,
        "atr_pct": 0.02,
    }
