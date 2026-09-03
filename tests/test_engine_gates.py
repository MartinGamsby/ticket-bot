"""`Gates` -- the human-in-the-loop decision points."""

from __future__ import annotations

import pytest

from ticketbot.config.schema import GatesConfig
from ticketbot.core.run import Run
from ticketbot.engine.gates import Gates
from ticketbot.engine.pipeline import StepDef


def _run() -> Run:
    return Run(id="r1", profile_name="p", work_item_key="ITEM-1")


def test_on_unclear_comment_and_unassign() -> None:
    gates = Gates(GatesConfig(on_unclear="comment_and_unassign", max_clarify_rounds=2))
    run = _run()
    decision = gates.on_unclear(run, "QUESTION: which auth flow?")
    assert decision.action == "block"
    assert decision.comment == "QUESTION: which auth flow?"
    assert decision.unassign is True
    assert run.extra["clarify_rounds"] == 1


def test_on_unclear_comment_only() -> None:
    gates = Gates(GatesConfig(on_unclear="comment_only", max_clarify_rounds=2))
    decision = gates.on_unclear(_run(), "QUESTION: ?")
    assert decision.action == "block"
    assert decision.comment == "QUESTION: ?"
    assert decision.unassign is False


def test_on_unclear_proceed() -> None:
    gates = Gates(GatesConfig(on_unclear="proceed", max_clarify_rounds=2))
    decision = gates.on_unclear(_run(), "QUESTION: ?")
    assert decision.action == "continue"


def test_on_unclear_fail() -> None:
    gates = Gates(GatesConfig(on_unclear="fail", max_clarify_rounds=2))
    decision = gates.on_unclear(_run(), "QUESTION: ?")
    assert decision.action == "fail"


def test_on_unclear_max_clarify_rounds_cutover_to_fail_regardless_of_mode() -> None:
    gates = Gates(GatesConfig(on_unclear="comment_and_unassign", max_clarify_rounds=2))
    run = _run()
    first = gates.on_unclear(run, "QUESTION: a?")
    assert first.action == "block"
    second = gates.on_unclear(run, "QUESTION: b?")
    assert second.action == "block"
    third = gates.on_unclear(run, "QUESTION: c?")
    assert third.action == "fail"
    assert "limit" in third.comment.lower()
    assert run.extra["clarify_rounds"] == 3


def test_on_unclear_increments_rounds_across_calls() -> None:
    gates = Gates(GatesConfig(max_clarify_rounds=5))
    run = _run()
    gates.on_unclear(run, "QUESTION: 1")
    gates.on_unclear(run, "QUESTION: 2")
    assert run.extra["clarify_rounds"] == 2


def test_on_pr_ready_human_review_awaits_human() -> None:
    gates = Gates(GatesConfig(on_pr_ready="human_review"))
    decision = gates.on_pr_ready(_run())
    assert decision.action == "await_human"


def test_on_pr_ready_auto_continues() -> None:
    gates = Gates(GatesConfig(on_pr_ready="auto"))
    decision = gates.on_pr_ready(_run())
    assert decision.action == "continue"


def test_on_step_gate_human_always_awaits() -> None:
    gates = Gates(GatesConfig())
    step = StepDef(id="plan", role="planner", gate="human")
    assert gates.on_step_gate(step, _run(), interactive=False).action == "await_human"
    assert gates.on_step_gate(step, _run(), interactive=True).action == "await_human"


def test_on_step_gate_optional_human_respects_interactive() -> None:
    gates = Gates(GatesConfig())
    step = StepDef(id="plan", role="planner", gate="optional_human")
    assert gates.on_step_gate(step, _run(), interactive=False).action == "continue"
    assert gates.on_step_gate(step, _run(), interactive=True).action == "await_human"


def test_on_step_gate_no_gate_always_continues() -> None:
    gates = Gates(GatesConfig())
    step = StepDef(id="plan", role="planner")
    assert gates.on_step_gate(step, _run(), interactive=True).action == "continue"
    assert gates.on_step_gate(step, _run(), interactive=False).action == "continue"
