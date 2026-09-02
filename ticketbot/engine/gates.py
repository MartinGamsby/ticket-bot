"""`Gates` -- the human-in-the-loop decision points: what happens when a step is
unclear (`on_unclear`), when a PR is ready (`on_pr_ready`), and when a step declares
`gate: human` / `gate: optional_human`.

None of these ever decide to merge anything -- `on_pr_ready`'s `'auto'` mode still
only ever means "don't pause the run", never "merge the PR". That decision (and the
"never merge" guarantee) lives entirely in the `repo` adapter (section 7), which has
no merge call anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.schema import GatesConfig
from ..core.run import Run
from .pipeline import StepDef


@dataclass
class GateDecision:
    action: str  # 'continue' | 'block' | 'fail' | 'await_human'
    comment: str = ""  # markdown to post
    transition: str = ""  # target state, '' for none
    unassign: bool = False


class Gates:
    def __init__(self, cfg: GatesConfig) -> None:
        self.cfg = cfg

    def on_unclear(self, run: Run, question: str) -> GateDecision:
        """Increments `run.extra['clarify_rounds']`; once it exceeds
        `cfg.max_clarify_rounds` the decision is 'fail' regardless of `on_unclear`.
        """
        rounds = int(run.extra.get("clarify_rounds", 0)) + 1
        run.extra["clarify_rounds"] = rounds

        if rounds > self.cfg.max_clarify_rounds:
            return GateDecision(
                action="fail",
                comment=(
                    f"Clarification limit reached ({self.cfg.max_clarify_rounds} round(s)); "
                    "stopping instead of asking again."
                ),
            )

        mode = self.cfg.on_unclear
        if mode == "comment_and_unassign":
            return GateDecision(action="block", comment=question, unassign=True)
        if mode == "comment_only":
            return GateDecision(action="block", comment=question)
        if mode == "proceed":
            return GateDecision(action="continue")
        if mode == "fail":
            return GateDecision(action="fail")
        raise ValueError(f"unknown gates.on_unclear mode: {mode!r}")

    def on_pr_ready(self, run: Run) -> GateDecision:
        """'human_review' -> await_human (open the PR as a DRAFT, never merge).
        'auto' -> continue (still never merges)."""
        if self.cfg.on_pr_ready == "human_review":
            return GateDecision(action="await_human")
        return GateDecision(action="continue")

    def on_step_gate(self, step: StepDef, run: Run, *, interactive: bool) -> GateDecision:
        """`step.gate == 'human'` -> await_human always. `step.gate ==
        'optional_human'` -> await_human only when `interactive` is True (i.e.
        `run --pause-at <step-id>`); otherwise continue."""
        if step.gate == "human":
            return GateDecision(action="await_human")
        if step.gate == "optional_human":
            return GateDecision(action="await_human" if interactive else "continue")
        return GateDecision(action="continue")
