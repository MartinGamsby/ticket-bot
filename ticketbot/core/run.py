"""`Run`, `StepResult` and `RunStore` — everything later sections need to persist and
resume one ticket-to-PR run.

`RunStore` owns the on-disk `runs/<id>/` layout that every later section writes
into:

    runs/2026-09-01-1443-ENG-1842-a3f9/
      banner.txt   config.resolved.yaml   run.json      workitem.json
      plan.md      sections/section-1.md  patch.diff    test-report.md
      review.md    security.md            pr.md         ticket_comment.md
      question.md  dryrun.log             screenshots/verify-01.png   logs/<step>.log

`run.json` is rewritten atomically after every step (temp file + `os.replace`), so a
crash mid-step never leaves a half-written status and `resume` can trust it.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..config.redact import Redactor, default_redactor
from .workitem import WorkItem, slugify


class RunStatus(str, Enum):
    RECEIVED = "received"
    CLARIFYING = "clarifying"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    PR_OPEN = "pr_open"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    OK = "ok"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


def _iso_utc(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class StepResult:
    id: str
    role: str = ""
    status: StepStatus = StepStatus.PENDING
    started_at: str | None = None  # ISO-8601 UTC
    ended_at: str | None = None
    duration_s: float = 0.0
    cost_usd: float = 0.0
    text: str = ""  # the step's "Return ONLY:" payload
    artifacts: list[str] = field(default_factory=list)  # run-dir-relative paths
    commits: list[str] = field(default_factory=list)
    question: str | None = None
    defers: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "cost_usd": self.cost_usd,
            "text": self.text,
            "artifacts": list(self.artifacts),
            "commits": list(self.commits),
            "question": self.question,
            "defers": list(self.defers),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepResult":
        return cls(
            id=d["id"],
            role=d.get("role", ""),
            status=StepStatus(d.get("status", "pending")),
            started_at=d.get("started_at"),
            ended_at=d.get("ended_at"),
            duration_s=d.get("duration_s", 0.0),
            cost_usd=d.get("cost_usd", 0.0),
            text=d.get("text", ""),
            artifacts=list(d.get("artifacts", [])),
            commits=list(d.get("commits", [])),
            question=d.get("question"),
            defers=list(d.get("defers", [])),
            error=d.get("error"),
        )


@dataclass
class Run:
    id: str
    profile_name: str
    work_item_key: str
    external_id: str | None = None
    status: RunStatus = RunStatus.RECEIVED
    created_at: str = ""  # ISO-8601 UTC
    updated_at: str = ""
    pipeline_ref: str = ""
    pipeline_reason: str = ""
    banner: str = ""
    cost_usd: float = 0.0
    steps: dict[str, StepResult] = field(default_factory=dict)  # insertion-ordered by step id
    extra: dict[str, Any] = field(default_factory=dict)  # pr_url, branch, workspace, ...

    def step(self, step_id: str) -> StepResult:
        """Get the `StepResult` for `step_id`, creating a PENDING one if absent."""
        if step_id not in self.steps:
            self.steps[step_id] = StepResult(id=step_id)
        return self.steps[step_id]

    def is_complete(self, step_id: str) -> bool:
        """True when the step's status is OK or SKIPPED."""
        result = self.steps.get(step_id)
        return result is not None and result.status in (StepStatus.OK, StepStatus.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_name": self.profile_name,
            "work_item_key": self.work_item_key,
            "external_id": self.external_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pipeline_ref": self.pipeline_ref,
            "pipeline_reason": self.pipeline_reason,
            "banner": self.banner,
            "cost_usd": self.cost_usd,
            "steps": {step_id: step.to_dict() for step_id, step in self.steps.items()},
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Run":
        steps = {
            step_id: StepResult.from_dict(step_dict)
            for step_id, step_dict in d.get("steps", {}).items()
        }
        return cls(
            id=d["id"],
            profile_name=d.get("profile_name", ""),
            work_item_key=d.get("work_item_key", ""),
            external_id=d.get("external_id"),
            status=RunStatus(d.get("status", "received")),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            pipeline_ref=d.get("pipeline_ref", ""),
            pipeline_reason=d.get("pipeline_reason", ""),
            banner=d.get("banner", ""),
            cost_usd=d.get("cost_usd", 0.0),
            steps=steps,
            extra=dict(d.get("extra", {})),
        )


class RunStore:
    """Owns `runs/<id>/` on disk: run ids, `run.json` persistence, and artifact/log
    writes — every write of user- or model-derived text is scrubbed through a
    `Redactor` first.
    """

    def __init__(self, root: Path, redactor: Redactor | None = None) -> None:
        self.root = Path(root)
        # The SHARED redactor, not a fresh one: `register_secret()` populates the
        # module-level instance, so a private `Redactor()` here would scrub by
        # pattern only and write every adapter-expanded credential (Jira, GitHub,
        # Anthropic, Solari, a `process` executor's `env:`) verbatim into
        # `runs/<id>/` artifacts and logs.
        self.redactor = redactor if redactor is not None else default_redactor()

    def new_id(self, item_key: str, now: datetime | None = None) -> str:
        """'%Y-%m-%d-%H%M' + '-' + slug(item_key) + '-' + 4 hex chars, e.g.
        '2026-09-01-1443-eng-1842-a3f9'. Uses secrets.token_hex(2).
        """
        when = now if now is not None else datetime.now(timezone.utc)
        timestamp = when.strftime("%Y-%m-%d-%H%M")
        return f"{timestamp}-{slugify(item_key)}-{secrets.token_hex(2)}"

    def new_run(self, *, profile_name: str, item: WorkItem, now: datetime | None = None) -> Run:
        when = now if now is not None else datetime.now(timezone.utc)
        run_id = self.new_id(item.key, now=when)
        created = _iso_utc(when)
        return Run(
            id=run_id,
            profile_name=profile_name,
            work_item_key=item.key,
            external_id=item.external_id,
            created_at=created,
            updated_at=created,
        )

    def dir(self, run_id: str) -> Path:
        """<root>/<run_id>, created on demand."""
        d = self.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, run: Run) -> None:
        """Atomic write: serialize to `<dir>/run.json.tmp`, then `os.replace` onto
        `run.json`. `os.replace` is atomic for same-volume paths on Windows too —
        never use `shutil.move` here.
        """
        run_dir = self.dir(run.id)
        target = run_dir / "run.json"
        tmp = run_dir / "run.json.tmp"
        payload = json.dumps(run.to_dict(), indent=2, sort_keys=False)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp, target)

    def load(self, run_id: str) -> Run:
        """Raises FileNotFoundError if the run does not exist."""
        path = self.root / run_id / "run.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Run.from_dict(data)

    def list_ids(self) -> list[str]:
        """Run ids under `root`, sorted (chronological, since the id is timestamp-
        prefixed) — newest last.
        """
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and (p / "run.json").exists()
        )

    def latest(self) -> Run | None:
        ids = self.list_ids()
        if not ids:
            return None
        return self.load(ids[-1])

    def _jailed_relpath(self, relpath: str) -> Path:
        rel = Path(relpath.replace("\\", "/"))
        if rel.is_absolute():
            raise ValueError(f"relpath must be relative: {relpath!r}")
        if any(part == ".." for part in rel.parts):
            raise ValueError(f"relpath must not contain '..': {relpath!r}")
        return rel

    def write_artifact(self, run: Run, relpath: str, data: str | bytes) -> Path:
        """Write under the run dir, creating parent directories. `str` data is
        scrubbed through the `Redactor` and written UTF-8 with `newline='\\n'`;
        `bytes` are written verbatim. `relpath` is jailed: absolute paths and any
        '..' segment raise `ValueError`. Returns the written path — the caller
        appends it to the relevant `StepResult.artifacts`.
        """
        rel = self._jailed_relpath(relpath)
        target = self.dir(run.id) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.redactor.scrub(data))
        return target

    def read_artifact(self, run: Run, relpath: str) -> str:
        rel = self._jailed_relpath(relpath)
        target = self.dir(run.id) / rel
        return target.read_text(encoding="utf-8")

    def append_log(self, run: Run, step_id: str, text: str) -> None:
        """Appends scrubbed text to `<dir>/logs/<step_id>.log`."""
        logs_dir = self.dir(run.id) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        with open(logs_dir / f"{step_id}.log", "a", encoding="utf-8", newline="\n") as f:
            f.write(self.redactor.scrub(text))
