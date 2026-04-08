"""分歧回封战法骨架。"""

NAME = "divergence_reseal"
ENTRY_WINDOW = "10:00-14:00"


def default_exit_params() -> dict:
    return {
        "max_hold_minutes": 480,
        "open_failure_minutes": 30,
        "time_stop": "14:50",
        "win_rate": 0.55,
        "pl_ratio": 1.8,
        "atr_pct": 0.02,
    }
