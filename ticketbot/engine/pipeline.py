"""`PipelineDef`/`StepDef` -- the YAML shape a pipeline file is loaded into.

A pipeline file is untrusted-ish config (it may ship with a profile a user wrote),
so every structural problem -- duplicate step ids, an unknown key, an empty `steps`
list, an unsupported `for_each` value, or a `when:` that fails to parse -- is caught
eagerly at LOAD time and raised as `ConfigError` naming the file and the offending
step, never surfacing as a `KeyError`/`AttributeError` mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.loader import ConfigError, load_yaml, resolve_ref
from ..core.predicate import PredicateError, evaluate_any

_VALID_STEP_KEYS = {
    "id", "role", "model", "executor", "tools", "when", "for_each", "produces",
    "gate", "isolation", "commit", "on_block", "timeout_s", "prompt", "optional",
    "max_rounds",
}
_VALID_GATES = {"optional_human", "human"}
_VALID_FOR_EACH = {"plan.sections"}


@dataclass
class StepDef:
    id: str
    role: str
    model: str | None = None  # model SLOT name; None -> pipeline default -> profile default
    executor: str | None = None  # executor KIND name; same fallback chain
    tools: list[str] = field(default_factory=list)
    when: str | dict | None = None
    for_each: str | None = None  # currently only "plan.sections"
    produces: list[str] = field(default_factory=list)
    gate: str | None = None  # "optional_human" | "human" | None
    isolation: str | None = None  # "worktree" | None (informational; the repo adapter decides)
    commit: str | None = None  # commit message template
    on_block: str | None = None  # sink state to transition to when the step blocks
    timeout_s: int | None = None
    prompt: str | None = None  # override prompt ref, e.g. "builtin:prompts/roles/coder.md"
    optional: bool = False  # a failure marks the step failed but does not fail the run
    max_rounds: int = 1


def _step_from_dict(raw: Any, *, path: Path, index: int, seen_ids: set[str]) -> StepDef:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: step {index} must be a mapping, got {type(raw).__name__}")

    unknown = set(raw) - _VALID_STEP_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: step {raw.get('id', index)!r} has unknown key(s): {sorted(unknown)}"
        )

    if "id" not in raw or not raw["id"]:
        raise ConfigError(f"{path}: step {index} is missing a required 'id'")
    step_id = str(raw["id"])
    if step_id in seen_ids:
        raise ConfigError(f"{path}: duplicate step id {step_id!r}")

    if "role" not in raw or not raw["role"]:
        raise ConfigError(f"{path}: step {step_id!r} is missing a required 'role'")

    for_each = raw.get("for_each")
    if for_each is not None and for_each not in _VALID_FOR_EACH:
        raise ConfigError(
            f"{path}: step {step_id!r} has an unsupported for_each {for_each!r} "
            f"(supported: {sorted(_VALID_FOR_EACH)})"
        )

    when = raw.get("when")
    if when is not None:
        try:
            evaluate_any(when, {})
        except PredicateError as exc:
            raise ConfigError(f"{path}: step {step_id!r} has an invalid when: {exc}") from exc

    gate = raw.get("gate")
    if gate is not None and gate not in _VALID_GATES:
        raise ConfigError(
            f"{path}: step {step_id!r} has an unknown gate {gate!r} (expected one of {sorted(_VALID_GATES)})"
        )

    return StepDef(
        id=step_id,
        role=str(raw["role"]),
        model=raw.get("model"),
        executor=raw.get("executor"),
        tools=list(raw.get("tools") or []),
        when=when,
        for_each=for_each,
        produces=list(raw.get("produces") or []),
        gate=gate,
        isolation=raw.get("isolation"),
        commit=raw.get("commit"),
        on_block=raw.get("on_block"),
        timeout_s=raw.get("timeout_s"),
        prompt=raw.get("prompt"),
        optional=bool(raw.get("optional", False)),
        max_rounds=int(raw.get("max_rounds", 1)),
    )


@dataclass
class PipelineDef:
    name: str
    defaults: dict[str, Any] = field(default_factory=dict)
    steps: list[StepDef] = field(default_factory=list)
    on_question: str = "pause_and_relay"  # pause_and_relay | fail | ignore
    on_defer: str = "spawn_fixer"  # spawn_fixer | ignore
    ref: str = ""  # the ref it was loaded from, for the banner
    source_path: Path | None = None

    @classmethod
    def load(cls, ref: str, base_dir: Path) -> "PipelineDef":
        """resolve_ref(ref, base_dir) -> load_yaml -> validate."""
        path = resolve_ref(ref, base_dir)
        data = load_yaml(path)

        raw_steps = data.get("steps")
        if not raw_steps:
            raise ConfigError(f"{path}: pipeline has no steps")
        if not isinstance(raw_steps, list):
            raise ConfigError(f"{path}: 'steps' must be a list, got {type(raw_steps).__name__}")

        steps: list[StepDef] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_steps):
            step = _step_from_dict(raw, path=path, index=index, seen_ids=seen_ids)
            seen_ids.add(step.id)
            steps.append(step)

        return cls(
            name=str(data.get("name") or path.stem),
            defaults=dict(data.get("defaults") or {}),
            steps=steps,
            on_question=str(data.get("on_question", "pause_and_relay")),
            on_defer=str(data.get("on_defer", "spawn_fixer")),
            ref=ref,
            source_path=path,
        )

    def step(self, step_id: str) -> StepDef | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def index_of(self, step_id: str) -> int:
        for i, s in enumerate(self.steps):
            if s.id == step_id:
                return i
        return -1
