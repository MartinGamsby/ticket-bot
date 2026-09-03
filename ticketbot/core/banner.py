"""The "what was used" text — not the config.

`render_banner` produces the human-readable summary that lands in
`runs/<id>/banner.txt` (built with a `WorkItem` in play, by the orchestrator in a
later section) and that `ticketbot config banner` prints for a profile alone (no
work item, via `facts_from_profile`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.schema import AdapterConfig, Profile, RuntimeConfig
from .workitem import WorkItem

_MODEL_JOIN = " · "  # U+00B7 MIDDLE DOT


@dataclass
class BannerFacts:
    source: str = ""  # 'Jira ENG-1842 "Login times out on SSO" (5 points, Bug)'
    pipeline: str = ""  # 'builtin:pipelines/standard.yaml  (rule: story_points <= 5)'
    models: list[str] = field(default_factory=list)  # ['planner:Claude Opus 5', ...]
    executor: str = ""  # 'process: claude -p'
    runtime: str = ""  # 'Solari desktop 1280x720'
    repo: str = ""  # 'acme/app @ agent/ENG-1842-login-timeout'


def render_banner(facts: BannerFacts) -> str:
    """Exactly this shape; omit any line whose fact is empty:

    Using source=Jira ENG-1842 "Login times out on SSO" (5 points, Bug)
    pipeline=builtin:pipelines/standard.yaml  (rule: story_points <= 5)
    models=planner:Claude Opus 5 · coder:Claude Opus 5 · reviewer:gpt-5 (openai_compat)
    executor=process: claude -p
    runtime=Solari desktop 1280x720
    repo=acme/app @ agent/ENG-1842-login-timeout

    Model entries are joined with ' · ' (U+00B7). The result ends with a newline and
    is passed through redact() by the caller.
    """
    lines: list[str] = []
    if facts.source:
        lines.append(f"Using source={facts.source}")
    if facts.pipeline:
        lines.append(f"pipeline={facts.pipeline}")
    if facts.models:
        lines.append("models=" + _MODEL_JOIN.join(facts.models))
    if facts.executor:
        lines.append(f"executor={facts.executor}")
    if facts.runtime:
        lines.append(f"runtime={facts.runtime}")
    if facts.repo:
        lines.append(f"repo={facts.repo}")

    if not lines:
        return "\n"
    return "\n".join(lines) + "\n"


def _format_points(points: float) -> str:
    if points == int(points):
        return str(int(points))
    return str(points)


def source_fact(item: WorkItem, source_kind: str) -> str:
    """'Jira ENG-1842 "Login times out on SSO" (5 points, Bug)' when external_id and
    points are present; degrades gracefully: 'file "Add a /health endpoint" (Task)'.
    """
    if item.story_points is not None:
        parenthetical = f"({_format_points(item.story_points)} points, {item.issue_type})"
    else:
        parenthetical = f"({item.issue_type})"

    if item.external_id:
        head = f'{source_kind} {item.external_id} "{item.title}"'
    else:
        head = f'{source_kind} "{item.title}"'

    return f"{head} {parenthetical}"


def _prettify_model_id(model_id: str) -> str:
    return " ".join(part.capitalize() for part in model_id.split("-") if part)


def _describe_model(cfg: AdapterConfig) -> str:
    model_id = str(cfg.opt("model", cfg.type))
    if cfg.type == "anthropic":
        return _prettify_model_id(model_id)
    return f"{model_id} ({cfg.type})"


def _describe_executor(cfg: AdapterConfig) -> str:
    if cfg.type == "process":
        cmd = cfg.opt("cmd") or []
        if isinstance(cmd, list) and cmd:
            return f"process: {' '.join(str(part) for part in cmd)}"
        return "process"
    if cfg.type == "api":
        model_slot = cfg.opt("model")
        return f"api: {model_slot}" if model_slot else "api"
    return cfg.type


def _describe_runtime(cfg: RuntimeConfig) -> str:
    extra = [str(v) for v in (cfg.opt("mode"), cfg.opt("resolution")) if v]
    return f"{cfg.type} {' '.join(extra)}" if extra else cfg.type


def _describe_repo(cfg: AdapterConfig) -> str:
    detail = cfg.opt("clone") or cfg.opt("path")
    return f"{cfg.type} ({detail})" if detail else cfg.type


def facts_from_profile(profile: Profile) -> BannerFacts:
    """Config-only facts (no work item): pipeline = selector default, models from the
    provider slots, executor from the default kind, runtime from runtime.type (+
    mode / resolution when present), repo from repo.type (+ clone/path). Used by
    `ticketbot config banner`.
    """
    executor_cfg = profile.executor.kinds.get(profile.executor.default)

    # Only an `api` executor resolves `model:` slots into providers and calls them.
    # Under `process` the spawned CLI picks its own model, and under `stub` nothing
    # is called at all -- listing the configured slots would advertise models this
    # profile can never use. Mirrors `Orchestrator._banner_facts`, which asks the
    # live executor object via `uses_model_slots`; here only the config exists, so
    # the decision is made from the kind's `type`.
    uses_models = executor_cfg is None or executor_cfg.type == "api"
    models = (
        [f"{slot}:{_describe_model(cfg)}" for slot, cfg in profile.model.providers.items()]
        if uses_models
        else []
    )

    return BannerFacts(
        source="",
        pipeline=profile.pipeline_selector.default,
        models=models,
        executor=_describe_executor(executor_cfg) if executor_cfg is not None else "",
        runtime=_describe_runtime(profile.runtime),
        repo=_describe_repo(profile.repo),
    )
