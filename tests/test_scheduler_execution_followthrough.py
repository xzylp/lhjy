import unittest
from types import SimpleNamespace

from ashare_system.scheduler import _should_supervision_followthrough_execution_chain


class SchedulerExecutionFollowthroughTests(unittest.TestCase):
    def test_ready_cycle_without_dispatch_should_followthrough(self) -> None:
        cycle = SimpleNamespace(
            discussion_state="round_summarized",
            execution_pool_case_ids=["case-1"],
        )

        result = _should_supervision_followthrough_execution_chain(
            trade_date="2026-04-21",
            session_open=True,
            cycle=cycle,
            execution_dispatch={},
        )

        self.assertTrue(result)

    def test_active_round_should_not_followthrough(self) -> None:
        cycle = SimpleNamespace(
            discussion_state="round_2_running",
            execution_pool_case_ids=["case-1"],
        )

        result = _should_supervision_followthrough_execution_chain(
            trade_date="2026-04-21",
            session_open=True,
            cycle=cycle,
            execution_dispatch={},
        )

        self.assertFalse(result)

    def test_existing_same_day_dispatch_should_not_followthrough(self) -> None:
        cycle = SimpleNamespace(
            discussion_state="round_summarized",
            execution_pool_case_ids=["case-1"],
        )

        result = _should_supervision_followthrough_execution_chain(
            trade_date="2026-04-21",
            session_open=True,
            cycle=cycle,
            execution_dispatch={"trade_date": "2026-04-21", "status": "submitted"},
        )

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
