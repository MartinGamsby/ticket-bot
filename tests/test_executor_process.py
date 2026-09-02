"""`ProcessExecutor` -- always exercised against `sys.executable`, never a real
coding CLI. Covers the security rails from section-4.md: shell=False/argv-only
(implicit in every test here since no test would pass with shell interpretation),
the executable-resolution and env-allowlist rules, the hard timeout with process
kill, and the QUESTION: protocol surfacing through `finish_result`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ticketbot.config.redact import REDACTED, Redactor
from ticketbot.config.schema import AdapterConfig
from ticketbot.executors.base import ExecRequest, ExecutorError
from ticketbot.executors.process import (
    DEFAULT_PASSTHROUGH,
    _SECRET_NAME_RE,
    ProcessExecutor,
)


def _dirs(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return ws, artifacts


def _req(ws, artifacts, prompt="", **kw) -> ExecRequest:
    return ExecRequest(system="", prompt=prompt, workspace=ws, artifacts_dir=artifacts, **kw)


def test_stdin_prompt_is_upper_cased_by_child(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        prompt="stdin",
    )
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts, prompt="hello world"))

    assert result.text == "HELLO WORLD"
    assert result.exit_code == 0
    assert result.error is None
    assert result.ok is True


def test_default_prompt_mode_is_stdin(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
    )
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts, prompt="default mode"))

    assert result.text == "DEFAULT MODE"


def test_files_written_detects_a_new_file_in_the_workspace(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    target = ws / "out.txt"
    script = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('hi')"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script, str(target)], prompt="stdin")
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert target.resolve() in result.files_written
    assert target.read_text() == "hi"


def test_cmd_given_as_a_string_raises_executor_error():
    cfg = AdapterConfig(type="process", cmd="claude -p")
    with pytest.raises(ExecutorError):
        ProcessExecutor(cfg)


def test_missing_executable_raises_executor_error_naming_it(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    cfg = AdapterConfig(type="process", cmd=["totally-not-a-real-executable-xyz"])
    executor = ProcessExecutor(cfg)

    with pytest.raises(ExecutorError) as excinfo:
        executor.run(_req(ws, artifacts))
    assert "totally-not-a-real-executable-xyz" in str(excinfo.value)


def test_sleeping_child_times_out_and_does_not_hang_the_suite(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        prompt="stdin",
    )
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts, timeout_s=1))

    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.error is not None
    assert "timed out" in result.error


def test_nonzero_exit_populates_error_and_preserves_text(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    script = "import sys; sys.stdout.write('partial output'); sys.exit(3)"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin")
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert result.exit_code == 3
    assert result.text == "partial output"
    assert result.error is not None
    assert "exit 3" in result.error
    assert result.ok is False


def test_child_env_contains_only_allowlisted_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETBOT_SECRET", "should-not-leak-to-child")
    ws, artifacts = _dirs(tmp_path)
    script = (
        "import os, sys\n"
        "sys.stdout.write('SECRET=' + os.environ.get('TICKETBOT_SECRET', '<absent>') + '\\n')\n"
        "sys.stdout.write('PATH=' + ('<present>' if os.environ.get('PATH') else '<absent>'))\n"
    )
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin")
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert "SECRET=<absent>" in result.text
    assert "PATH=<present>" in result.text


def test_env_passthrough_adds_extra_names(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_ALLOWED_VAR", "visible")
    ws, artifacts = _dirs(tmp_path)
    script = "import os, sys; sys.stdout.write(os.environ.get('MY_ALLOWED_VAR', '<absent>'))"
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", script],
        prompt="stdin",
        env_passthrough=["MY_ALLOWED_VAR"],
    )
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert result.text == "visible"


# ---- the credential contract: the spawned CLI authenticates itself --------------
#
# `claude -p` / `codex exec` sign in from their OWN credential store (an OAuth
# profile under the user's home, or the OS keyring), not from an API key we hand
# them. The default allowlist therefore has to carry every non-secret LOCATOR that
# store needs to be findable -- on Windows AND on POSIX -- while still carrying no
# credential of its own. See `DEFAULT_PASSTHROUGH` and README "A spawned coding CLI
# authenticates itself".


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("USERPROFILE", "windows: %USERPROFILE%\\.claude"),
        ("APPDATA", "windows: roaming config"),
        ("LOCALAPPDATA", "windows: local config / DPAPI-backed credential files"),
        ("HOME", "posix: ~/.claude, ~/.codex"),
        ("XDG_CONFIG_HOME", "posix: relocated config root"),
        ("XDG_DATA_HOME", "posix: relocated data root"),
        ("XDG_CACHE_HOME", "posix: relocated cache root"),
        ("XDG_RUNTIME_DIR", "linux: half of reaching a Secret Service keyring"),
        ("DBUS_SESSION_BUS_ADDRESS", "linux: the other half -- without it, no keyring"),
    ],
)
def test_credential_store_locators_reach_the_child(tmp_path, monkeypatch, name, why):
    """Each of these is what lets the CLI FIND its own credentials. Drop one and the
    CLI starts unauthenticated on that platform, with no error we would recognize.
    """
    monkeypatch.setenv(name, f"locator-value-for-{name}")
    ws, artifacts = _dirs(tmp_path)
    script = f"import os, sys; sys.stdout.write(os.environ.get({name!r}, '<absent>'))"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin")

    result = ProcessExecutor(cfg).run(_req(ws, artifacts))

    assert result.text == f"locator-value-for-{name}", why


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY"]
)
def test_no_api_key_is_forwarded_by_default(tmp_path, monkeypatch, name):
    """The other half of the contract: a key in the parent environment is NOT a
    silent grant to every CLI a profile spawns. A profile that wants one names it.
    """
    monkeypatch.setenv(name, "sk-must-not-leak-by-default")
    ws, artifacts = _dirs(tmp_path)
    script = f"import os, sys; sys.stdout.write(os.environ.get({name!r}, '<absent>'))"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin")

    result = ProcessExecutor(cfg).run(_req(ws, artifacts))

    assert result.text == "<absent>"
    assert name not in DEFAULT_PASSTHROUGH


def test_default_passthrough_declares_no_credential_shaped_name():
    """A standing guard on the list itself: nothing in it may read like a secret."""
    assert [n for n in DEFAULT_PASSTHROUGH if _SECRET_NAME_RE.search(n)] == []


def test_a_deliberately_forwarded_credential_is_scrubbed_from_the_step_log(
    tmp_path, monkeypatch
):
    """A `${ENV}` value in `env:` is `register_secret()`'d, so it never survives into
    `runs/<id>/logs/`. A name forwarded via `env_passthrough:` must get the same
    treatment even though its VALUE never appeared in the profile -- otherwise the
    documented "use env_passthrough, not env" advice would trade a working profile
    for a leaking log.
    """
    import ticketbot.config.redact as redact_module

    monkeypatch.setattr(redact_module, "_default", Redactor())
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-live-abcdefghijklmnop")

    ws, artifacts = _dirs(tmp_path)
    log_path = artifacts / "logs" / "implement.log"
    script = "import os, sys; sys.stdout.write('using ' + os.environ['MY_SERVICE_TOKEN'])"
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", script],
        prompt="stdin",
        env_passthrough=["MY_SERVICE_TOKEN"],
    )

    result = ProcessExecutor(cfg).run(_req(ws, artifacts, log_path=log_path))

    assert result.text == "using tok-live-abcdefghijklmnop"  # the child really got it
    logged = log_path.read_text(encoding="utf-8")
    assert "tok-live-abcdefghijklmnop" not in logged
    assert REDACTED in logged


def test_a_forwarded_path_is_not_turned_into_a_redaction_pattern(tmp_path, monkeypatch):
    """The counterpart: matching is on the NAME, not the value. `CLAUDE_CONFIG_DIR`
    is a path, and registering it would blank that path out of every log line in the
    process -- turning a diagnostic into a puzzle.
    """
    import ticketbot.config.redact as redact_module

    monkeypatch.setattr(redact_module, "_default", Redactor())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))

    ws, artifacts = _dirs(tmp_path)
    log_path = artifacts / "logs" / "implement.log"
    script = "import os, sys; sys.stdout.write('config at ' + os.environ['CLAUDE_CONFIG_DIR'])"
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", script],
        prompt="stdin",
        env_passthrough=["CLAUDE_CONFIG_DIR"],
    )

    ProcessExecutor(cfg).run(_req(ws, artifacts, log_path=log_path))

    assert str(tmp_path / "claude-config") in log_path.read_text(encoding="utf-8")


def test_an_unset_env_passthrough_name_is_skipped_not_an_error(tmp_path, monkeypatch):
    """`ANTHROPIC_API_KEY` is absent by design on a machine that signs in by OAuth.
    Forwarding it opportunistically is what lets ONE profile serve both, which is
    the whole reason the shipped profiles use `env_passthrough:` rather than an
    `env: {..: "${ANTHROPIC_API_KEY}"}` ref (expanded strictly -> a failed run).
    """
    monkeypatch.delenv("DEFINITELY_NOT_SET_ANYWHERE", raising=False)
    ws, artifacts = _dirs(tmp_path)
    script = (
        "import os, sys; "
        "sys.stdout.write(os.environ.get('DEFINITELY_NOT_SET_ANYWHERE', '<absent>'))"
    )
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", script],
        prompt="stdin",
        env_passthrough=["DEFINITELY_NOT_SET_ANYWHERE"],
    )

    result = ProcessExecutor(cfg).run(_req(ws, artifacts))

    assert result.error is None
    assert result.text == "<absent>"


def test_cfg_env_is_expanded_and_registered_as_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "sk-ant-abcdefghijklmnopqrstuvwx")
    ws, artifacts = _dirs(tmp_path)
    script = "import os, sys; sys.stdout.write(os.environ.get('API_TOKEN', '<absent>'))"
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", script],
        prompt="stdin",
        env={"API_TOKEN": "${SOME_TOKEN}"},
    )
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert result.text == "sk-ant-abcdefghijklmnopqrstuvwx"


def test_question_marker_surfaces_in_result_question(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    script = "import sys; sys.stdout.write('Some notes.\\nQUESTION:\\nWhich database?')"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin")
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert result.question is not None
    assert "Which database?" in result.question


def test_arg_prompt_mode_appends_prompt_as_final_argv_element(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    script = "import sys; sys.stdout.write(sys.argv[-1].upper())"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="arg")
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts, prompt="from argv"))

    assert result.text == "FROM ARGV"


def test_file_prompt_mode_writes_prompt_file_and_substitutes_placeholder(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    script = "import sys, pathlib; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())"
    cfg = AdapterConfig(
        type="process",
        cmd=[sys.executable, "-c", script],
        prompt="file",
        args_template=["{prompt_file}"],
        prompt_file_name="the_prompt.txt",
    )
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts, prompt="from a file"))

    assert result.text == "from a file"
    assert (artifacts / "the_prompt.txt").exists()


def test_cwd_mode_artifacts_runs_in_the_artifacts_dir(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    script = "import os, sys; sys.stdout.write(os.getcwd())"
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin", cwd="artifacts")
    executor = ProcessExecutor(cfg)

    result = executor.run(_req(ws, artifacts))

    assert Path(result.text).resolve() == artifacts.resolve()


def test_log_path_receives_redacted_stdout_and_stderr(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    script = (
        "import sys; "
        "sys.stdout.write('token sk-ant-abcdefghijklmnopqrstuvwx in stdout'); "
        "sys.stderr.write('nothing secret in stderr')"
    )
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", script], prompt="stdin")
    executor = ProcessExecutor(cfg)
    log_path = tmp_path / "logs" / "step.log"

    executor.run(_req(ws, artifacts, log_path=log_path))

    logged = log_path.read_text()
    assert "sk-ant-abcdefghijklmnopqrstuvwx" not in logged
    assert "REDACTED" in logged
    assert "nothing secret in stderr" in logged


def test_describe_joins_cmd_with_spaces():
    cfg = AdapterConfig(type="process", cmd=["claude", "-p", "--flag"])
    executor = ProcessExecutor(cfg)
    assert executor.describe() == "process: claude -p --flag"


def test_invalid_prompt_mode_raises_executor_error():
    cfg = AdapterConfig(type="process", cmd=["claude"], prompt="carrier-pigeon")
    with pytest.raises(ExecutorError):
        ProcessExecutor(cfg)


def test_missing_cwd_directory_raises_executor_error(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    cfg = AdapterConfig(type="process", cmd=[sys.executable, "-c", "pass"], prompt="stdin")
    executor = ProcessExecutor(cfg)
    missing = ws / "does-not-exist"

    with pytest.raises(ExecutorError):
        executor.run(_req(missing, artifacts))


def test_cmd_with_non_string_item_raises_executor_error():
    cfg = AdapterConfig(type="process", cmd=[sys.executable, 123])
    with pytest.raises(ExecutorError):
        ProcessExecutor(cfg)


def test_empty_cmd_list_raises_executor_error():
    cfg = AdapterConfig(type="process", cmd=[])
    with pytest.raises(ExecutorError):
        ProcessExecutor(cfg)
