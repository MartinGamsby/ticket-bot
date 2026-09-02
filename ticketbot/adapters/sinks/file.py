"""`FileSink` -- writes run results to plain files under the run directory, so the
Jira comment format (and the whole pipeline) is rehearsed even fully offline. The
orchestrator separately writes `pr.md` and `screenshots/`; this sink does not.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from ...config.redact import redact
from ...config.schema import AdapterConfig
from ...core.workitem import Attachment, WorkItem

_SEPARATOR = "\n\n---\n\n"


def _safe_filename(name: str, fallback: str = "attachment") -> str:
    """Reduce `name` to its basename so an attacker-controlled attachment
    filename (e.g. `../../evil.txt`) can never write outside `attachments/`.
    """
    candidate = Path(str(name).replace("\\", "/")).name.strip()
    if not candidate or candidate in {".", ".."}:
        return fallback
    return candidate


class FileSink:
    def __init__(self, cfg: AdapterConfig, *, run_dir: Path | None = None) -> None:
        configured = cfg.opt("dir")
        base = Path(str(configured)) if configured else (run_dir if run_dir is not None else Path("."))
        self.dir = Path(base).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        return "file"

    def _append(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(redact(text))

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None:
        comment_path = self.dir / "ticket_comment.md"
        prefix = _SEPARATOR if comment_path.exists() else ""
        self._append(comment_path, prefix + markdown)

        copied: list[str] = []
        if attachments:
            attachments_dir = (self.dir / "attachments").resolve()
            attachments_dir.mkdir(parents=True, exist_ok=True)
            for att in attachments:
                name = _safe_filename(att.filename)
                dest = (attachments_dir / name).resolve()
                if dest != attachments_dir and not dest.is_relative_to(attachments_dir):
                    continue  # never write outside attachments/
                dest.write_bytes(att.read_bytes())
                copied.append(name)

        summary = f"- comment ({len(markdown)} chars"
        if copied:
            summary += f", attachments: {', '.join(copied)}"
        summary += f") @ {self._timestamp()}\n"
        self._append(self.dir / "result.md", summary)

    def transition(self, item: WorkItem, state: str) -> None:
        self._append(self.dir / "result.md", f"- transition -> {state}\n")

    def unassign(self, item: WorkItem) -> None:
        self._append(self.dir / "result.md", "- unassigned\n")

    def link(self, item: WorkItem, url: str, title: str) -> None:
        self._append(self.dir / "result.md", f"- link: [{title}]({url})\n")

    def close(self) -> None:
        pass
