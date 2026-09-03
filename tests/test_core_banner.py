from pathlib import Path

import pytest

from ticketbot.config.loader import load_profile
from ticketbot.config.schema import Profile
from ticketbot.core.banner import BannerFacts, facts_from_profile, render_banner, source_fact
from ticketbot.core.workitem import WorkItem

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def test_render_banner_matches_exact_expected_block():
    facts = BannerFacts(
        source='Jira ENG-1842 "Login times out on SSO" (5 points, Bug)',
        pipeline="builtin:pipelines/standard.yaml  (rule: story_points <= 5)",
        models=["planner:Claude Opus 5", "coder:Claude Opus 5", "reviewer:gpt-5 (openai_compat)"],
        executor="process: claude -p",
        runtime="Solari desktop 1280x720",
        repo="acme/app @ agent/ENG-1842-login-timeout",
    )

    expected = (
        'Using source=Jira ENG-1842 "Login times out on SSO" (5 points, Bug)\n'
        "pipeline=builtin:pipelines/standard.yaml  (rule: story_points <= 5)\n"
        "models=planner:Claude Opus 5 · coder:Claude Opus 5 · reviewer:gpt-5 (openai_compat)\n"
        "executor=process: claude -p\n"
        "runtime=Solari desktop 1280x720\n"
        "repo=acme/app @ agent/ENG-1842-login-timeout\n"
    )

    assert render_banner(facts) == expected


def test_render_banner_omits_empty_fact_lines():
    facts = BannerFacts(pipeline="builtin:pipelines/standard.yaml")
    result = render_banner(facts)

    assert result == "pipeline=builtin:pipelines/standard.yaml\n"
    assert "Using" not in result
    assert "models=" not in result
    assert "executor=" not in result
    assert "runtime=" not in result
    assert "repo=" not in result


def test_render_banner_all_empty_facts_still_ends_with_newline():
    assert render_banner(BannerFacts()) == "\n"


def test_render_banner_ends_with_single_trailing_newline():
    facts = BannerFacts(executor="process: claude -p")
    result = render_banner(facts)
    assert result.endswith("\n")
    assert not result.endswith("\n\n")


def test_source_fact_full_jira_style():
    item = WorkItem(
        id="eng-1842", title="Login times out on SSO",
        external_id="ENG-1842", issue_type="Bug", story_points=5,
    )
    assert source_fact(item, "Jira") == 'Jira ENG-1842 "Login times out on SSO" (5 points, Bug)'


def test_source_fact_degrades_without_external_id_or_points():
    item = WorkItem(id="add-health", title="Add a /health endpoint", issue_type="Task")
    assert source_fact(item, "file") == 'file "Add a /health endpoint" (Task)'


def test_source_fact_integral_points_render_without_decimal():
    item = WorkItem(id="x", title="T", external_id="ENG-1", story_points=3.0, issue_type="Story")
    assert source_fact(item, "Jira") == 'Jira ENG-1 "T" (3 points, Story)'


def test_source_fact_fractional_points_render_with_decimal():
    item = WorkItem(id="x", title="T", external_id="ENG-1", story_points=2.5, issue_type="Story")
    assert source_fact(item, "Jira") == 'Jira ENG-1 "T" (2.5 points, Story)'


def test_facts_from_profile_on_minimal_fixture():
    profile = load_profile(FIXTURES / "minimal.yaml")
    facts = facts_from_profile(profile)

    assert facts.source == ""
    assert facts.pipeline == "builtin:pipelines/standard.yaml"
    assert facts.models == ["main:Claude Opus 5"]
    assert facts.executor == "api: main"
    assert facts.runtime == "local_shell"
    assert facts.repo == "git_local (.)"


def test_facts_from_profile_distinguishes_anthropic_and_openai_compat_models():
    profile = Profile.model_validate(
        {
            "name": "banner-test",
            "source": {"type": "file"},
            "sink": {"type": "file"},
            "repo": {"type": "git_local", "path": "."},
            "model": {
                "default": "main",
                "providers": {
                    "main": {"type": "anthropic", "model": "claude-opus-5"},
                    "peer": {"type": "openai_compat", "model": "gpt-5"},
                },
            },
            # An `api` kind on purpose: only that executor resolves model slots, so
            # only that one gets a `models=` line for this test to inspect.
            "executor": {
                "default": "inline",
                "kinds": {"inline": {"type": "api", "model": "main"}},
            },
            "runtime": {"type": "solari", "mode": "desktop", "resolution": "1280x720"},
        }
    )

    facts = facts_from_profile(profile)

    assert facts.models == ["main:Claude Opus 5", "peer:gpt-5 (openai_compat)"]
    assert facts.executor == "api: main"
    assert facts.runtime == "solari desktop 1280x720"


def test_facts_from_profile_runtime_none_still_produces_a_fact():
    profile = load_profile(FIXTURES / "minimal.yaml")
    profile.runtime.type = "none"
    facts = facts_from_profile(profile)
    assert facts.runtime == "none"


def test_banner_output_is_passed_through_redact_by_caller():
    # facts_from_profile / render_banner never expand secrets themselves;
    # config.redact.redact() is the caller's job (exercised via the CLI).
    from ticketbot.config.redact import redact

    facts = BannerFacts(repo="github (token sk-ant-abc123def456ghi789)")
    banner_text = render_banner(facts)
    assert "sk-ant-" in banner_text  # not yet redacted
    assert "sk-ant-" not in redact(banner_text)  # redact() strips it


def _profile_with_executor(kind: dict) -> Profile:
    return Profile.model_validate(
        {
            "name": "banner-exec",
            "source": {"type": "file"},
            "sink": {"type": "file"},
            "repo": {"type": "git_local", "path": "."},
            "model": {
                "default": "main",
                "providers": {"main": {"type": "anthropic", "model": "claude-opus-5"}},
            },
            "executor": {"default": "k", "kinds": {"k": kind}},
            "runtime": {"type": "none"},
        }
    )


@pytest.mark.parametrize(
    "kind, lists_models",
    [
        ({"type": "api", "model": "main"}, True),
        ({"type": "process", "cmd": ["claude", "-p"]}, False),
        ({"type": "stub"}, False),
    ],
)
def test_the_config_banner_lists_models_only_for_an_api_executor(kind, lists_models):
    """A `process` CLI picks its own model and a `stub` calls none, so advertising
    the configured slots would name models the profile can never use."""
    facts = facts_from_profile(_profile_with_executor(kind))

    assert bool(facts.models) is lists_models
