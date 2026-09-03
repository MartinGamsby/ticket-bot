"""`LocalShellRuntime` -- exercised only against `sys.executable`, never a real
shell string. Covers the section-5 security rails: shell=False/argv-only, the
hard timeout, the path jail (reusing `executors.tools.jail`) for `cwd` and for
`read_file`/`write_file`, and the env allowlist.
"""

from __future__ import annotations

import sys

import pytest

from ticketbot.adapters.runtimes.base import RuntimeAdapterError
from ticketbot.adapters.runtimes.local_shell import LocalShellRuntime
from ticketbot.config.schema import AdapterConfig


def _runtime(tmp_path, **opts) -> LocalShellRuntime:
    cfg = AdapterConfig(type="local_shell", **opts)
    return LocalShellRuntime(cfg, root=tmp_path)


def test_exec_runs_python_and_captures_stdout(tmp_path):
    runtime = _runtime(tmp_path)
    out = runtime.exec([sys.executable, "-c", "print(1 + 1)"])
    assert out.exit_code == 0
    assert "2" in out.stdout
    assert out.timed_out is False


def test_exec_times_out_and_does_not_hang_the_suite(tmp_path):
    runtime = _runtime(tmp_path)
    out = runtime.exec([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert out.timed_out is True
    assert out.exit_code == -1


def test_exec_cwd_outside_root_raises(tmp_path):
    outside = tmp_path.parent / "outside-root"
    outside.mkdir(exist_ok=True)
    runtime = _runtime(tmp_path)
    with pytest.raises(Exception):
        runtime.exec([sys.executable, "-c", "pass"], cwd=str(outside))


def test_exec_with_string_argv_raises(tmp_path):
    runtime = _runtime(tmp_path)
    with pytest.raises(RuntimeAdapterError):
        runtime.exec("not a list")  # type: ignore[arg-type]


def test_read_write_file_round_trip_inside_root(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.write_file("nested/out.txt", b"hello runtime")
    assert runtime.read_file("nested/out.txt") == b"hello runtime"
    assert (tmp_path / "nested" / "out.txt").read_bytes() == b"hello runtime"


def test_read_file_rejects_escape(tmp_path):
    runtime = _runtime(tmp_path)
    with pytest.raises(Exception):
        runtime.read_file("../escape.txt")


def test_write_file_rejects_escape(tmp_path):
    runtime = _runtime(tmp_path)
    with pytest.raises(Exception):
        runtime.write_file("../escape.txt", b"nope")


def test_sentinel_env_var_is_not_visible_to_child(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETBOT_LOCAL_SHELL_SECRET", "should-not-leak")
    runtime = _runtime(tmp_path)
    script = "import os, sys; sys.stdout.write(os.environ.get('TICKETBOT_LOCAL_SHELL_SECRET', '<absent>'))"
    out = runtime.exec([sys.executable, "-c", script])
    assert out.stdout == "<absent>"


def test_env_passthrough_adds_extra_names(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_LOCAL_SHELL_VAR", "visible")
    runtime = _runtime(tmp_path, env_passthrough=["MY_LOCAL_SHELL_VAR"])
    script = "import os, sys; sys.stdout.write(os.environ.get('MY_LOCAL_SHELL_VAR', '<absent>'))"
    out = runtime.exec([sys.executable, "-c", script])
    assert out.stdout == "visible"


def test_screenshot_returns_none(tmp_path):
    assert _runtime(tmp_path).screenshot() is None


def test_preview_url_returns_localhost_url(tmp_path):
    assert _runtime(tmp_path).preview_url(3000) == "http://127.0.0.1:3000"


def test_describe_includes_root(tmp_path):
    runtime = _runtime(tmp_path)
    assert str(tmp_path) in runtime.describe()


def test_explicit_root_kwarg_wins_over_cfg_root(tmp_path):
    cfg_root = tmp_path / "from-cfg"
    cfg_root.mkdir()
    caller_root = tmp_path / "from-caller"
    caller_root.mkdir()
    cfg = AdapterConfig(type="local_shell", root=str(cfg_root))
    runtime = LocalShellRuntime(cfg, root=caller_root)
    assert runtime.root == caller_root.resolve()


def test_start_and_stop_are_idempotent(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start()
    runtime.start()
    runtime.stop()
    runtime.stop()
