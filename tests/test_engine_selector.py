"""Rule-based pipeline selection: first matching rule wins, in order; the
banner-facing `reason` text; a broken rule is a `ConfigError` naming its index
rather than silently falling through to the default.
"""

from __future__ import annotations

import pytest

from ticketbot.config.loader import ConfigError
from ticketbot.config.schema import (
    AdapterConfig,
    ExecutorConfig,
    GatesConfig,
    ModelConfig,
    PipelineSelector,
    Profile,
    SelectorRule,
    SinkConfig,
)
from ticketbot.core.workitem import WorkItem
from ticketbot.engine.selector import select

SMALL_BUG = "builtin:pipelines/small-bug.yaml"
STANDARD = "builtin:pipelines/standard.yaml"
LARGE = "builtin:pipelines/large-with-clarification.yaml"
DEFAULT = "builtin:pipelines/default.yaml"


def _profile(rules: list[dict], default: str = DEFAULT) -> Profile:
    return Profile(
        name="p",
        source=AdapterConfig(type="file"),
        sink=SinkConfig(type="file"),
        repo=AdapterConfig(type="git_local"),
        model=ModelConfig(default="m", providers={"m": AdapterConfig(type="fake")}),
        executor=ExecutorConfig(default="e", kinds={"e": AdapterConfig(type="api", model="m")}),
        pipeline_selector=PipelineSelector(
            rules=[SelectorRule(when=r["when"], use=r["use"]) for r in rules],
            default=default,
        ),
        gates=GatesConfig(),
    )


def _item(*, points: float | None, issue_type: str = "Task") -> WorkItem:
    return WorkItem(id="x", title="Some title", story_points=points, issue_type=issue_type)


RULES = [
    {"when": {"story_points": {"lte": 2}, "issue_type": "Bug"}, "use": SMALL_BUG},
    {"when": {"story_points": {"lte": 5}}, "use": STANDARD},
    {"when": {"story_points": {"gte": 8}}, "use": LARGE},
]


@pytest.mark.parametrize(
    "points,issue_type,expected_ref",
    [
        (1, "Bug", SMALL_BUG),
        (2, "Bug", SMALL_BUG),
        (3, "Task", STANDARD),
        (5, "Story", STANDARD),
        (8, "Task", LARGE),
        (13, "Task", LARGE),
    ],
)
def test_rule_table_picks_right_pipeline(points, issue_type, expected_ref) -> None:
    profile = _profile(RULES)
    item = _item(points=points, issue_type=issue_type)
    selection = select(profile, item)
    assert selection.ref == expected_ref


def test_bug_with_low_points_takes_small_bug_over_standard() -> None:
    # points=2 matches BOTH rule 0 (small-bug, Bug+<=2) and rule 1 (standard,
    # <=5) -- order matters, and rule 0 comes first.
    profile = _profile(RULES)
    selection = select(profile, _item(points=2, issue_type="Bug"))
    assert selection.ref == SMALL_BUG
    assert "issue_type" in selection.reason


def test_non_bug_with_low_points_skips_to_standard() -> None:
    profile = _profile(RULES)
    selection = select(profile, _item(points=2, issue_type="Story"))
    assert selection.ref == STANDARD


def test_no_match_returns_default_with_reason_default() -> None:
    profile = _profile(RULES)
    # story_points=None: `<=`/`>=` against None never holds (see predicate's
    # numeric/ordered-enum fallback), so no rule matches.
    selection = select(profile, _item(points=None))
    assert selection.ref == DEFAULT
    assert selection.reason == "default"


def test_reason_string_reads_story_points_lte_5() -> None:
    profile = _profile(RULES)
    selection = select(profile, _item(points=5, issue_type="Task"))
    assert selection.reason == "story_points <= 5"


def test_reason_for_small_bug_rule_combines_both_clauses() -> None:
    profile = _profile(RULES)
    selection = select(profile, _item(points=1, issue_type="Bug"))
    assert selection.reason == "story_points <= 2 and issue_type == Bug"


def test_malformed_rule_raises_config_error_naming_rule_index() -> None:
    bad_rules = [
        {"when": {"story_points": {"lte": 2}}, "use": SMALL_BUG},
        {"when": {"story_points": {"bogus_op": 3}}, "use": STANDARD},
    ]
    profile = _profile(bad_rules)
    # An item that does NOT match rule 0, so evaluation reaches the broken
    # rule 1 -- proving a broken rule raises rather than being silently
    # skipped in favor of the default.
    with pytest.raises(ConfigError, match=r"rules\[1\]"):
        select(profile, _item(points=100, issue_type="Task"))
