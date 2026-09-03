"""`.env` loading: parsing, precedence, and the redactor hand-off.

The precedence tests are the load-bearing ones. A stale `.env` in a working copy
silently shadowing a real exported credential is the failure that costs an
afternoon, so "the real environment wins" is pinned from both directions.
"""

from __future__ import annotations

import pytest

from ticketbot.cli import main
from ticketbot.config.dotenv import (
    DotenvError,
    find_dotenv,
    load_dotenv,
    parse_dotenv,
)
from ticketbot.config.redact import Redactor, is_secret_name, redact


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A=1", {"A": "1"}),
        ("export A=1", {"A": "1"}),
        ("  A = 1  ", {"A": "1"}),
        ("A=", {"A": ""}),
        ("# comment\nA=1", {"A": "1"}),
        ("\n\nA=1\n\n", {"A": "1"}),
        ('A="quoted value"', {"A": "quoted value"}),
        ("A='quoted value'", {"A": "quoted value"}),
        ("A=bare value", {"A": "bare value"}),
        ("A=1 # trailing", {"A": "1"}),
        ('A="1 # not a comment"', {"A": "1 # not a comment"}),
        ("A=sk-ant-abc#123", {"A": "sk-ant-abc#123"}),
        ("A=1\nB=2", {"A": "1", "B": "2"}),
    ],
)
def test_parse_accepts_the_shapes_a_real_env_file_uses(text, expected):
    assert parse_dotenv(text) == expected


def test_a_url_value_keeps_its_scheme_slashes_and_query():
    parsed = parse_dotenv("MODEL_BASE_URL=https://api.example.com/v1?x=1")
    assert parsed["MODEL_BASE_URL"] == "https://api.example.com/v1?x=1"


@pytest.mark.parametrize("bad", ["JUST_A_NAME", "=novalue"])
def test_a_malformed_line_raises_rather_than_vanishing(bad):
    # A typo'd credential line that is silently skipped produces a confusing
    # "unset variable" failure much later; fail at the file instead.
    with pytest.raises(DotenvError):
        parse_dotenv(bad)


def test_the_error_names_the_line_number():
    with pytest.raises(DotenvError, match="line 3"):
        parse_dotenv("A=1\nB=2\noops\n")


# --------------------------------------------------------------------------- precedence


def test_a_value_from_the_file_is_loaded_when_the_name_is_unset(tmp_path):
    (tmp_path / ".env").write_text("SOLARI_API_KEY=slr_live_fromfile\n", encoding="utf-8")
    env: dict[str, str] = {}

    applied = load_dotenv(tmp_path / ".env", env=env)

    assert applied == ["SOLARI_API_KEY"]
    assert env["SOLARI_API_KEY"] == "slr_live_fromfile"


def test_the_real_environment_wins_over_the_file(tmp_path):
    (tmp_path / ".env").write_text("SOLARI_API_KEY=slr_live_fromfile\n", encoding="utf-8")
    env = {"SOLARI_API_KEY": "slr_live_fromshell"}

    applied = load_dotenv(tmp_path / ".env", env=env)

    assert applied == []
    assert env["SOLARI_API_KEY"] == "slr_live_fromshell"


def test_override_is_opt_in(tmp_path):
    (tmp_path / ".env").write_text("A=fromfile\n", encoding="utf-8")
    env = {"A": "fromshell"}

    load_dotenv(tmp_path / ".env", override=True, env=env)

    assert env["A"] == "fromfile"


def test_an_empty_existing_value_is_treated_as_unset(tmp_path):
    # `A=` exported by a shell is indistinguishable from "not configured" in
    # practice, and every adapter treats empty as missing.
    (tmp_path / ".env").write_text("A=fromfile\n", encoding="utf-8")
    env = {"A": ""}

    load_dotenv(tmp_path / ".env", env=env)

    assert env["A"] == "fromfile"


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env", env={}) == []


# --------------------------------------------------------------------------- discovery


def test_find_dotenv_finds_one_in_the_given_directory(tmp_path):
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    assert find_dotenv(tmp_path) == tmp_path / ".env"


def test_find_dotenv_returns_none_when_there_is_none(tmp_path):
    assert find_dotenv(tmp_path) is None


def test_find_dotenv_does_not_walk_up_to_a_parent(tmp_path):
    # An ancestor's .env applying to a run in a subdirectory would silently
    # misdirect a credential.
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()

    assert find_dotenv(child) is None


# --------------------------------------------------------------------------- redaction


def test_a_credential_shaped_name_is_registered_with_the_redactor(tmp_path, monkeypatch):
    fresh = Redactor()
    monkeypatch.setattr("ticketbot.config.redact._default", fresh)
    (tmp_path / ".env").write_text("JIRA_API_TOKEN=totally-secret-value\n", encoding="utf-8")

    load_dotenv(tmp_path / ".env", env={})

    assert "totally-secret-value" not in redact("token is totally-secret-value")
    assert redact("token is totally-secret-value").endswith("***REDACTED***")


def test_a_non_credential_name_is_not_turned_into_a_redaction_pattern(
    tmp_path, monkeypatch
):
    # MODEL_BASE_URL is a locator. Registering it would mask the host in every
    # log line and banner that mentions it.
    fresh = Redactor()
    monkeypatch.setattr("ticketbot.config.redact._default", fresh)
    (tmp_path / ".env").write_text(
        "MODEL_BASE_URL=https://api.example.com/v1\n", encoding="utf-8"
    )

    load_dotenv(tmp_path / ".env", env={})

    assert redact("calling https://api.example.com/v1") == "calling https://api.example.com/v1"


@pytest.mark.parametrize(
    "name, secret",
    [
        ("ANTHROPIC_API_KEY", True),
        ("JIRA_API_TOKEN", True),
        ("SOLARI_API_KEY", True),
        ("GITHUB_TOKEN", True),
        ("CLIENT_SECRET", True),
        ("DB_PASSWORD", True),
        ("MODEL_BASE_URL", False),
        ("JIRA_EMAIL", False),
        ("CLAUDE_CONFIG_DIR", False),
        ("HOME", False),
    ],
)
def test_is_secret_name_matches_on_the_name(name, secret):
    assert is_secret_name(name) is secret


# --------------------------------------------------------------------------- CLI


def test_the_cli_loads_dotenv_from_the_working_directory(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("TICKETBOT_DOTENV_PROBE=loaded\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TICKETBOT_DOTENV_PROBE", raising=False)

    main(["config", "list", "--dir", str(tmp_path)])

    import os

    assert os.environ["TICKETBOT_DOTENV_PROBE"] == "loaded"


def test_no_env_file_skips_loading(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TICKETBOT_DOTENV_SKIPPED=nope\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TICKETBOT_DOTENV_SKIPPED", raising=False)

    main(["--no-env-file", "config", "list", "--dir", str(tmp_path)])

    import os

    assert "TICKETBOT_DOTENV_SKIPPED" not in os.environ


def test_an_explicit_env_file_that_does_not_exist_is_an_error(tmp_path, capsys):
    # The implicit ./.env is optional; a path the user typed is not.
    code = main(["--env-file", str(tmp_path / "nope.env"), "config", "list"])

    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_a_malformed_env_file_fails_the_command(tmp_path, capsys):
    bad = tmp_path / "bad.env"
    bad.write_text("A=1\nthis is not an assignment\n", encoding="utf-8")

    code = main(["--env-file", str(bad), "config", "list"])

    assert code == 2
    assert "line 2" in capsys.readouterr().err
