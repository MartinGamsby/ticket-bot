from pathlib import Path

import pytest

from ticketbot.config.loader import (
    ConfigError,
    MissingEnvError,
    builtin_root,
    deep_merge,
    expand_env,
    has_env_ref,
    load_profile,
    load_profile_dict,
    load_yaml,
    resolve_ref,
    resolved_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def test_deep_merge_nested_dicts_merge_lists_replace_inputs_unmutated():
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2], "keep": "base"}
    override = {"a": {"b": 99}, "list": [9]}

    merged = deep_merge(base, override)

    assert merged == {"a": {"b": 99, "c": 2}, "list": [9], "keep": "base"}
    # inputs must not be mutated
    assert base == {"a": {"b": 1, "c": 2}, "list": [1, 2], "keep": "base"}
    assert override == {"a": {"b": 99}, "list": [9]}


def test_deep_merge_returns_new_dict():
    base = {"a": {"b": 1}}
    merged = deep_merge(base, {})
    merged["a"]["b"] = 2
    assert base["a"]["b"] == 1


def test_extends_child_overrides_parent_scalars_adds_keys_replaces_lists():
    child_path = FIXTURES / "child-extends.yaml"
    merged, base_dir = load_profile_dict(child_path)

    assert "extends" not in merged
    assert merged["name"] == "child"  # scalar override
    assert merged["budget"] == {"max_cost_usd": 10}  # new key not in parent
    assert merged["runtime"]["screenshot_on"] == ["verify", "publish"]  # list replaced, not concatenated
    assert merged["gates"]["max_clarify_rounds"] == 5  # nested scalar override
    assert base_dir == FIXTURES.resolve()


def test_extends_result_validates_as_profile():
    profile = load_profile(FIXTURES / "child-extends.yaml")
    assert profile.name == "child"
    assert profile.budget.max_cost_usd == 10
    assert profile.runtime.screenshot_on == ["verify", "publish"]
    assert profile.base_dir == FIXTURES.resolve()


def test_extends_cycle_detection_names_both_files(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("extends: b.yaml\nname: a\n", encoding="utf-8")
    b.write_text("extends: a.yaml\nname: b\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_profile_dict(a)
    message = str(exc_info.value)
    assert "a.yaml" in message
    assert "b.yaml" in message


def test_resolve_ref_builtin_lands_under_the_package(tmp_path):
    resolved = resolve_ref("builtin:pipelines/.gitkeep", tmp_path)
    assert resolved == (builtin_root() / "pipelines" / ".gitkeep").resolve()
    assert builtin_root() in resolved.parents


def test_resolve_ref_builtin_dotdot_rejected(tmp_path):
    with pytest.raises(ConfigError):
        resolve_ref("builtin:../pyproject.toml", tmp_path)


def test_resolve_ref_builtin_dotdot_rejected_even_mid_path(tmp_path):
    with pytest.raises(ConfigError):
        resolve_ref("builtin:pipelines/../../secrets.yaml", tmp_path)


def test_resolve_ref_local_relative_path(tmp_path):
    target = tmp_path / "sub" / "file.yaml"
    target.parent.mkdir()
    target.write_text("x: 1\n", encoding="utf-8")

    resolved = resolve_ref("sub/file.yaml", tmp_path)
    assert resolved == target.resolve()


def test_resolve_ref_missing_path_raises(tmp_path):
    with pytest.raises(ConfigError):
        resolve_ref("does-not-exist.yaml", tmp_path)


def test_env_ref_survives_load_and_resolved_yaml_round_trip(tmp_path, monkeypatch):
    profile_path = tmp_path / "secret.yaml"
    profile_path.write_text(
        """
name: secret-profile
source: {type: jira, token: "${JIRA_API_TOKEN}"}
sink: {type: file}
repo: {type: git_local, path: "."}
model:
  default: main
  providers:
    main: {type: anthropic, model: claude-opus-5}
executor:
  default: inline
  kinds:
    inline: {type: api, model: main}
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

    profile = load_profile(profile_path)
    assert profile.source.opt("token") == "${JIRA_API_TOKEN}"

    dumped = resolved_yaml(profile)
    assert "${JIRA_API_TOKEN}" in dumped


def test_expand_env_substitutes_from_injected_mapping():
    assert expand_env("${FOO}-${BAR}", {"FOO": "a", "BAR": "b"}) == "a-b"


def test_expand_env_non_str_input_returned_unchanged():
    assert expand_env(42, {}) == 42
    assert expand_env(None, {}) is None


def test_expand_env_missing_variable_raises_naming_it():
    with pytest.raises(MissingEnvError) as exc_info:
        expand_env("${MISSING_TOKEN}", {})
    assert "MISSING_TOKEN" in str(exc_info.value)


def test_expand_env_empty_value_treated_as_missing():
    with pytest.raises(MissingEnvError):
        expand_env("${EMPTY}", {"EMPTY": ""})


def test_has_env_ref():
    assert has_env_ref("${FOO}") is True
    assert has_env_ref("plain text") is False
    assert has_env_ref(123) is False


def test_load_yaml_safe_load_rejects_python_object_tags(tmp_path):
    malicious = tmp_path / "evil.yaml"
    malicious.write_text(
        "cmd: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_yaml(malicious)


def test_load_yaml_non_mapping_document_raises(tmp_path):
    listy = tmp_path / "list.yaml"
    listy.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml(listy)


def test_load_yaml_empty_document_is_empty_dict(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_yaml(empty) == {}
