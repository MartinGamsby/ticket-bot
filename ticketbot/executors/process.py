"""`ProcessExecutor` — spawns a coding CLI (`claude -p`, `codex exec`, `aider`, ...)
defined entirely by config, so a flag change is a config edit, not a code change.

Non-negotiable, and tested: `shell=False` always, argv is always a list (never a
shell string, never `shlex.split`), the executable is resolved with `shutil.which`
so a relative name can't be hijacked by cwd and so a bare `claude` finds
`claude.cmd` on Windows, the child environment is an explicit allowlist (never
`os.environ` passed through wholesale), the prompt goes via stdin by default
(Windows caps a command line near 32 KB), and a timeout kills the whole process
tree rather than leaving an orphan running.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config.loader import expand_env
from ..config.redact import is_secret_name, redact, register_secret
from ..config.schema import AdapterConfig
from ..core.templating import render
from .base import (
    ExecRequest,
    ExecResult,
    ExecutorError,
    append_log,
    diff_snapshots,
    finish_result,
    snapshot_tree,
)

logger = logging.getLogger(__name__)

# The credential contract for a spawned coding CLI: **the CLI authenticates
# itself**. `claude -p` and `codex exec` both read their own credential store
# (an OAuth profile under the user's home, or the OS keyring) rather than an API
# key handed to them, so this list carries the non-secret LOCATORS that store
# needs to be findable -- and nothing that is itself a credential.
#
#   Windows  USERPROFILE, APPDATA, LOCALAPPDATA  (`%USERPROFILE%\.claude`,
#            `%APPDATA%`-rooted config, DPAPI-backed credential files)
#   POSIX    HOME, XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_CACHE_HOME
#   Linux    XDG_RUNTIME_DIR + DBUS_SESSION_BUS_ADDRESS -- without BOTH, a
#            Secret Service keyring cannot even be reached, and a CLI that
#            stores its token there starts unauthenticated
#   both     PATH/PATHEXT/COMSPEC/SYSTEMROOT/WINDIR/PROGRAMDATA to start at all,
#            TEMP/TMP/TMPDIR to write scratch files, LANG/LC_ALL for encoding
#
# An API KEY is never added here. A profile that wants one forwarded says so in
# its own `env_passthrough:` (see `profiles/jira-claude-solari.yaml`) -- that
# keeps "this credential goes into this subprocess" a visible, per-profile
# decision instead of a silent default. Never `os.environ` wholesale: that would
# hand EVERY key in the parent process to an arbitrary CLI.
DEFAULT_PASSTHROUGH: list[str] = [
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "USERPROFILE",
    "HOME", "LANG", "LC_ALL", "PATHEXT", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
]

# A forwarded variable whose NAME reads like a credential is registered with the
# redactor (`is_secret_name`, shared with `config.dotenv`), so the child's own
# stdout/stderr -- appended to `runs/<id>/logs/` -- can never echo it back in the
# clear.

_PROMPT_MODES = {"stdin", "arg", "file"}
_TREE_KILL_GRACE_S = 5


class ProcessExecutor:
    def __init__(self, cfg: AdapterConfig) -> None:
        cmd = cfg.opt("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            raise ExecutorError(
                "executor type=process: 'cmd' must be a non-empty list of strings "
                "(never a shell string)"
            )
        self.cmd: list[str] = list(cmd)

        prompt_mode = str(cfg.opt("prompt", "stdin"))
        if prompt_mode not in _PROMPT_MODES:
            raise ExecutorError(
                f"executor type=process: 'prompt' must be one of {sorted(_PROMPT_MODES)}, "
                f"got {prompt_mode!r}"
            )
        self.prompt_mode: str = prompt_mode

        self.default_timeout_s: int = int(cfg.opt("timeout_s", 1800))
        self.cwd_mode: str = str(cfg.opt("cwd", "workspace"))
        self.env_cfg: dict[str, Any] = dict(cfg.opt("env") or {})
        self.env_passthrough: list[str] = list(cfg.opt("env_passthrough") or [])
        self.args_template: list[str] = list(cfg.opt("args_template") or [])
        self.prompt_file_name: str = str(cfg.opt("prompt_file_name", "prompt.txt"))
        self.encoding: str = str(cfg.opt("encoding", "utf-8"))

    def describe(self) -> str:
        return f"process: {' '.join(self.cmd)}"

    def run(self, req: ExecRequest) -> ExecResult:
        exe = shutil.which(self.cmd[0])
        if exe is None:
            raise ExecutorError(f"executable {self.cmd[0]!r} not found on PATH")

        cwd = Path(req.artifacts_dir) if self.cwd_mode == "artifacts" else Path(req.workspace)
        if not cwd.is_dir():
            raise ExecutorError(f"process executor cwd does not exist: {cwd}")

        prompt_file: Path | None = None
        if self.prompt_mode == "file":
            prompt_file = Path(req.artifacts_dir) / self.prompt_file_name
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            with open(prompt_file, "w", encoding=self.encoding, newline="\n") as f:
                f.write(req.prompt)

        values = {
            "prompt_file": prompt_file,
            "workspace": req.workspace,
            "artifacts_dir": req.artifacts_dir,
            "step_id": req.step_id,
            "model": req.model,
        }
        rendered_args = [render(a, values) for a in self.args_template]

        argv = [exe, *self.cmd[1:], *rendered_args]
        stdin_data: bytes | None = None
        if self.prompt_mode == "stdin":
            stdin_data = req.prompt.encode(self.encoding)
        elif self.prompt_mode == "arg":
            argv = [*argv, req.prompt]
        # file mode: the prompt is already on disk; {prompt_file} above pointed at it.

        env = self._build_env(req)
        timeout_s = req.timeout_s or self.default_timeout_s
        before = snapshot_tree(req.workspace)

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ExecutorError(f"failed to start {self.cmd[0]!r}: {exc}") from exc

        timed_out = False
        try:
            stdout_b, stderr_b = proc.communicate(input=stdin_data, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=_TREE_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                stdout_b, stderr_b = b"", b""

        stdout = stdout_b.decode(self.encoding, errors="replace")
        stderr = stderr_b.decode(self.encoding, errors="replace")

        if req.log_path is not None:
            append_log(req.log_path, redact(stdout))
            append_log(req.log_path, redact(stderr))

        files_written = diff_snapshots(before, snapshot_tree(req.workspace))

        if timed_out:
            return finish_result(
                stdout.strip(),
                files_written=files_written,
                exit_code=-1,
                error=f"timed out after {timeout_s}s",
                timed_out=True,
            )

        exit_code = proc.returncode if proc.returncode is not None else -1
        error = None
        if exit_code != 0:
            error = redact(f"exit {exit_code}: {stderr[-500:]}")

        # `usage` is left at its default `Usage()` -- a spawned CLI's token spend is
        # not observable from here; only the `api` executor can report real usage.
        return finish_result(
            stdout.strip(),
            files_written=files_written,
            exit_code=exit_code,
            error=error,
        )

    def _build_env(self, req: ExecRequest) -> dict[str, str]:
        """The child's WHOLE environment: `DEFAULT_PASSTHROUGH` (non-secret
        locators, so a CLI can find its own credential store) + the profile's
        `env_passthrough:` names that are actually set + its expanded `env:`
        values. A name the profile forwards deliberately and that reads like a
        credential is `register_secret()`'d, exactly as an `env:` value is, so it
        is scrubbed from this step's log even though its value never appeared in
        the profile.
        """
        env: dict[str, str] = {
            name: os.environ[name] for name in DEFAULT_PASSTHROUGH if name in os.environ
        }
        for name in self.env_passthrough:
            value = os.environ.get(name)
            if value is None:
                continue  # forwarded opportunistically; an unset name is not an error
            if is_secret_name(name):
                register_secret(value)
            env[name] = value
        for key, raw_value in self.env_cfg.items():
            expanded = expand_env(raw_value)
            register_secret(expanded)
            env[key] = expanded
        env.update(req.env)
        return env


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort: kill the child and, on Windows, its whole descendant tree.
    Never raises -- a timeout is already the failure we're reporting.
    """
    # `taskkill /T` FIRST, while the parent is still alive: it walks the tree from
    # this pid, and once the parent is dead Windows no longer relates the children
    # to it, so killing first would orphan the whole subtree.
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                shell=False,
                capture_output=True,
            )
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
