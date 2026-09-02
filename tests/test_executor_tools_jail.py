"""The key security tests for `executors/tools.py`: the path jail, the tool
allowlist, and the guarantee that a bad tool call never raises out of `dispatch()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ticketbot.adapters.runtimes.none import NoneRuntime
from ticketbot.executors.tools import (
    ToolContext,
    ToolError,
    build_tools,
    from_wire,
    jail,
    wire_name,
)


def _ctx(tmp_path, **kw) -> ToolContext:
    ws = kw.pop("workspace", None) or (tmp_path / "workspace")
    ws.mkdir(exist_ok=True)
    artifacts = kw.pop("artifacts_dir", None) or (tmp_path / "artifacts")
    artifacts.mkdir(exist_ok=True)
    return ToolContext(workspace=ws, artifacts_dir=artifacts, **kw)


# --------------------------------------------------------------------------- #
# jail() -- rejections
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("candidate", ["../outside.txt", "a/../../outside.txt"])
def test_jail_rejects_relative_escapes(tmp_path, candidate):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ToolError):
        jail(ws, candidate)


def test_jail_rejects_absolute_path_outside_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("shh")
    with pytest.raises(ToolError):
        jail(ws, str(secret))


def test_jail_rejects_nul_byte(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ToolError):
        jail(ws, "a\x00b.txt")


def test_jail_rejects_empty_string(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ToolError):
        jail(ws, "")


def test_jail_rejects_overlong_path(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ToolError):
        jail(ws, "a" * 5000)


def test_jail_error_never_reveals_the_absolute_workspace_path(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ToolError) as excinfo:
        jail(ws, "../outside.txt")
    assert str(ws.resolve()) not in str(excinfo.value)


def test_jail_rejects_symlink_inside_workspace_pointing_outside(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("shh")
    link = ws / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not available on this platform/user")

    with pytest.raises(ToolError):
        jail(ws, "escape/secret.txt")


def test_jail_rejects_new_file_under_a_symlinked_ancestor(tmp_path):
    """A write target that doesn't exist yet must also be rejected when its
    nearest EXISTING ancestor is a symlink pointing outside the workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not available on this platform/user")

    with pytest.raises(ToolError):
        jail(ws, "escape/brand_new_file.txt")


# --------------------------------------------------------------------------- #
# jail() -- acceptances
# --------------------------------------------------------------------------- #


def test_jail_accepts_paths_inside_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a").mkdir()
    (ws / "a" / "b.txt").write_text("hi")

    assert jail(ws, "a/b.txt") == (ws / "a" / "b.txt").resolve()
    assert jail(ws, "./a/b.txt") == (ws / "a" / "b.txt").resolve()


def test_jail_accepts_the_workspace_root_itself(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert jail(ws, ".") == ws.resolve()


def test_jail_accepts_a_new_file_in_a_new_nested_directory(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    # nothing under ws/new/nested exists yet -- ancestor walk lands back on ws.
    assert jail(ws, "new/nested/file.txt") == (ws / "new" / "nested" / "file.txt").resolve()


# --------------------------------------------------------------------------- #
# the SECOND permitted root: the artifacts dir (runs/<id>/)
#
# `jail()` itself stays single-root; `_jailed()` is what tries the artifacts dir
# after the workspace. The role prompts render `{plan_file}`, `{section_file}` and
# `{run_dir}/pr.md` as ABSOLUTE run-dir paths, so a workspace-only tool path
# resolver refuses every artifact the planner and reporter are told to write.
# --------------------------------------------------------------------------- #


def test_fs_write_accepts_an_absolute_path_under_the_artifacts_dir(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.write"})
    _, dispatch = build_tools(["fs.write"], ctx)
    target = ctx.artifacts_dir / "sections" / "section-1.md"

    out, is_err = dispatch("fs_write", {"path": str(target), "content": "# one\n"})

    assert is_err is False, out
    assert target.read_text(encoding="utf-8") == "# one\n"


def test_fs_read_accepts_an_absolute_path_under_the_artifacts_dir(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    (ctx.artifacts_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    _, dispatch = build_tools(["fs.read"], ctx)

    out, is_err = dispatch("fs_read", {"path": str(ctx.artifacts_dir / "plan.md")})

    assert is_err is False
    assert out.strip() == "# Plan"


def test_a_relative_path_still_means_the_workspace_not_the_artifacts_dir(tmp_path):
    """The workspace is tried FIRST, so adding the second root must not quietly
    redirect the coder's ordinary `fs.write("src/a.py", ...)` into the run dir."""
    ctx = _ctx(tmp_path, allow={"fs.write"})
    _, dispatch = build_tools(["fs.write"], ctx)

    out, is_err = dispatch("fs_write", {"path": "src/a.py", "content": "x\n"})

    assert is_err is False, out
    assert (ctx.workspace / "src" / "a.py").exists()
    assert not (ctx.artifacts_dir / "src" / "a.py").exists()


def test_a_path_outside_both_roots_is_still_refused(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.write"})
    outside = tmp_path / "outside"
    outside.mkdir()
    _, dispatch = build_tools(["fs.write"], ctx)

    out, is_err = dispatch("fs_write", {"path": str(outside / "evil.txt"), "content": "x"})

    assert is_err is True
    assert "escapes the workspace" in out
    assert not (outside / "evil.txt").exists()


def test_the_two_root_error_message_still_hides_both_absolute_paths(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    _, dispatch = build_tools(["fs.read"], ctx)

    out, _is_err = dispatch("fs_read", {"path": "../../outside.txt"})

    assert str(Path(ctx.workspace).resolve()) not in out
    assert str(Path(ctx.artifacts_dir).resolve()) not in out


# --------------------------------------------------------------------------- #
# fs.write / fs.edit failure modes
# --------------------------------------------------------------------------- #


def test_fs_write_refuses_oversize_content(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.write"}, max_write_bytes=5)
    _, dispatch = build_tools(["fs.write"], ctx)

    out, is_err = dispatch("fs_write", {"path": "big.txt", "content": "way too long"})

    assert is_err is True
    assert "too large" in out
    assert not (ctx.workspace / "big.txt").exists()


def test_fs_edit_errors_on_non_unique_old(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.edit"})
    (ctx.workspace / "f.txt").write_text("foo foo foo")
    _, dispatch = build_tools(["fs.edit"], ctx)

    out, is_err = dispatch("fs_edit", {"path": "f.txt", "old": "foo", "new": "bar"})

    assert is_err is True
    assert "not unique" in out
    assert (ctx.workspace / "f.txt").read_text() == "foo foo foo"


def test_fs_edit_replace_all_succeeds_on_non_unique_old(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.edit"})
    (ctx.workspace / "f.txt").write_text("foo foo foo")
    _, dispatch = build_tools(["fs.edit"], ctx)

    out, is_err = dispatch(
        "fs_edit", {"path": "f.txt", "old": "foo", "new": "bar", "replace_all": True}
    )

    assert is_err is False
    assert (ctx.workspace / "f.txt").read_text() == "bar bar bar"


# --------------------------------------------------------------------------- #
# allowlist enforcement
# --------------------------------------------------------------------------- #


def test_shell_run_absent_from_build_tools_when_not_allowlisted(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    tool_defs, _ = build_tools(["fs.read"], ctx)
    assert "shell_run" not in {t.name for t in tool_defs}


def test_shell_run_called_anyway_returns_an_error_not_an_execution(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    _, dispatch = build_tools(["fs.read"], ctx)

    out, is_err = dispatch("shell_run", {"argv": [sys.executable, "-c", "print('nope')"]})

    assert is_err is True
    assert "not allowlisted" in out


def test_shell_run_executes_when_allowlisted(tmp_path):
    ctx = _ctx(tmp_path, allow={"shell.run"})
    _, dispatch = build_tools(["shell.run"], ctx)

    out, is_err = dispatch("shell_run", {"argv": [sys.executable, "-c", "print('hi')"]})

    assert is_err is False
    assert "exit=0" in out
    assert "hi" in out


def test_shell_run_falls_back_to_local_execution_when_the_runtime_cannot_exec(tmp_path):
    """`NoneRuntime` is an OBJECT, not `None`, and its `exec()` raises. A plain
    `runtime is not None` check therefore failed every `shell.run` under the
    default `runtime: {type: none}` -- which `implement` and `verify` are granted
    in all three built-in pipelines."""
    ctx = _ctx(tmp_path, allow={"shell.run"}, runtime=NoneRuntime())
    _, dispatch = build_tools(["shell.run"], ctx)

    out, is_err = dispatch("shell_run", {"argv": [sys.executable, "-c", "print('local')"]})

    assert is_err is False, out
    assert "exit=0" in out
    assert "local" in out
    assert "cannot execute commands" not in out


def test_shell_run_still_routes_through_a_runtime_that_can_execute(tmp_path):
    """A runtime that never declares `can_exec` at all is assumed capable, so the
    duck-typed default cannot silently strand a third-party runtime."""

    class _Recording:
        def __init__(self) -> None:
            self.argv: list[str] | None = None

        def exec(self, argv, *, cwd=None, timeout=None):  # noqa: ANN001
            from ticketbot.adapters.runtimes.base import ExecOut

            self.argv = list(argv)
            return ExecOut(exit_code=0, stdout="from-the-runtime")

    runtime = _Recording()
    assert not hasattr(runtime, "can_exec")
    ctx = _ctx(tmp_path, allow={"shell.run"}, runtime=runtime)
    _, dispatch = build_tools(["shell.run"], ctx)

    out, is_err = dispatch("shell_run", {"argv": ["echo", "hi"]})

    assert is_err is False
    assert runtime.argv == ["echo", "hi"]
    assert "from-the-runtime" in out


def test_source_read_returns_the_work_item_text_it_was_given(tmp_path):
    ctx = _ctx(tmp_path, allow={"source.read"}, work_item_text="ENG-1: Add /health\n\nBody.")
    _, dispatch = build_tools(["source.read"], ctx)

    out, is_err = dispatch("source_read", {})

    assert is_err is False
    assert "ENG-1: Add /health" in out


def test_build_tools_skips_unknown_tool_names(tmp_path):
    ctx = _ctx(tmp_path, allow={"totally.bogus"})
    tool_defs, _ = build_tools(["totally.bogus"], ctx)
    assert tool_defs == []


def test_build_tools_skips_sink_only_names_silently(tmp_path):
    ctx = _ctx(tmp_path, allow={"sink.comment", "sink.unassign"})
    tool_defs, _ = build_tools(["sink.comment", "sink.unassign"], ctx)
    assert tool_defs == []


def test_a_tool_not_in_the_allowlist_is_refused_even_if_advertised(tmp_path):
    # fs.write is a real catalogue tool, but the allowlist passed to the context
    # doesn't include it -- dispatch must refuse rather than execute.
    ctx = _ctx(tmp_path, allow={"fs.read"})
    _, dispatch = build_tools(["fs.read"], ctx)

    out, is_err = dispatch("fs_write", {"path": "x.txt", "content": "hi"})

    assert is_err is True
    assert not (ctx.workspace / "x.txt").exists()


# --------------------------------------------------------------------------- #
# dispatch() never raises
# --------------------------------------------------------------------------- #


def test_dispatch_returns_error_tuple_for_unknown_tool(tmp_path):
    ctx = _ctx(tmp_path, allow={"totally.unknown"})
    _, dispatch = build_tools([], ctx)

    out, is_err = dispatch("totally_unknown", {})

    assert is_err is True
    assert isinstance(out, str)


def test_dispatch_returns_error_tuple_for_missing_required_args(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    _, dispatch = build_tools(["fs.read"], ctx)

    out, is_err = dispatch("fs_read", {})

    assert is_err is True


def test_dispatch_returns_error_tuple_for_a_path_escape(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    _, dispatch = build_tools(["fs.read"], ctx)

    out, is_err = dispatch("fs_read", {"path": "../outside.txt"})

    assert is_err is True
    assert isinstance(out, str)


def test_dispatch_returns_error_tuple_for_missing_file(tmp_path):
    ctx = _ctx(tmp_path, allow={"fs.read"})
    _, dispatch = build_tools(["fs.read"], ctx)

    out, is_err = dispatch("fs_read", {"path": "does_not_exist.txt"})

    assert is_err is True


# --------------------------------------------------------------------------- #
# runtime.screenshot without a runtime
# --------------------------------------------------------------------------- #


def test_runtime_screenshot_without_runtime_returns_sentinel(tmp_path):
    ctx = _ctx(tmp_path, allow={"runtime.screenshot"}, runtime=None)
    _, dispatch = build_tools(["runtime.screenshot"], ctx)

    out, is_err = dispatch("runtime_screenshot", {})

    assert is_err is False
    assert out == "no runtime configured"


# --------------------------------------------------------------------------- #
# wire_name / from_wire
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "dotted",
    ["fs.read", "fs.write", "fs.edit", "fs.list", "shell.run", "runtime.screenshot", "source.read"],
)
def test_wire_name_round_trips(dotted):
    assert from_wire(wire_name(dotted)) == dotted


def test_wire_name_maps_dots_to_underscores():
    assert wire_name("fs.read") == "fs_read"


def test_from_wire_maps_underscores_to_dots():
    assert from_wire("fs_read") == "fs.read"
