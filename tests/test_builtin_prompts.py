"""`builtin/prompts/roles/*.md` and `builtin/prompts/comments/*.md`: every file
loads, every `{placeholder}` it uses is a key `engine.context.prompt_values()`
actually produces (a typo'd placeholder is caught here, not at run time -- an
unknown placeholder is left verbatim by `core.templating.render`, per
`ticketbot/core/templating.py`), and every role's `Return ONLY:` contract survives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ticketbot.config.loader import builtin_root
from ticketbot.config.schema import (
    AdapterConfig,
    ExecutorConfig,
    GatesConfig,
    ModelConfig,
    Profile,
    SinkConfig,
)
from ticketbot.core.run import Run
from ticketbot.core.templating import missing_placeholders, render
from ticketbot.core.workitem import Comment, WorkItem
from ticketbot.engine.context import prompt_values
from ticketbot.engine.orchestrator import _PLAN_SECURITY_RE, _load_role_prompt
from ticketbot.engine.pipeline import PipelineDef

ROLES_DIR = builtin_root() / "prompts" / "roles"
COMMENTS_DIR = builtin_root() / "prompts" / "comments"

ROLE_FILES = sorted(ROLES_DIR.glob("*.md"))
RETURN_ONLY_ROLES = [
    "coder", "tester", "reviewer", "security", "reporter", "fixer", "ingest", "clarifier",
]

# A placeholder token left over after rendering: `{` immediately followed by a
# lowercase letter. `{{`/`}}` render to literal single braces (never leaves a
# lowercase letter directly after `{`), so this pattern only fires on a genuine
# unsubstituted `{name}`.
_RESIDUAL_PLACEHOLDER = re.compile(r"\{[a-z]")


def _profile() -> Profile:
    return Profile(
        name="prompt-test",
        source=AdapterConfig(type="file"),
        sink=SinkConfig(type="file"),
        repo=AdapterConfig(type="git_local", path="."),
        model=ModelConfig(
            default="main",
            providers={
                "main": AdapterConfig(type="anthropic", model="claude-opus-5"),
                "cheap": AdapterConfig(type="anthropic", model="claude-haiku-4-5"),
                "peer": AdapterConfig(type="openai_compat", model="gpt-5"),
            },
        ),
        executor=ExecutorConfig(
            default="inline", kinds={"inline": AdapterConfig(type="api", model="main")}
        ),
        gates=GatesConfig(),
    )


def _full_values(tmp_path: Path) -> dict:
    item = WorkItem(
        id="tick-1",
        title="Add a /health endpoint",
        description="Add a GET /health endpoint returning 200.",
        external_id="ENG-1842",
        issue_type="Bug",
        story_points=3,
        labels=["agent", "backend"],
        acceptance="- returns 200\n- returns JSON body",
        url="https://example.atlassian.net/browse/ENG-1842",
        comments=[Comment(author="alice", body="please add a test for the 500 case")],
    )
    run = Run(
        id="2026-09-01-1443-eng-1842-a3f9",
        profile_name="prompt-test",
        work_item_key=item.key,
        banner="Using source=file ...\n",
        extra={
            "plan_security": "no",
            "section_count": 1,
            "pr_url": "https://github.com/acme/app/pull/7",
            "screenshots": ["screenshots/verify-01.png"],
        },
    )
    profile = _profile()
    pipeline = PipelineDef.load("builtin:pipelines/standard.yaml", tmp_path)
    step = pipeline.steps[0]
    section = {"file": str(tmp_path / "sections" / "section-1.md"), "title": "Add the route", "index": 1, "count": 1}

    return prompt_values(
        item=item,
        run=run,
        profile=profile,
        pipeline=pipeline,
        step=step,
        workspace=tmp_path / "workspace",
        run_dir=tmp_path / "run",
        section=section,
        extra={"defer_line": "DEFER: the retry backoff policy needs a design decision", "context_paths": "- ticketbot/engine/context.py -- placeholder source"},
    )


# --------------------------------------------------------------------------- #
# Role prompts
# --------------------------------------------------------------------------- #


def test_nine_role_prompts_exist() -> None:
    names = sorted(p.stem for p in ROLE_FILES)
    assert names == [
        "clarifier", "coder", "fixer", "ingest", "planner", "reporter", "reviewer",
        "security", "tester",
    ]


@pytest.mark.parametrize("path", ROLE_FILES, ids=lambda p: p.stem)
def test_role_prompt_loads_and_front_matter_parses(path: Path) -> None:
    system, body = _load_role_prompt(path)
    assert system
    assert body.strip()


@pytest.mark.parametrize("path", ROLE_FILES, ids=lambda p: p.stem)
def test_every_placeholder_is_produced_by_prompt_values(path: Path, tmp_path: Path) -> None:
    values = _full_values(tmp_path)
    text = path.read_text(encoding="utf-8")
    missing = missing_placeholders(text, values)
    assert missing == [], f"{path.name} references placeholder(s) not in prompt_values(): {missing}"


@pytest.mark.parametrize("role", RETURN_ONLY_ROLES)
def test_role_prompt_contains_return_only_contract(role: str) -> None:
    text = (ROLES_DIR / f"{role}.md").read_text(encoding="utf-8")
    assert "Return ONLY" in text


def test_planner_contains_premise_check() -> None:
    text = (ROLES_DIR / "planner.md").read_text(encoding="utf-8")
    assert "PREMISE CHECK" in text


def test_planner_teaches_a_security_line_the_engines_regex_matches() -> None:
    """The engine extracts plan.md's security flag with
    `_PLAN_SECURITY_RE` (`^\\s*(?:##+\\s*)?Security[: ].*?\\b(yes|no)\\b`). planner.md
    must itself contain a standalone example line in exactly that shape, so a model
    following the instructions produces a plan.md the engine can parse.
    """
    text = (ROLES_DIR / "planner.md").read_text(encoding="utf-8")
    assert _PLAN_SECURITY_RE.search(text) is not None


@pytest.mark.parametrize("path", ROLE_FILES, ids=lambda p: p.stem)
def test_rendering_with_full_values_leaves_no_residual_placeholder(path: Path, tmp_path: Path) -> None:
    values = _full_values(tmp_path)
    text = path.read_text(encoding="utf-8")
    rendered = render(text, values)
    residual = _RESIDUAL_PLACEHOLDER.findall(rendered)
    assert residual == [], f"{path.name} left unsubstituted placeholder(s): {residual}"


def test_ingest_doubled_braces_render_to_a_literal_single_brace_json_object(tmp_path: Path) -> None:
    values = _full_values(tmp_path)
    text = (ROLES_DIR / "ingest.md").read_text(encoding="utf-8")
    assert "{{" in text and "}}" in text  # the deliberate doubled-brace JSON literal

    rendered = render(text, values)
    assert "{{" not in rendered and "}}" not in rendered
    assert '{"summary":' in rendered
    assert rendered.rstrip().endswith("}")


# --------------------------------------------------------------------------- #
# Comment templates
# --------------------------------------------------------------------------- #


def test_clarify_comment_renders_with_its_documented_keys() -> None:
    text = (COMMENTS_DIR / "clarify.md").read_text(encoding="utf-8")
    values = {"ticket_key": "ENG-1842", "question": "1. What auth scheme?", "banner": "Using ..."}
    assert missing_placeholders(text, values) == []
    rendered = render(text, values)
    assert "ENG-1842" in rendered and "What auth scheme?" in rendered


def test_blocked_comment_renders_with_its_documented_keys() -> None:
    text = (COMMENTS_DIR / "blocked.md").read_text(encoding="utf-8")
    values = {"ticket_key": "ENG-1842", "reason": "budget exceeded", "run_id": "2026-09-01-1443-eng-1842-a3f9", "banner": "Using ..."}
    assert missing_placeholders(text, values) == []
    rendered = render(text, values)
    assert "budget exceeded" in rendered and "2026-09-01-1443-eng-1842-a3f9" in rendered


def test_done_comment_renders_with_its_documented_keys() -> None:
    text = (COMMENTS_DIR / "done.md").read_text(encoding="utf-8")
    values = {"ticket_key": "ENG-1842", "summary": "Added the /health endpoint.", "pr_url": "https://github.com/acme/app/pull/7", "banner": "Using ..."}
    assert missing_placeholders(text, values) == []
    rendered = render(text, values)
    assert "pull/7" in rendered


def test_every_comment_ends_with_the_banner_line() -> None:
    for path in COMMENTS_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "{banner}" in text, f"{path.name} does not embed the banner"
