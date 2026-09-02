"""`LocalShellRuntime` -- runs `shell.run` commands as real subprocesses on this
machine, jailed to a root directory. Same security rules as `ProcessExecutor`:
`shell=False` always, `argv` is always a list (never a shell string), and the
child environment is an explicit allowlist, never `os.environ` wholesale.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...config.schema import AdapterConfig
from ...executors.tools import ToolError, jail
from .base import BaseRuntime, ExecOut, RuntimeAdapterError

# Same rationale as executors.process.DEFAULT_PASSTHROUGH: enough for a normal
# interpreter/CLI to start and find DLLs/certs on both platforms, never the
# whole parent environment.
DEFAULT_PASSTHROUGH: list[str] = [
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
    "HOME", "LANG", "LC_ALL", "PATHEXT", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA",
]


class LocalShellRuntime(BaseRuntime):
    def __init__(self, cfg: AdapterConfig, *, root: Path | None = None) -> None:
        """`root` (from the caller) wins over `cfg.opt("root")`; it is the jail for
        `cwd` and for `read_file`/`write_file`. Resolved to an absolute path now,
        at construction time, so later jail checks compare against a stable root.
        """
        configured = root if root is not None else Path(str(cfg.opt("root", ".")))
        self.root: Path = Path(configured).resolve()
        self.timeout_s: float = float(cfg.opt("timeout_s", 600))
        self.env_passthrough: list[str] = list(cfg.opt("env_passthrough") or [])

    def describe(self) -> str:
        return f"local shell ({self.root})"

    def _jail(self, candidate: str) -> Path:
        try:
            return jail(self.root, candidate)
        except ToolError as exc:
            raise RuntimeAdapterError(str(exc)) from exc

    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        names = [*DEFAULT_PASSTHROUGH, *self.env_passthrough]
        child_env = {name: os.environ[name] for name in names if name in os.environ}
        if extra:
            child_env.update(extra)
        return child_env

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecOut:
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise RuntimeAdapterError(
                "local_shell exec requires a non-empty 'argv' list of strings "
                "(never a shell string)"
            )

        resolved_cwd = self._jail(cwd) if cwd else self.root
        timeout_s = float(timeout) if timeout is not None else self.timeout_s
        child_env = self._build_env(env)

        try:
            proc = subprocess.run(
                list(argv),
                cwd=str(resolved_cwd),
                timeout=timeout_s,
                capture_output=True,
                shell=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            return ExecOut(exit_code=-1, timed_out=True, stderr=f"timed out after {timeout_s:g}s")
        except OSError as exc:
            raise RuntimeAdapterError(f"local_shell failed to start {argv[0]!r}: {exc}") from exc

        return ExecOut(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )

    def read_file(self, path: str) -> bytes:
        p = self._jail(path)
        if not p.is_file():
            raise RuntimeAdapterError(f"not a file: {path!r}")
        return p.read_bytes()

    def write_file(self, path: str, data: bytes) -> None:
        p = self._jail(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def screenshot(self) -> bytes | None:
        return None

    def preview_url(self, port: int) -> str | None:
        return f"http://127.0.0.1:{port}"
