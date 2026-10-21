"""Iteration budget.

Caps how many model turns a single request may consume. 60 is enough for a
12-panel dashboard report (one plan, ~2 calls per panel, narrative, plus
repair attempts) and low enough to stop a runaway loop.

The budget is per-request, not per-process: a fresh user message gets a fresh
budget, so a long-lived session cannot be starved by one bad turn.
"""

from __future__ import annotations

from ..errors import BudgetExhausted


class IterationBudget:
    def __init__(self, max_iterations: int = 60):
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        self.max_iterations = max_iterations
        self._used = 0

    def consume(self, n: int = 1) -> None:
        """Consume budget. Raises :class:`BudgetExhausted` when spent."""
        if self._used >= self.max_iterations:
            raise BudgetExhausted(
                f"iteration budget exhausted after {self._used} iterations",
                details={"max_iterations": self.max_iterations},
            )
        self._used += n

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.max_iterations - self._used)

    @property
    def exhausted(self) -> bool:
        return self._used >= self.max_iterations

    def reset(self) -> None:
        self._used = 0

    def to_dict(self) -> dict:
        return {
            "used": self._used,
            "max_iterations": self.max_iterations,
            "remaining": self.remaining,
        }
