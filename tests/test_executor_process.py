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

from ticketbot.config.schema import AdapterConfig
from ticketbot.executors.base import ExecRequest, ExecutorError
from ticketbot.executors.process import ProcessExecutor


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
