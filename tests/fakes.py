"""Shared test fakes. Created in section 3 (`FakeModelProvider` helpers); later
sections append `FakeExecutor`, `FakeRuntime`, `FakeSource`, `FakeSink` here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from ticketbot.adapters.repos.base import CommitResult
from ticketbot.adapters.runtimes.base import ExecOut
from ticketbot.adapters.sinks.base import SinkError
from ticketbot.core.workitem import Attachment, WorkItem
from ticketbot.engine import protocol as _protocol
from ticketbot.executors.base import ExecRequest, ExecResult
from ticketbot.models.base import ProviderMessage, ToolCall, Usage
from ticketbot.models.fake import FakeModelProvider


def fake_provider(*texts: str) -> FakeModelProvider:
    """A `FakeModelProvider` whose script is exactly these texts, in order (each
    becomes an end_turn `ProviderMessage`)."""
    return FakeModelProvider(script=list(texts))


def tool_turn(name: str, input: dict, *, id: str = "toolu_1") -> ProviderMessage:
    return ProviderMessage(text="", tool_calls=[ToolCall(id, name, input)], stop_reason="tool_use")


def text_turn(text: str) -> ProviderMessage:
    return ProviderMessage(text=text, stop_reason="end_turn")


class FakeRuntime:
    """Records calls; returns canned results. Satisfies the `Runtime` protocol
    (`adapters.runtimes.base.Runtime`) without touching a real subprocess or
    cloud session.
    """

    def __init__(
        self,
        *,
        exec_out: ExecOut | None = None,
        png: bytes | None = b"\x89PNG\r\n\x1a\n",
        preview: str | None = None,
        file_data: bytes = b"",
    ) -> None:
        self.exec_out = exec_out if exec_out is not None else ExecOut(exit_code=0, stdout="ok")
        self.png = png
        self.preview = preview
        self.file_data = file_data
        self.calls: list[tuple[str, tuple, dict]] = []
        self.started = False
        self.stopped = False

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        self.calls.append((name, args, kwargs))

    def describe(self) -> str:
        self._record("describe")
        return "fake"

    def start(self) -> None:
        self._record("start")
        self.started = True

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecOut:
        self._record("exec", argv, cwd=cwd, timeout=timeout, env=env)
        return self.exec_out

    def read_file(self, path: str) -> bytes:
        self._record("read_file", path)
        return self.file_data

    def write_file(self, path: str, data: bytes) -> None:
        self._record("write_file", path, data)

    def screenshot(self) -> bytes | None:
        self._record("screenshot")
        return self.png

    def preview_url(self, port: int) -> str | None:
        self._record("preview_url", port)
        return self.preview

    def stop(self) -> None:
        self._record("stop")
        self.stopped = True


class FakeSource:
    """Satisfies the `Source` protocol (`adapters.sources.base.Source`) over a
    fixed in-memory list of items -- no filesystem, no network.
    """

    def __init__(self, items: list[WorkItem]) -> None:
        self.items = list(items)
        self.claimed: list[str] = []
        self.closed = False

    def describe(self) -> str:
        return "fake"

    def fetch(self, external_id: str | None = None) -> WorkItem:
        if external_id:
            for item in self.items:
                if item.key == external_id:
                    return item
            raise KeyError(f"fake source: no item with key {external_id!r}")
        if self.items:
            return self.items[0]
        raise KeyError("fake source: no items")

    def poll(self) -> Iterator[WorkItem]:
        yield from self.items

    def claim(self, item: WorkItem) -> bool:
        self.claimed.append(item.key)
        return True

    def close(self) -> None:
        self.closed = True


class FakeRepo:
    """Satisfies the `Repo` protocol (`adapters.repos.base.Repo`); records every
    call and returns canned results -- no real git subprocess, no network.
    """

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        commit_shas: list[str | None] | None = None,
        pr_url: str | None = "https://github.com/acme/app/pull/1",
        missing: list[str] | None = None,
    ) -> None:
        self._workspace = workspace or Path("/fake/workspace")
        self._commit_shas = list(commit_shas) if commit_shas is not None else ["deadbeef"]
        self.pr_url = pr_url
        self.missing = list(missing) if missing is not None else []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.pushed = False
        self.cleaned_up = False

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        self.calls.append((name, args, kwargs))

    def describe(self) -> str:
        self._record("describe")
        return "fake/repo @ fake-branch"

    def checkout(self, branch: str) -> Path:
        self._record("checkout", branch)
        return self._workspace

    def workspace(self) -> Path:
        self._record("workspace")
        return self._workspace

    def status(self) -> list[str]:
        self._record("status")
        return []

    def diff(self, base: str | None = None) -> str:
        self._record("diff", base)
        return ""

    def commit(self, message: str, body: str = "") -> CommitResult:
        self._record("commit", message, body)
        sha = self._commit_shas.pop(0) if self._commit_shas else None
        return CommitResult(sha=sha, message=message, files=1 if sha else 0)

    def push(self) -> None:
        self._record("push")
        self.pushed = True

    def open_pr(self, title: str, body: str) -> str | None:
        self._record("open_pr", title, body)
        return self.pr_url

    def cleanup(self) -> None:
        self._record("cleanup")
        self.cleaned_up = True

    def verify_landed(self, paths: Sequence[Path | str]) -> list[str]:
        self._record("verify_landed", paths)
        return list(self.missing)


class FakeSink:
    """Satisfies the `Sink` protocol (`adapters.sinks.base.Sink`); records every
    call and can be told to raise `SinkError` on specific methods via `fail_on`,
    for exercising `MultiSink`'s secondary-failure handling.
    """

    def __init__(self, fail_on: set[str] = frozenset()) -> None:
        self.comments: list[tuple[str, str, tuple]] = []
        self.transitions: list[tuple[str, str]] = []
        self.unassigned: list[str] = []
        self.links: list[tuple[str, str, str]] = []
        self.fail_on: set[str] = set(fail_on)
        self.closed = False

    def describe(self) -> str:
        return "fake"

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None:
        if "comment" in self.fail_on:
            raise SinkError("fake sink: comment failing on purpose")
        self.comments.append((item.key, markdown, tuple(attachments)))

    def transition(self, item: WorkItem, state: str) -> None:
        if "transition" in self.fail_on:
            raise SinkError("fake sink: transition failing on purpose")
        self.transitions.append((item.key, state))

    def unassign(self, item: WorkItem) -> None:
        if "unassign" in self.fail_on:
            raise SinkError("fake sink: unassign failing on purpose")
        self.unassigned.append(item.key)

    def link(self, item: WorkItem, url: str, title: str) -> None:
        if "link" in self.fail_on:
            raise SinkError("fake sink: link failing on purpose")
        self.links.append((item.key, url, title))

    def close(self) -> None:
        self.closed = True


WriteEntry = tuple[str, "str | bytes"]
WriteSpec = "list[WriteEntry] | Callable[[ExecRequest], list[WriteEntry]]"


class FakeExecutor:
    """Satisfies the `Executor` protocol (`executors.base.Executor`); returns
    scripted `ExecResult`s keyed by step id, and can WRITE files into the
    workspace and/or the run dir (`artifacts_dir`) so the repo/commit path and
    `for_each: plan.sections` fan-out are exercised without a real coding CLI or
    model call.

        FakeExecutor(
            {"plan": ExecResult(text="...")},
            writes={"implement": [("src/a.py", "x")]},              # -> workspace
            artifact_writes={"plan": [("plan.md", "..."),
                                       ("sections/section-1.md", "# one")]},  # -> run dir
        )

    A step id absent from `results` gets a generic ok result (`default_text`).
    `question`/`defers` are always (re)derived from the final text via
    `engine.protocol`, so a test only has to set `.text` (e.g. to a string
    starting with "QUESTION:" or containing a "DEFER:" line) -- it never needs
    to call the parser itself. A scripted result's `files_written` (when
    non-empty) wins over the paths this call actually wrote to the workspace,
    which is how a test forces the "landed outside the workspace" failure path.
    Every `ExecRequest` is recorded, in call order, in `.requests`.
    """

    def __init__(
        self,
        results: dict[str, ExecResult] | None = None,
        *,
        writes: dict[str, WriteSpec] | None = None,
        artifact_writes: dict[str, WriteSpec] | None = None,
        default_text: str = "ok",
    ) -> None:
        self.results = dict(results or {})
        self.writes = dict(writes or {})
        self.artifact_writes = dict(artifact_writes or {})
        self.default_text = default_text
        self.requests: list[ExecRequest] = []

    def describe(self) -> str:
        return "fake executor"

    def _write_entries(self, root: Path, spec: object, req: ExecRequest) -> list[Path]:
        if spec is None:
            return []
        entries = spec(req) if callable(spec) else spec
        written: list[Path] = []
        for relpath, content in entries:
            target = Path(root) / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
            written.append(target)
        return written

    def run(self, req: ExecRequest) -> ExecResult:
        self.requests.append(req)

        written = self._write_entries(req.workspace, self.writes.get(req.step_id), req)
        self._write_entries(req.artifacts_dir, self.artifact_writes.get(req.step_id), req)

        scripted = self.results.get(req.step_id)
        text = scripted.text if scripted is not None else self.default_text
        usage = scripted.usage if scripted is not None else Usage(input_tokens=5, output_tokens=5)
        files_written = (
            scripted.files_written if (scripted is not None and scripted.files_written) else written
        )
        exit_code = scripted.exit_code if scripted is not None else 0
        error = scripted.error if scripted is not None else None
        timed_out = scripted.timed_out if scripted is not None else False

        return ExecResult(
            text=text,
            usage=usage,
            files_written=files_written,
            question=_protocol.parse_question(text),
            defers=_protocol.parse_defers(text),
            exit_code=exit_code,
            error=error,
            timed_out=timed_out,
        )
