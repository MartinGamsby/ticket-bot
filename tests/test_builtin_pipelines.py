"""`builtin/pipelines/{standard,small-bug,large-with-clarification}.yaml`: they all
load through the real `PipelineDef.load("builtin:...")` path, have the right shape,
every `role:` maps to a shipped role prompt, every tool name is a real one, and
every `when:` actually evaluates -- not just parses at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticketbot.config.loader import builtin_root
from ticketbot.core.predicate import evaluate_any
from ticketbot.engine.pipeline import PipelineDef
from ticketbot.executors.tools import CATALOGUE

# Orchestrator-handled tool names: implemented from a step's returned text /
# adapters, never routed through executors.tools.CATALOGUE -- see
# engine/orchestrator.py (sink.*, repo.open_pr) and executors/tools.py's own
# `_SINK_ONLY_NAMES` comment.
ORCHESTRATOR_HANDLED_TOOLS = {"sink.comment", "sink.unassign", "sink.transition", "repo.open_pr"}
ALLOWED_TOOLS = set(CATALOGUE) | ORCHESTRATOR_HANDLED_TOOLS

PIPELINE_NAMES = ["standard", "small-bug", "large-with-clarification"]
ROLES_DIR = builtin_root() / "prompts" / "roles"

# A representative `when:` evaluation context -- the shape `engine.context.
# build_context()` produces (workitem mirrored at top level, plus plan/diff/step).
REPRESENTATIVE_CONTEXT = {
    "workitem": {"acceptance": "- returns 200", "ambiguity": "low"},
    "acceptance": "- returns 200",
    "ambiguity": "low",
    "plan": {"security": "no", "sections": 1},
    "diff": {"touches_security": False, "files": 2},
    "step": {},
}


def _load(name: str, tmp_path: Path) -> PipelineDef:
    return PipelineDef.load(f"builtin:pipelines/{name}.yaml", tmp_path)


@pytest.mark.parametrize("name", PIPELINE_NAMES)
def test_pipeline_loads_through_the_builtin_scheme(name: str, tmp_path: Path) -> None:
    pipeline = _load(name, tmp_path)
    assert pipeline.name == name
    assert pipeline.steps


def test_standard_has_the_eight_steps_in_order(tmp_path: Path) -> None:
    pipeline = _load("standard", tmp_path)
    assert [s.id for s in pipeline.steps] == [
        "intake", "clarify", "plan", "implement", "verify", "review", "security", "publish",
    ]


def test_small_bug_has_five_steps_and_drops_clarify_and_security(tmp_path: Path) -> None:
    pipeline = _load("small-bug", tmp_path)
    ids = [s.id for s in pipeline.steps]
    assert len(ids) == 5
    assert "clarify" not in ids
    assert "security" not in ids


def test_large_with_clarification_has_a_research_step_and_mandatory_plan_gate(tmp_path: Path) -> None:
    pipeline = _load("large-with-clarification", tmp_path)
    ids = [s.id for s in pipeline.steps]
    assert "research" in ids
    assert pipeline.step("plan").gate == "human"
    # clarify is unconditional in this pipeline (no `when:`), unlike standard's.
    assert pipeline.step("clarify").when is None


@pytest.mark.parametrize("name", PIPELINE_NAMES)
def test_every_role_has_a_shipped_prompt_file(name: str, tmp_path: Path) -> None:
    pipeline = _load(name, tmp_path)
    for step in pipeline.steps:
        prompt_path = ROLES_DIR / f"{step.role}.md"
        assert prompt_path.is_file(), f"{name}.yaml step {step.id!r}: no {prompt_path}"


@pytest.mark.parametrize("name", PIPELINE_NAMES)
def test_every_tool_name_is_known(name: str, tmp_path: Path) -> None:
    pipeline = _load(name, tmp_path)
    for step in pipeline.steps:
        unknown = set(step.tools) - ALLOWED_TOOLS
        assert not unknown, f"{name}.yaml step {step.id!r} uses unknown tool(s): {unknown}"


@pytest.mark.parametrize("name", PIPELINE_NAMES)
def test_every_when_evaluates_against_a_representative_context(name: str, tmp_path: Path) -> None:
    pipeline = _load(name, tmp_path)
    for step in pipeline.steps:
        if step.when is None:
            continue
        # Must not raise -- the boolean result itself is not asserted, since it
        # legitimately varies by step (clarify's `when` is meant to be False here).
        evaluate_any(step.when, REPRESENTATIVE_CONTEXT)


def test_security_tool_allowlists_never_grant_shell_run() -> None:
    """The tool allowlists are a security control (see section-9 security notes):
    clarifier gets no filesystem tools; reviewer and security get read+edit but
    never shell.run; only implement and verify get shell.run.
    """
    for name in PIPELINE_NAMES:
        pipeline = PipelineDef.load(f"builtin:pipelines/{name}.yaml", Path("."))
        for step in pipeline.steps:
            if step.role == "clarifier":
                assert not ({"fs.read", "fs.write", "fs.edit", "shell.run"} & set(step.tools))
            if step.role in ("reviewer", "security"):
                assert "shell.run" not in step.tools


def test_builtin_pipelines_directory_ships_standard_yaml() -> None:
    """Packaging check: `builtin_root()` resolves to a real directory shipping
    `pipelines/standard.yaml`, whether run from source or an installed wheel."""
    assert (builtin_root() / "pipelines" / "standard.yaml").is_file()
