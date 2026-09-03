"""`Budget` -- cost and wall-clock caps."""

from __future__ import annotations

import pytest

from ticketbot.config.schema import BudgetConfig
from ticketbot.engine.budget import MIN_STEP_TIMEOUT_S, Budget, BudgetExceeded
from ticketbot.models.base import Usage


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_charge_accumulates() -> None:
    budget = Budget(BudgetConfig())
    budget.start()
    budget.charge(Usage(cost_usd=1.5))
    budget.charge(Usage(cost_usd=2.25))
    assert budget.spent_usd == pytest.approx(3.75)


def test_check_raises_past_cost_cap() -> None:
    budget = Budget(BudgetConfig(max_cost_usd=5.0))
    budget.start()
    budget.charge(Usage(cost_usd=5.01))
    with pytest.raises(BudgetExceeded, match="cost cap"):
        budget.check(where="verify")


def test_check_does_not_raise_at_or_under_cost_cap() -> None:
    budget = Budget(BudgetConfig(max_cost_usd=5.0))
    budget.start()
    budget.charge(Usage(cost_usd=5.0))
    budget.check()  # must not raise


def test_check_raises_past_wall_clock_cap_with_injected_clock() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(max_wall_clock_s=100), clock=clock)
    budget.start()
    clock.advance(101)
    with pytest.raises(BudgetExceeded, match="wall-clock cap"):
        budget.check(where="plan")


def test_check_does_not_raise_before_wall_clock_cap() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(max_wall_clock_s=100), clock=clock)
    budget.start()
    clock.advance(50)
    budget.check()  # must not raise


def test_uncapped_budget_never_raises() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(), clock=clock)
    budget.start()
    budget.charge(Usage(cost_usd=1_000_000.0))
    clock.advance(10_000_000)
    budget.check()  # must not raise
    assert budget.remaining_s() is None


def test_step_timeout_shrinks_to_remaining_wall_clock() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(max_wall_clock_s=100), clock=clock)
    budget.start()
    clock.advance(60)  # 40s remaining
    assert budget.step_timeout(1800) == 40


def test_step_timeout_uncapped_returns_requested() -> None:
    budget = Budget(BudgetConfig())
    budget.start()
    assert budget.step_timeout(1800) == 1800


def test_step_timeout_never_below_minimum() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(max_wall_clock_s=100), clock=clock)
    budget.start()
    clock.advance(90)  # 10s remaining, below the 30s floor
    assert budget.step_timeout(1800) == MIN_STEP_TIMEOUT_S


def test_step_timeout_raises_when_nothing_remains() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(max_wall_clock_s=100), clock=clock)
    budget.start()
    clock.advance(100)
    with pytest.raises(BudgetExceeded):
        budget.step_timeout(1800)


def test_step_timeout_respects_requested_when_it_is_the_smaller_value() -> None:
    clock = _FakeClock()
    budget = Budget(BudgetConfig(max_wall_clock_s=1000), clock=clock)
    budget.start()
    assert budget.step_timeout(45) == 45


def test_elapsed_s_before_start_is_zero() -> None:
    budget = Budget(BudgetConfig())
    assert budget.elapsed_s == 0.0
