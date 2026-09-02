"""`Budget` -- the cost and wall-clock caps that stop a run before it can spend
unboundedly. `Runtime.timeout_ms` (Solari's rolling idle window) is explicitly NOT
a substitute for this: it resets on use, so the actual deadline lives here instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..config.schema import BudgetConfig
from ..models.base import Usage

MIN_STEP_TIMEOUT_S = 30


class BudgetExceeded(RuntimeError):
    """Raised when a cost or wall-clock cap has been (or would be) exceeded."""


class Budget:
    def __init__(self, cfg: BudgetConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._clock = clock
        self._start: float | None = None
        self._spent_usd = 0.0

    def start(self) -> None:
        self._start = self._clock()

    def charge(self, usage: Usage) -> None:
        self._spent_usd += usage.cost_usd

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def elapsed_s(self) -> float:
        if self._start is None:
            return 0.0
        return self._clock() - self._start

    def remaining_s(self) -> float | None:
        """None when uncapped."""
        if self.cfg.max_wall_clock_s is None:
            return None
        return self.cfg.max_wall_clock_s - self.elapsed_s

    def check(self, *, where: str = "") -> None:
        """Raise `BudgetExceeded` naming which cap tripped and the numbers."""
        label = f" at {where}" if where else ""
        if self.cfg.max_cost_usd is not None and self._spent_usd > self.cfg.max_cost_usd:
            raise BudgetExceeded(
                f"cost cap exceeded{label}: spent ${self._spent_usd:.4f} > "
                f"max_cost_usd=${self.cfg.max_cost_usd:.4f}"
            )
        remaining = self.remaining_s()
        if remaining is not None and remaining <= 0:
            raise BudgetExceeded(
                f"wall-clock cap exceeded{label}: elapsed {self.elapsed_s:.1f}s > "
                f"max_wall_clock_s={self.cfg.max_wall_clock_s}"
            )

    def step_timeout(self, requested: int) -> int:
        """`min(requested, remaining wall clock)` so a single step cannot outlive
        the cap; at least `MIN_STEP_TIMEOUT_S`, and raises `BudgetExceeded` when
        nothing is left.
        """
        remaining = self.remaining_s()
        if remaining is None:
            return requested
        if remaining <= 0:
            raise BudgetExceeded(
                f"no wall-clock budget remaining: elapsed {self.elapsed_s:.1f}s >= "
                f"max_wall_clock_s={self.cfg.max_wall_clock_s}"
            )
        return max(MIN_STEP_TIMEOUT_S, min(int(requested), int(remaining)))
