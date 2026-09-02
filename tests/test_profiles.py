"""`profiles/*.yaml`: every example profile (and the shared `_base.yaml`) loads and
validates with NO environment variables set (`${ENV}` refs must survive
unexpanded), every `pipeline_selector` ref resolves to a real pipeline file, every
`model.default`/`executor.default` names a real slot/kind, `facts_from_profile()`
renders a banner without raising, `github-codex.yaml` names no Anthropic model or
vendor, `file-text-none.yaml` is the fully-offline default, no profile leaks a
secret-shaped literal, and the selector picks the right built-in pipeline end to
end for a representative work item at each size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ticketbot.config.loader import load_profile, resolve_ref
from ticketbot.config.redact import PATTERNS
from ticketbot.config.schema import Profile
from ticketbot.core.banner import facts_from_profile, render_banner
from ticketbot.core.workitem import WorkItem
from ticketbot.engine.selector import select

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"
PROFILE_PATHS = sorted(PROFILES_DIR.glob("*.yaml"))
PROFILE_NAMES = [p.stem for p in PROFILE_PATHS]

assert PROFILE_PATHS, f"no profiles found under {PROFILES_DIR}"


@pytest.fixture(autouse=True)
def _no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every assertion in this module runs with NO relevant environment variables
    set -- `${ENV}` refs must survive `load_profile` unexpanded regardless."""
    for name in (
        "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BOT_ACCOUNT_ID", "GITHUB_TOKEN",
        "PEER_BASE_URL", "PEER_API_KEY", "MODEL_BASE_URL", "MODEL_API_KEY",
        "ANTHROPIC_API_KEY", "SOLARI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_profile_loads_and_validates_with_no_env_vars_set(path: Path) -> None:
    profile = load_profile(path)
    assert isinstance(profile, Profile)


def _contains_env_ref(value: Any) -> bool:
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, dict):
        return any(_contains_env_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_env_ref(v) for v in value)
    return False


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_env_refs_survive_unexpanded(path: Path) -> None:
    # Read the raw YAML data (not the file's text) so a `${ENV}` mentioned only in
    # a comment doesn't produce a false positive either way.
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not _contains_env_ref(raw_data):
        pytest.skip(f"{path.name} has no ${{ENV}} refs to check")
    profile = load_profile(path)
    dumped = profile.model_dump_json()
    assert "${" in dumped


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_pipeline_selector_refs_resolve_to_real_files(path: Path) -> None:
    profile = load_profile(path)
    base_dir = profile.base_dir or PROFILES_DIR
    resolve_ref(profile.pipeline_selector.default, base_dir)
    for rule in profile.pipeline_selector.rules:
        resolve_ref(rule.use, base_dir)


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_model_and_executor_defaults_name_a_real_slot_and_kind(path: Path) -> None:
    profile = load_profile(path)
    assert profile.model.default in profile.model.providers
    assert profile.executor.default in profile.executor.kinds


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_facts_from_profile_renders_a_banner_without_raising(path: Path) -> None:
    profile = load_profile(path)
    facts = facts_from_profile(profile)
    banner = render_banner(facts)
    assert isinstance(banner, str)


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_no_profile_contains_a_secret_shaped_literal(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    for name, pattern in PATTERNS:
        assert not pattern.search(raw), f"{path.name} contains what looks like a real {name} secret"


def test_github_codex_names_no_anthropic_model_or_vendor() -> None:
    raw = (PROFILES_DIR / "github-codex.yaml").read_text(encoding="utf-8").lower()
    assert "anthropic" not in raw
    assert "claude" not in raw


def test_file_text_none_is_the_fully_offline_default() -> None:
    profile = load_profile(PROFILES_DIR / "file-text-none.yaml")
    assert profile.source.type == "file"
    assert profile.sink.type == "file"
    assert profile.repo.type == "git_local"
    assert profile.runtime.type == "none"


def test_base_yaml_itself_is_a_complete_valid_profile() -> None:
    # _base.yaml has no `extends:` of its own -- it must validate standalone, not
    # just as a fragment merged into a child.
    profile = load_profile(PROFILES_DIR / "_base.yaml")
    assert profile.name == "_base"


@pytest.mark.parametrize(
    "points,issue_type,expected_ref",
    [
        (2, "Bug", "builtin:pipelines/small-bug.yaml"),
        (5, "Story", "builtin:pipelines/standard.yaml"),
        (13, "Story", "builtin:pipelines/large-with-clarification.yaml"),
    ],
)
def test_selector_end_to_end_picks_the_right_builtin_pipeline(points, issue_type, expected_ref) -> None:
    profile = load_profile(PROFILES_DIR / "file-text-none.yaml")
    item = WorkItem(id="x", title="Some ticket", story_points=points, issue_type=issue_type)
    selection = select(profile, item)
    assert selection.ref == expected_ref
