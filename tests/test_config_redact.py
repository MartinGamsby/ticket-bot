import pytest

from ticketbot.config.redact import REDACTED, Redactor, redact, register_secret

SYNTHETIC_SECRETS = {
    "anthropic": "sk-ant-" + "a" * 32,
    "solari": "slr_live_" + "b" * 16,
    "github": "ghp_" + "c" * 20,
    "github_pat": "github_pat_" + "d" * 24,
    "atlassian": "ATATT" + "e" * 20,
    "openai": "sk-" + "f" * 24,
}


@pytest.fixture(autouse=True)
def _fresh_default_redactor(monkeypatch):
    """Isolate tests from each other: `register_secret`/`redact` share one module-level
    default Redactor, so give each test its own instance."""
    import ticketbot.config.redact as redact_module

    monkeypatch.setattr(redact_module, "_default", Redactor())


@pytest.mark.parametrize("name", SYNTHETIC_SECRETS)
def test_each_pattern_masks(name):
    secret = SYNTHETIC_SECRETS[name]
    text = f"token={secret} end"
    assert redact(text) == f"token={REDACTED} end"


def test_bearer_pattern_keeps_header_name_masks_only_credential():
    text = "Authorization: Bearer abcDEF123token"
    result = redact(text)
    assert result.startswith("Authorization: Bearer ")
    assert result.endswith(REDACTED)
    assert "abcDEF123token" not in result


def test_bearer_pattern_case_insensitive_and_basic_scheme():
    result = redact("authorization=Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in result
    assert REDACTED in result


def test_registered_literal_secret_masked_even_without_pattern_match():
    literal = "totally-custom-secret-value-123"
    register_secret(literal)
    text = f"the value is {literal} here"
    result = redact(text)
    assert literal not in result
    assert REDACTED in result


def test_register_secret_noop_for_none_short_or_whitespace():
    r = Redactor()
    r.register(None)
    r.register("short")
    r.register("        ")
    assert r.scrub("short and none stay untouched") == "short and none stay untouched"


def test_scrub_is_idempotent():
    secret = "sk-ant-" + "a" * 32
    text = f"key={secret}"
    once = redact(text)
    twice = redact(once)
    assert once == twice


def test_scrub_obj_recurses_into_nested_dicts_and_lists():
    secret = "sk-ant-" + "a" * 32
    obj = {"a": [secret, {"b": secret}], "c": ("x", secret), "d": 42}

    scrubbed = Redactor().scrub_obj(obj)

    assert scrubbed["a"][0] == REDACTED
    assert scrubbed["a"][1]["b"] == REDACTED
    assert scrubbed["c"] == ("x", REDACTED)
    assert scrubbed["d"] == 42  # non-str values pass through untouched
