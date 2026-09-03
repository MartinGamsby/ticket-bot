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

import re
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

# Every environment variable any shipped profile references, plus the ones an
# adapter reads on its own -- deleted before each test, and set to a sentinel by
# `test_profile_loading_never_reads_the_environment`.
ENV_VAR_NAMES = (
    "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BOT_ACCOUNT_ID", "GITHUB_TOKEN",
    "PEER_BASE_URL", "PEER_API_KEY", "MODEL_BASE_URL", "MODEL_API_KEY",
    "ANTHROPIC_API_KEY", "SOLARI_API_KEY",
)

_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

SENTINEL = "expanded-at-load-time-which-must-never-happen"


@pytest.fixture(autouse=True)
def _no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every assertion in this module runs with NO relevant environment variables
    set -- `${ENV}` refs must survive `load_profile` unexpanded regardless."""
    for name in ENV_VAR_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_profile_loads_and_validates_with_no_env_vars_set(path: Path) -> None:
    profile = load_profile(path)
    assert isinstance(profile, Profile)


def _env_refs(value: Any) -> set[str]:
    """Every `${NAME}` token in a parsed YAML tree. Reads the parsed DATA, not the
    file's text, so a `${ENV}` mentioned only in a comment is never counted.
    """
    if isinstance(value, str):
        return set(_ENV_REF_RE.findall(value))
    if isinstance(value, dict):
        return set().union(*(_env_refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_env_refs(v) for v in value)) if value else set()
    return set()


@pytest.mark.parametrize("path", PROFILE_PATHS, ids=PROFILE_NAMES)
def test_profile_loading_never_reads_the_environment(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property holds for EVERY profile, including the three that carry no
    `${ENV}` refs at all: loading is byte-for-byte identical whether the
    referenced variables are absent or populated. That is what makes
    `ticketbot validate` runnable with no credentials anywhere, and it is a
    stronger claim than "a `${` survived" -- with the variables unset, a loader
    that DID expand would leave the ref looking untouched anyway.
    """
    raw_refs = _env_refs(yaml.safe_load(path.read_text(encoding="utf-8")))

    with_nothing_set = load_profile(path).model_dump_json()  # autouse fixture cleared them
    for name in ENV_VAR_NAMES:
        monkeypatch.setenv(name, SENTINEL)
    with_everything_set = load_profile(path).model_dump_json()

    assert with_everything_set == with_nothing_set
    assert SENTINEL not in with_everything_set
    for ref in raw_refs:  # and each declared ref is still there, verbatim
        assert ref in with_everything_set, ref


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


def _yaml_body_lower(path: Path) -> str:
    """The file's text minus whole-line `#` comments, lowercased -- a comment may
    legitimately NAME the vendor it is explaining how to avoid."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ).lower()


def test_github_codex_names_no_anthropic_model_or_vendor() -> None:
    """Asserted on the LOADED profile, not only on the file's own text.

    `extends:` DEEP-MERGES, so a model slot this profile omits is inherited from
    `_base.yaml` -- where `peer`, the slot `standard.yaml`'s `review` step asks
    for, is an `anthropic` provider. A raw-text scan alone passes happily while
    the review step calls the one vendor this profile exists to avoid.
    """
    raw = _yaml_body_lower(PROFILES_DIR / "github-codex.yaml")
    assert "anthropic" not in raw
    assert "claude" not in raw

    profile = load_profile(PROFILES_DIR / "github-codex.yaml")
    assert profile.model.providers, "an empty provider map would pass vacuously"
    for slot, cfg in profile.model.providers.items():
        assert cfg.type == "openai_compat", f"model slot {slot!r} resolved to {cfg.type!r}"
        assert "claude" not in str(cfg.opt("model", "")).lower(), slot
    for kind, cfg in profile.executor.kinds.items():
        cmd = " ".join(str(part) for part in (cfg.opt("cmd") or []))
        assert "claude" not in cmd.lower(), f"executor kind {kind!r} spawns {cmd!r}"


def test_no_github_repo_profile_inherits_a_local_repo_path() -> None:
    """Asserted on the LOADED profile, for the same reason as the `peer` slot above:
    `extends:` deep-merges, so a `path:` in `_base.yaml`'s `repo:` block survives
    into every child -- including the ones that switch to `repo: {type: github}`
    and give a `clone:` instead.

    `GithubRepo.__init__` only installs its per-repo clone cache when `path` is
    absent. With an inherited `path: "."` it points at the PROFILE's own directory
    instead, so `ensure_clone()` fetches and `checkout()` runs `git worktree add`
    against whatever repository contains `profiles/` -- this checkout -- rather
    than the clone URL the profile names.
    """
    github_profiles = [p for p in PROFILE_PATHS if load_profile(p).repo.type == "github"]
    assert github_profiles, "no profile uses repo type 'github' -- this would pass vacuously"

    for path in github_profiles:
        repo = load_profile(path).repo
        assert repo.opt("clone"), f"{path.name}: a github repo profile must name a clone URL"
        assert repo.opt("path") is None, (
            f"{path.name} carries repo.path={repo.opt('path')!r} "
            "(inherited from _base.yaml?), which defeats GithubRepo's clone cache"
        )


def test_file_text_none_is_the_fully_offline_default() -> None:
    profile = load_profile(PROFILES_DIR / "file-text-none.yaml")
    assert profile.source.type == "file"
    assert profile.sink.type == "file"
    assert profile.repo.type == "git_local"
    assert profile.runtime.type == "none"


@pytest.mark.parametrize(
    ("profile_name", "kind", "key_name", "store_name"),
    [
        ("jira-claude-solari", "claude-cli", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"),
        ("github-codex", "codex-cli", "OPENAI_API_KEY", "CODEX_HOME"),
    ],
)
def test_shipped_cli_executors_can_authenticate_out_of_the_box(
    profile_name: str, kind: str, key_name: str, store_name: str
) -> None:
    """The credential contract, pinned where a user meets it. The spawned CLI signs
    in from its own store (reachable through `DEFAULT_PASSTHROUGH` alone), and the
    two names below are forwarded ONLY when set -- for headless/CI, and for a
    relocated store.

    The declaration must be `env_passthrough:`, never `env:`. An `env:` value is a
    `${ENV}` ref expanded strictly at adapter-construction time, so
    `env: {ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"}` would fail the run on every
    machine that authenticates by OAuth instead of by key -- which is the normal
    case for `claude -p`.
    """
    profile = load_profile(PROFILES_DIR / f"{profile_name}.yaml")
    executor = profile.executor.kinds[kind]

    assert executor.type == "process"
    assert set(executor.opt("env_passthrough") or []) == {key_name, store_name}
    assert not (executor.opt("env") or {}), "credentials go in env_passthrough, not env"


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


def test_the_claude_cli_profile_drives_a_process_executor_not_the_api():
    """The zero-key local path: `claude -p` authenticates itself, so this profile
    must never resolve a model provider (which would demand ANTHROPIC_API_KEY)."""
    profile = load_profile(PROFILES_DIR / "file-claude-cli.yaml")

    kind = profile.executor.kinds[profile.executor.default]

    assert profile.executor.default == "claude-cli"
    assert kind.type == "process"
    assert kind.opt("cmd")[0] == "claude"
    # The key may be FORWARDED when set (headless/CI), but must not be REQUIRED:
    # `env_passthrough` skips an unset name, whereas an `env:` `${ENV}` ref would
    # be expanded strictly and fail on every OAuth-authenticated machine.
    assert "ANTHROPIC_API_KEY" in (kind.opt("env_passthrough") or [])
    assert kind.opt("env") in (None, {})


def test_the_claude_cli_profile_loads_with_no_environment_at_all(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "JIRA_API_TOKEN", "GITHUB_TOKEN", "SOLARI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    profile = load_profile(PROFILES_DIR / "file-claude-cli.yaml")

    assert profile.name == "file-claude-cli"
    assert profile.runtime.type == "none"
