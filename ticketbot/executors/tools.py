"""The tool implementations behind the `api` executor's tool loop — and the path
jail every filesystem tool goes through.

**This is the path jail.** `jail()` is the single choke point that decides whether
a model-supplied path may be touched at all: it resolves symlinks, proves the
result stays inside one permitted root, and — for a write target that does not
exist yet — proves the same about its nearest existing ancestor, so a symlinked
directory cannot be used to redirect a write outside the sandbox. It rejects on
any doubt rather than clamping the path back inside the root.

There are exactly TWO permitted roots, both owned by the orchestrator, and
`_jailed()` is the only thing that knows about the second: the **workspace** (the
repo checkout a step edits) and the **artifacts dir** (`runs/<id>/`, which
`ExecRequest.artifacts_dir` names as "where the step may drop files"). The role
prompts require the second — `prompts/roles/planner.md` writes `{plan_file}` and
`{sections_dir}/section-N.md`, `coder.md` reads `{section_file}` back, and
`reporter.md` writes `{run_dir}/pr.md` — and `engine/context.py` renders all of
those as absolute paths under the run dir. A workspace-only jail refuses every one
of them, which leaves `plan.sections` empty and fails the run. A relative path is
still resolved against the workspace first, so nothing that used to land in the
repo now lands in the run dir instead.

`shell.run` is the other hot spot: `shell=False` always, argv only, and it only
runs at all when `"shell.run"` is in the step's tool allowlist (`ctx.allow`) — a
model asking for a tool it was not granted gets a `ToolError`, not an execution.
That allowlist check lives in `dispatch()` and applies to every tool, not just
`shell.run`: a name outside `ctx.allow` never reaches its handler.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.redact import redact
from ..models.base import ToolDef

logger = logging.getLogger(__name__)

DEFAULT_MAX_READ_BYTES = 1_000_000
DEFAULT_MAX_WRITE_BYTES = 5_000_000
MAX_PATH_LEN = 4096
MAX_SHELL_OUTPUT_CHARS = 20_000
MAX_LIST_ENTRIES = 500

# Tool names the orchestrator implements from the step's returned text, not here.
# Present in the catalogue conceptually, but build_tools() skips them silently.
_SINK_ONLY_NAMES = {"sink.comment", "sink.unassign"}


class ToolError(Exception):
    """Returned to the model as a tool_result with is_error=True — NEVER raised out
    of `dispatch()` / an executor's `run()`.
    """


def jail(workspace: Path, candidate: str) -> Path:
    """Resolve `candidate` inside `workspace` and prove it stays there.

    Rejects (never clamps): a NUL byte in the string, an empty string, a candidate
    longer than MAX_PATH_LEN, an absolute or relative path that resolves outside
    `workspace`, and — for a target that does not exist yet — a nearest existing
    ancestor that resolves outside `workspace` (so a symlinked parent directory
    cannot redirect a write). The error message never reveals the absolute
    workspace path.

    ONE root per call, deliberately: `_jailed()` is what tries the second permitted
    root, so this stays a single, auditable containment check.
    """
    if (
        not isinstance(candidate, str)
        or candidate == ""
        or "\x00" in candidate
        or len(candidate) > MAX_PATH_LEN
    ):
        raise ToolError(f"path escapes the workspace: {candidate!r}")

    ws = Path(workspace).resolve(strict=False)
    raw = Path(candidate)
    p = raw if raw.is_absolute() else (ws / raw)
    p = p.resolve(strict=False)

    def _inside(check: Path) -> bool:
        return check == ws or check.is_relative_to(ws)

    if not _inside(p):
        raise ToolError(f"path escapes the workspace: {candidate!r}")

    if not p.exists():
        ancestor = p.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        ancestor = ancestor.resolve(strict=False)
        if not _inside(ancestor):
            raise ToolError(f"path escapes the workspace: {candidate!r}")

    return p


@dataclass
class ToolContext:
    workspace: Path
    artifacts_dir: Path
    runtime: Any | None = None  # duck-typed Runtime from section 5, or None
    allow: set[str] = field(default_factory=set)
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES
    shell_timeout_s: int = 600
    files_written: list[Path] = field(default_factory=list)
    log: Callable[[str], None] | None = None
    work_item_text: str = ""  # ticket text for `source.read`; set by the orchestrator


def _jailed(ctx: ToolContext, candidate: str) -> Path:
    """`jail()` against the workspace, falling back to the artifacts dir.

    The workspace is tried FIRST so a relative path always means "in the repo".
    Only a path that is not under the workspace at all — in practice an absolute
    `<run_dir>/...` path from a role prompt's `{plan_file}`/`{section_file}`/
    `{run_dir}/pr.md` — gets a second chance against `ctx.artifacts_dir`. When
    both refuse, the WORKSPACE error is what propagates, so the message a model
    sees still talks about the workspace and still never names an absolute path.
    """
    try:
        return jail(ctx.workspace, candidate)
    except ToolError:
        artifacts = Path(ctx.artifacts_dir) if ctx.artifacts_dir else None
        if artifacts is None:
            raise
        try:
            return jail(artifacts, candidate)
        except ToolError:
            raise ToolError(f"path escapes the workspace: {candidate!r}") from None


def _log(ctx: ToolContext, message: str) -> None:
    if ctx.log is not None:
        ctx.log(message)
    else:
        logger.info(message)


def wire_name(name: str) -> str:
    """`fs.read` -> `fs_read` — the Anthropic API only accepts `^[a-zA-Z0-9_-]{1,128}$`."""
    return name.replace(".", "_")


def from_wire(name: str) -> str:
    """`fs_read` -> `fs.read`."""
    return name.replace("_", ".")


# --------------------------------------------------------------------------- #
# Tool handlers — each is (ctx, args) -> str, raising ToolError on failure.
# --------------------------------------------------------------------------- #


def _fs_read(ctx: ToolContext, args: dict) -> str:
    rel = args.get("path")
    if not isinstance(rel, str):
        raise ToolError("fs.read requires a 'path' string")
    path = _jailed(ctx, rel)
    if not path.is_file():
        raise ToolError(f"not a file: {rel!r}")

    data = path.read_bytes()
    truncated = len(data) > ctx.max_read_bytes
    if truncated:
        data = data[: ctx.max_read_bytes]
    text = data.decode("utf-8", errors="replace")

    offset = args.get("offset")
    limit = args.get("limit")
    if offset is not None or limit is not None:
        lines = text.split("\n")
        start = max(int(offset), 0) if offset is not None else 0
        end = start + int(limit) if limit is not None else len(lines)
        text = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines[start:end], start=start))

    if truncated:
        text += "\n…[truncated]"
    return text


def _fs_list(ctx: ToolContext, args: dict) -> str:
    rel = args.get("path") or "."
    if not isinstance(rel, str):
        raise ToolError("fs.list requires 'path' to be a string")
    path = _jailed(ctx, rel)
    if not path.is_dir():
        raise ToolError(f"not a directory: {rel!r}")

    entries = sorted(path.iterdir(), key=lambda p: p.name)
    lines = [entry.name + ("/" if entry.is_dir() else "") for entry in entries[:MAX_LIST_ENTRIES]]
    return "\n".join(lines)


def _fs_write(ctx: ToolContext, args: dict) -> str:
    rel = args.get("path")
    content = args.get("content")
    if not isinstance(rel, str):
        raise ToolError("fs.write requires a 'path' string")
    if not isinstance(content, str):
        raise ToolError("fs.write requires 'content' to be a string")

    encoded = content.encode("utf-8")
    if len(encoded) > ctx.max_write_bytes:
        raise ToolError(
            f"content too large: {len(encoded)} bytes (max {ctx.max_write_bytes})"
        )

    path = _jailed(ctx, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    ctx.files_written.append(path)
    return f"wrote {len(encoded)} bytes to {rel}"


def _fs_edit(ctx: ToolContext, args: dict) -> str:
    rel = args.get("path")
    old = args.get("old")
    new = args.get("new")
    replace_all = bool(args.get("replace_all", False))
    if not isinstance(rel, str) or not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("fs.edit requires 'path', 'old' and 'new' strings")
    if old == "":
        raise ToolError("fs.edit requires a non-empty 'old' string")

    path = _jailed(ctx, rel)
    if not path.is_file():
        raise ToolError(f"not a file: {rel!r}")

    text = path.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"'old' not found in {rel!r}")
    if count > 1 and not replace_all:
        raise ToolError(
            f"'old' is not unique in {rel!r} ({count} occurrences); pass replace_all to replace them all"
        )

    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    encoded = new_text.encode("utf-8")
    if len(encoded) > ctx.max_write_bytes:
        raise ToolError(f"result too large: {len(encoded)} bytes (max {ctx.max_write_bytes})")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    ctx.files_written.append(path)
    replaced = count if replace_all else 1
    return f"replaced {replaced} occurrence(s) in {rel}"


def _shell_run(ctx: ToolContext, args: dict) -> str:
    if "shell.run" not in ctx.allow:
        raise ToolError("shell.run is not allowlisted for this step")

    argv = args.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ToolError("shell.run requires a non-empty 'argv' list of strings")

    cwd_arg = args.get("cwd")
    cwd = jail(ctx.workspace, cwd_arg) if cwd_arg else Path(ctx.workspace).resolve(strict=False)

    requested_timeout = args.get("timeout")
    timeout_s = ctx.shell_timeout_s
    if requested_timeout is not None:
        try:
            timeout_s = min(int(requested_timeout), ctx.shell_timeout_s)
        except (TypeError, ValueError) as exc:
            raise ToolError("shell.run 'timeout' must be a number") from exc

    # Route through the runtime only when it actually HAS a command-execution
    # surface. `runtime: {type: none}` -- the default, and what
    # `profiles/file-text-none.yaml` ships -- is a real object, not `None`, whose
    # `exec()` raises `RuntimeUnavailable`; so would `solari` in `mode: desktop`
    # or `browser`. A bare `is not None` check therefore made `shell.run` fail on
    # every call under three of the four shipped profiles, for `implement` and
    # `verify` alike. `can_exec` is read duck-typed (default True) so this module
    # still knows nothing about runtime classes.
    if ctx.runtime is not None and getattr(ctx.runtime, "can_exec", True):
        out = ctx.runtime.exec(list(argv), cwd=str(cwd), timeout=timeout_s)
        exit_code = getattr(out, "exit_code", getattr(out, "returncode", 0))
        stdout = getattr(out, "stdout", "") or ""
        stderr = getattr(out, "stderr", "") or ""
        return f"exit={exit_code}\n{stdout}\n{stderr}"[:MAX_SHELL_OUTPUT_CHARS]

    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd), timeout=timeout_s, capture_output=True, shell=False
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
        text = f"exit=-1 (timed out after {timeout_s}s)\n{stdout}\n{stderr}"
        return text[:MAX_SHELL_OUTPUT_CHARS]
    except OSError as exc:
        raise ToolError(f"shell.run failed to start {argv[0]!r}: {exc}") from exc

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    text = f"exit={proc.returncode}\n{stdout}\n{stderr}"
    return text[:MAX_SHELL_OUTPUT_CHARS]


def _runtime_screenshot(ctx: ToolContext, args: dict) -> str:
    if ctx.runtime is None:
        return "no runtime configured"
    data = ctx.runtime.screenshot()
    if not data:
        return "no runtime configured"

    shots_dir = Path(ctx.artifacts_dir) / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    next_n = len(list(shots_dir.glob("tool-*.png"))) + 1
    path = shots_dir / f"tool-{next_n:02d}.png"
    path.write_bytes(data)
    ctx.files_written.append(path)
    return f"screenshots/{path.name}"


def _source_read(ctx: ToolContext, args: dict) -> str:
    return ctx.work_item_text


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


def _def(name: str, description: str, properties: dict[str, Any], required: list[str]) -> ToolDef:
    return ToolDef(
        name=wire_name(name),
        description=description,
        input_schema={"type": "object", "properties": properties, "required": required},
    )


CATALOGUE: dict[str, tuple[ToolDef, Callable[[ToolContext, dict], str]]] = {
    "fs.read": (
        _def(
            "fs.read",
            "Read a file inside the workspace. Optionally return numbered lines "
            "starting at `offset` (0-based), up to `limit` lines.",
            {
                "path": {"type": "string", "description": "Workspace-relative or absolute path."},
                "offset": {"type": "integer", "description": "0-based starting line."},
                "limit": {"type": "integer", "description": "Maximum number of lines."},
            },
            ["path"],
        ),
        _fs_read,
    ),
    "fs.list": (
        _def(
            "fs.list",
            "List the entries of a directory inside the workspace, one per line "
            "('name/' for a subdirectory).",
            {"path": {"type": "string", "description": "Defaults to the workspace root."}},
            [],
        ),
        _fs_list,
    ),
    "fs.write": (
        _def(
            "fs.write",
            "Write (create or overwrite) a file inside the workspace, creating "
            "parent directories as needed.",
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            ["path", "content"],
        ),
        _fs_write,
    ),
    "fs.edit": (
        _def(
            "fs.edit",
            "Replace an exact substring in a file inside the workspace. Fails if "
            "'old' is absent, or (without replace_all) not unique.",
            {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            ["path", "old", "new"],
        ),
        _fs_edit,
    ),
    "shell.run": (
        _def(
            "shell.run",
            "Run a command (argv list, never a shell string) inside the workspace. "
            "Only available when explicitly allowlisted for this step.",
            {
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            ["argv"],
        ),
        _shell_run,
    ),
    "runtime.screenshot": (
        _def("runtime.screenshot", "Take a screenshot of the configured runtime, if any.", {}, []),
        _runtime_screenshot,
    ),
    "source.read": (
        _def("source.read", "Read the work item's ticket text.", {}, []),
        _source_read,
    ),
}


def build_tools(
    names: Sequence[str], ctx: ToolContext
) -> tuple[list[ToolDef], Callable[[str, dict], tuple[str, bool]]]:
    """Returns (tool definitions for the allowlisted names, dispatch(name, args) ->
    (result_text, is_error)).

    - A name not in the catalogue is skipped (with a log line), not an error.
    - `sink.comment`/`sink.unassign` are known-but-not-implemented-here names the
      orchestrator handles from the step's returned text; build_tools skips them
      silently (no log line).
    - dispatch() catches ToolError and every other Exception and returns
      (redact(str(exc)), True) so a bad tool call becomes a tool_result the model
      can recover from — it must NEVER propagate out of the loop.
    - `ctx.allow` is the enforcement point: dispatch() refuses any wire name whose
      dotted form is not in `ctx.allow`, whether or not it was ever advertised in
      the returned tool definitions.
    """
    tool_defs: list[ToolDef] = []
    for name in names:
        if name in _SINK_ONLY_NAMES:
            continue
        entry = CATALOGUE.get(name)
        if entry is None:
            _log(ctx, f"build_tools: skipping unknown tool {name!r}")
            continue
        tool_defs.append(entry[0])

    def dispatch(wire: str, args: dict) -> tuple[str, bool]:
        name = from_wire(wire)
        try:
            if name not in ctx.allow:
                raise ToolError(f"tool not allowlisted for this step: {name}")
            entry = CATALOGUE.get(name)
            if entry is None:
                raise ToolError(f"unknown tool: {name}")
            _, handler = entry
            result = handler(ctx, dict(args or {}))
            return redact(result), False
        except ToolError as exc:
            return redact(str(exc)), True
        except Exception as exc:  # noqa: BLE001 - must never propagate out of the loop
            return redact(str(exc)), True

    return tool_defs, dispatch
