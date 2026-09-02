import pytest
from pydantic import ValidationError

from ticketbot.config.schema import AdapterConfig, Profile, SinkConfig


def _minimal_profile_dict() -> dict:
    return {
        "name": "minimal",
        "source": {"type": "file"},
        "sink": {"type": "file"},
        "repo": {"type": "git_local", "path": "."},
        "model": {
            "default": "main",
            "providers": {"main": {"type": "anthropic", "model": "claude-opus-5"}},
        },
        "executor": {
            "default": "inline",
            "kinds": {"inline": {"type": "api", "model": "main"}},
        },
    }


def test_minimal_profile_validates():
    profile = Profile.model_validate(_minimal_profile_dict())
    assert profile.name == "minimal"
    assert profile.model.default == "main"
    assert profile.executor.default == "inline"
    # defaults applied for everything not specified
    assert profile.runtime.type == "none"
    assert profile.gates.on_unclear == "comment_and_unassign"
    assert profile.gates.max_clarify_rounds == 2
    assert profile.runs_dir == "runs"


def test_adapter_config_options_excludes_type():
    adapter = AdapterConfig(type="jira", base_url="https://x", token="${JIRA_TOKEN}")
    assert adapter.options() == {"base_url": "https://x", "token": "${JIRA_TOKEN}"}
    assert adapter.opt("base_url") == "https://x"
    assert adapter.opt("missing", "fallback") == "fallback"


def test_subclass_options_excludes_declared_subclass_fields_too():
    sink = SinkConfig(type="jira", also=[{"type": "file"}], project="ENG")
    # `also` is a declared field on SinkConfig, not an "extra" option.
    assert sink.options() == {"project": "ENG"}
    assert len(sink.also) == 1
    assert sink.also[0].type == "file"


def test_model_default_not_in_providers_raises_naming_key():
    data = _minimal_profile_dict()
    data["model"]["default"] = "missing-provider"
    with pytest.raises(ValidationError) as exc_info:
        Profile.model_validate(data)
    message = str(exc_info.value)
    assert "missing-provider" in message
    assert "main" in message


def test_executor_default_not_in_kinds_raises_naming_key():
    data = _minimal_profile_dict()
    data["executor"]["default"] = "missing-kind"
    with pytest.raises(ValidationError) as exc_info:
        Profile.model_validate(data)
    message = str(exc_info.value)
    assert "missing-kind" in message
    assert "inline" in message


def test_unknown_top_level_key_rejected():
    data = _minimal_profile_dict()
    data["bogus_field"] = True
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
