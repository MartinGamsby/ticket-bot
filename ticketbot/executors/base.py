"""`ExecRequest`/`ExecResult`/`Executor` — the shared shape every executor kind
(`process`, `api`) is driven through — plus the change-detection helpers
(`snapshot_tree`/`diff_snapshots`) both concrete executors use to compute
`files_written`, and `finish_result`, which parses the `QUESTION:`/`DEFER:`
protocol out of a step's returned text.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..engine import protocol
from ..models.base import Usage

logger = logging.getLogger(__name__)


@dataclass
class ExecRequest:
    system: str
    prompt: str
    workspace: Path  # the path-jail root; absolute
    artifacts_dir: Path  # where the step may drop files (run dir); absolute
    tools: list[str] = field(default_factory=list)  # allowlist, e.g. ["fs.read","shell.run"]
    timeout_s: int = 1800
    max_cost_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)  # extra env for `process`, post-expansion
    step_id: str = ""
    log_path: Path | None = None  # append stdout/stderr/tool traffic here
    model: str | None = None  # model SLOT name; resolved by the caller for `api`


@dataclass
class ExecResult:
    text: str = ""
    usage: Usage = Usage()
    files_written: list[Path] = field(default_factory=list)
    question: str | None = None
    defers: list[str] = field(default_factory=list)
    exit_code: int = 0
    error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0


class ExecutorError(RuntimeError):
    """A configuration or environment problem that prevents an executor from even
    attempting to run a step (a bad `cmd`, a missing executable, a missing cwd, ...).
    """


@runtime_checkable
class Executor(Protocol):
    def describe(self) -> str: ...
    def run(self, req: ExecRequest) -> ExecResult: ...


IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "runs", "dist", "build", ".mypy_cache", ".ruff_cache",
}
MAX_SNAPSHOT_FILES = 20_000


def snapshot_tree(root: Path) -> dict[Path, tuple[float, int]]:
    """Walk `root` (skipping IGNORED_DIRS, not following symlinked directories) and
    map each file to (mtime_ns_as_float, size). Stops after MAX_SNAPSHOT_FILES
    entries and logs a warning — do not hang on a huge tree.
    """
    root = Path(root)
    snapshot: dict[Path, tuple[float, int]] = {}
    if not root.is_dir():
        return snapshot

    count = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            if count >= MAX_SNAPSHOT_FILES:
                truncated = True
                break
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            snapshot[path] = (float(st.st_mtime_ns), st.st_size)
            count += 1
        if truncated:
            break

    if truncated:
        logger.warning(
            "snapshot_tree: stopped after %d files under %s (MAX_SNAPSHOT_FILES exceeded)",
            MAX_SNAPSHOT_FILES,
            root,
        )
    return snapshot


def diff_snapshots(
    before: dict[Path, tuple[float, int]], after: dict[Path, tuple[float, int]]
) -> list[Path]:
    """Files added or whose (mtime, size) changed. Sorted."""
    changed = [path for path, value in after.items() if before.get(path) != value]
    return sorted(changed)


def append_log(path: Path, text: str) -> None:
    """Append `text` to `path`, creating parent directories as needed. Callers are
    responsible for redacting `text` first — this helper never scrubs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def finish_result(text: str, **kw: Any) -> ExecResult:
    """Build an `ExecResult` with `question`/`defers` filled in from
    `protocol.parse_question(text)` / `parse_defers(text)`.
    """
    return ExecResult(
        text=text,
        question=protocol.parse_question(text),
        defers=protocol.parse_defers(text),
        **kw,
    )
