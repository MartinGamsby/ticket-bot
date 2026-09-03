"""Rule-based pipeline selection: which pipeline YAML runs for a given work item,
plus a human-readable reason for the banner.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.loader import ConfigError
from ..config.schema import Profile
from ..core.predicate import PredicateError, describe_mapping, evaluate_mapping
from ..core.run import Run
from ..core.workitem import WorkItem
from .context import build_context


@dataclass
class Selection:
    ref: str  # 'builtin:pipelines/standard.yaml'
    reason: str  # 'rule: story_points <= 5' | 'default'


def select(profile: Profile, item: WorkItem) -> Selection:
    """Evaluate `profile.pipeline_selector.rules` IN ORDER against
    `build_context()`'s top-level mirror; the FIRST rule whose `when` mapping holds
    wins, and its reason is `describe_mapping(rule.when)`. No rule matches ->
    `Selection(default, 'default')`.

    No `Run` exists yet at selection time (it is created right after this call), so
    a throwaway, never-persisted `Run` is used purely to build the same context
    shape `build_context()` always produces -- selector rules only ever reference
    `workitem.*` fields anyway, which are already present before any step has run.
    """
    dummy_run = Run(id="", profile_name=profile.name, work_item_key=item.key)
    ctx = build_context(item=item, run=dummy_run, profile=profile)

    for index, rule in enumerate(profile.pipeline_selector.rules):
        try:
            matched = evaluate_mapping(rule.when, ctx)
        except PredicateError as exc:
            raise ConfigError(f"pipeline_selector.rules[{index}]: invalid when: {exc}") from exc
        if matched:
            return Selection(ref=rule.use, reason=describe_mapping(rule.when))

    return Selection(ref=profile.pipeline_selector.default, reason="default")
