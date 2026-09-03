"""One place that builds the mapping used by BOTH `when:` predicates
(`build_context`) and role-prompt templates (`prompt_values`).

Keeping these together means a `when:` rule and a `{placeholder}` in a role prompt
can never silently drift apart on what a name like `story_points` or `plan.security`
resolves to.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.schema import Profile
    from ..core.run import Run
    from ..core.workitem import WorkItem
    from .pipeline import PipelineDef, StepDef

# The two fixed paragraphs every role prompt embeds verbatim -- kept HERE, once, so
# every role gets identical wording no matter which prompt file renders them.
GIT_PROHIBITION = (
    "Do NOT run git (no add/commit/push/checkout). Leave every change in the working "
    "tree — the orchestrator commits."
)

QUESTION_PROTOCOL = (
    "If a genuine decision blocks you — ambiguous requirement, missing credential, "
    "a design fork the user must choose — do NOT guess. End your turn with a block "
    "that starts with `QUESTION:` on its own line, state the decision needed and the "
    "options, and stop. Otherwise, complete the task."
)


def build_context(
    *,
    item: "WorkItem",
    run: "Run",
    profile: "Profile",
    workspace: Path | None = None,
    run_dir: Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    workitem_ctx = item.as_context()
    ctx: dict[str, Any] = {
        "workitem": workitem_ctx,
        # the workitem keys are ALSO mirrored at the top level so selector rules can
        # say `story_points` / `issue_type` / `labels` / `size` / `ambiguity` directly
        **workitem_ctx,
        "plan": {
            # "yes" before the planner has run, matching `_after_planner`'s
            # fail-closed default: an unset `plan_security` means "nobody has
            # assessed this yet", and a security gate must not read that as "no".
            "security": run.extra.get("plan_security", "yes"),
            "sections": run.extra.get("section_count", 0),
        },
        "diff": {
            "touches_security": run.extra.get("diff_touches_security", False),
            "files": run.extra.get("diff_files", 0),
        },
        "run": {
            "id": run.id,
            "clarify_rounds": run.extra.get("clarify_rounds", 0),
            "status": run.status.value,
        },
        # Reserved namespace, deliberately empty: nothing populates it today, so a
        # `when: "step.<anything>"` resolves to MISSING (falsy) rather than raising.
        # If a per-step fact is ever needed here, the orchestrator has to pass it
        # through `extra=` -- do not assume it is already filled.
        "step": {},
    }
    if extra:
        ctx.update(extra)
    return ctx


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _join_list(value: Any) -> str:
    if not value:
        return ""
    return ", ".join(str(v) for v in value)


def _comments_text(item: "WorkItem") -> str:
    if not item.comments:
        return ""
    return "\n".join(f"- {c.author}: {c.body}" for c in item.comments)


def prompt_values(
    *,
    item: "WorkItem",
    run: "Run",
    profile: "Profile",
    pipeline: "PipelineDef",
    step: "StepDef",
    workspace: Path | None,
    run_dir: Path | None,
    section: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = Path(workspace) if workspace else None
    rd = Path(run_dir) if run_dir else None

    values: dict[str, Any] = {
        "repo_root": str(ws) if ws else "",
        "workspace": str(ws) if ws else "",
        "run_dir": str(rd) if rd else "",
        "platform": "Windows / PowerShell 5.1" if sys.platform == "win32" else "POSIX shell",
        "python_note": f"Python {sys.version.split()[0]}",
        "task": item.description or item.title,
        "ticket_key": item.key,
        "ticket_title": item.title,
        "ticket_type": item.issue_type,
        "ticket_points": _s(item.story_points),
        "ticket_url": _s(item.url),
        "ticket_description": item.description,
        "ticket_acceptance": item.acceptance,
        "ticket_labels": _join_list(item.labels),
        "ticket_comments": _comments_text(item),
        "context_paths": "",
        "banner": run.banner,
        "step_id": step.id,
        "role": step.role,
        "plan_file": str(rd / "plan.md") if rd else "",
        "sections_dir": str(rd / "sections") if rd else "",
        "section_file": "",
        "section_title": "",
        "section_index": "",
        "section_count": "",
        "diff": str(rd / "patch.diff") if rd else "",
        "test_report": str(rd / "test-report.md") if rd else "",
        "review_file": str(rd / "review.md") if rd else "",
        "security_file": str(rd / "security.md") if rd else "",
        "pr_url": run.extra.get("pr_url", ""),
        "screenshots": _join_list(run.extra.get("screenshots", [])),
        "question_protocol": QUESTION_PROTOCOL,
        "git_prohibition": GIT_PROHIBITION,
    }

    if section:
        values["section_file"] = _s(section.get("file"))
        values["section_title"] = _s(section.get("title"))
        values["section_index"] = _s(section.get("index"))
        values["section_count"] = _s(section.get("count"))

    if extra:
        values.update(extra)
    return values
