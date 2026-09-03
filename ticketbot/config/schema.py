"""Pydantic v2 models for the whole `ticketbot` profile.

Every adapter block (`AdapterConfig` and its subclasses) is deliberately permissive:
adapters validate their own options at use time, so adding a new source/sink/model/
executor/runtime/repo kind never requires touching this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AdapterConfig(BaseModel):
    """A `{type: <name>, ...arbitrary options}` block. Options are validated by the adapter."""

    model_config = ConfigDict(extra="allow")
    type: str

    def options(self) -> dict[str, Any]:
        """Every key except `type` (and except the fields declared on subclasses)."""
        return dict(self.model_extra or {})

    def opt(self, key: str, default: Any = None) -> Any:
        """Raw option value — may still contain unexpanded `${ENV}` references."""
        return self.options().get(key, default)


class ModelConfig(BaseModel):
    default: str
    providers: dict[str, AdapterConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_is_known_provider(self) -> "ModelConfig":
        if self.default not in self.providers:
            available = ", ".join(sorted(self.providers)) or "(none)"
            raise ValueError(
                f"model.default={self.default!r} is not a key of model.providers "
                f"(available: {available})"
            )
        return self


class ExecutorConfig(BaseModel):
    default: str
    kinds: dict[str, AdapterConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_is_known_kind(self) -> "ExecutorConfig":
        if self.default not in self.kinds:
            available = ", ".join(sorted(self.kinds)) or "(none)"
            raise ValueError(
                f"executor.default={self.default!r} is not a key of executor.kinds "
                f"(available: {available})"
            )
        return self


class SinkConfig(AdapterConfig):
    also: list[AdapterConfig] = Field(default_factory=list)


class RuntimeConfig(AdapterConfig):
    screenshot_on: list[str] = Field(default_factory=list)  # step ids


class SelectorRule(BaseModel):
    when: dict[str, Any]
    use: str


class PipelineSelector(BaseModel):
    rules: list[SelectorRule] = Field(default_factory=list)
    default: str = "builtin:pipelines/standard.yaml"


class GatesConfig(BaseModel):
    on_unclear: Literal["comment_and_unassign", "comment_only", "proceed", "fail"] = "comment_and_unassign"
    on_pr_ready: Literal["human_review", "auto"] = "human_review"
    max_clarify_rounds: int = 2


class BudgetConfig(BaseModel):
    max_cost_usd: float | None = None
    max_wall_clock_s: int | None = None


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    version: int = 1
    extends: str | None = None
    source: AdapterConfig
    sink: SinkConfig
    repo: AdapterConfig
    model: ModelConfig
    executor: ExecutorConfig
    runtime: RuntimeConfig = RuntimeConfig(type="none")
    pipeline_selector: PipelineSelector = PipelineSelector()
    gates: GatesConfig = GatesConfig()
    budget: BudgetConfig = BudgetConfig()
    runs_dir: str = "runs"

    # Set by the loader, not by YAML. Exclude from serialization.
    base_dir: Path | None = Field(default=None, exclude=True)
