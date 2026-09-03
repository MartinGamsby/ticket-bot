"""`ExecOut`/`Runtime` — the shared shape every runtime kind (`none`, `local_shell`,
`solari`) is driven through, plus `BaseRuntime`, a mixin that makes `start()`/
`stop()` idempotent and adds the `with runtime:` context-manager form.

Runtime is not a model. It is where a step's shell commands run and where its
screenshots come from — `executors/tools.py`'s `shell.run` and
`runtime.screenshot` tool handlers call straight through this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ExecOut:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class RuntimeAdapterError(RuntimeError):
    """A configuration or environment problem in a runtime adapter."""


class RuntimeUnavailable(RuntimeAdapterError):
    """The requested operation is not supported by this runtime / mode."""


@runtime_checkable
class Runtime(Protocol):
    # Optional capability flag; callers read it with `getattr(rt, "can_exec", True)`
    # rather than requiring it, so a duck-typed runtime need not declare it.
    # False means `exec()` raises `RuntimeUnavailable` and the caller should run
    # the command itself instead. See `BaseRuntime`.

    def describe(self) -> str: ...  # "Solari desktop 1280x720" / "none"

    def start(self) -> None: ...  # idempotent

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecOut: ...

    def read_file(self, path: str) -> bytes: ...

    def write_file(self, path: str, data: bytes) -> None: ...

    def screenshot(self) -> bytes | None: ...  # PNG bytes, or None when not applicable

    def preview_url(self, port: int) -> str | None: ...

    def stop(self) -> None: ...  # idempotent; safe to call without start()


class BaseRuntime:
    """Mixin providing `__enter__`/`__exit__` (start/stop) and a `_started` flag.

    A subclass with nothing but no-op startup/teardown (`NoneRuntime`,
    `LocalShellRuntime`) gets an idempotent `start()`/`stop()` for free by
    overriding `_do_start()`/`_do_stop()`. A subclass with real lifecycle
    subtleties (`SolariRuntime`, which must let `stop()` clean up a partial
    `start()`) overrides `start()`/`stop()` directly instead and is responsible
    for its own idempotency.

    `_started` is deliberately a plain class attribute, not a dataclass field:
    concrete runtimes have their own `__init__` that parses an `AdapterConfig`
    and never calls `super().__init__()`, so a dataclass-generated `__init__`
    here would simply be shadowed and never run.

    `can_exec` is the capability flag callers use INSTEAD of `runtime is not None`
    to decide whether to route a command here: `NoneRuntime` and `solari` in
    `mode: desktop`/`browser` have no command-execution surface and raise
    `RuntimeUnavailable` from `exec()`. `executors/tools.py: _shell_run` reads it
    duck-typed (`getattr(runtime, "can_exec", True)`) and falls back to a local
    subprocess when it is False, which is what keeps `shell.run` working under the
    default `runtime: {type: none}`.
    """

    _started: bool = False
    can_exec: bool = True

    def start(self) -> None:
        if self._started:
            return
        self._do_start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._do_stop()
        finally:
            self._started = False

    def _do_start(self) -> None:
        """Overridden by subclasses that need real startup work."""

    def _do_stop(self) -> None:
        """Overridden by subclasses that need real teardown work."""

    def __enter__(self) -> "BaseRuntime":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
