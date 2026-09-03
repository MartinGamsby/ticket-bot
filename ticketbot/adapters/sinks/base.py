"""`Sink` -- where a run's results go (`file`, `jira`, `github_pr`, ...) -- plus
`MultiSink` (primary + the profile's `also:` list) and `DryRunSink` (records
intended calls instead of performing them, for `ticketbot run --dry-run`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from ...core.workitem import Attachment, WorkItem

logger = logging.getLogger(__name__)


class SinkError(RuntimeError):
    """A sink failed to perform an operation: bad config, a network failure, an
    unknown transition target, ...
    """


@runtime_checkable
class Sink(Protocol):
    def describe(self) -> str: ...

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None: ...

    def transition(self, item: WorkItem, state: str) -> None: ...

    def unassign(self, item: WorkItem) -> None: ...

    def link(self, item: WorkItem, url: str, title: str) -> None: ...

    def close(self) -> None: ...


class MultiSink:
    """Primary sink plus the `also:` list.

    Every method calls the primary FIRST -- its exception propagates and aborts
    the call, so a broken primary is never silently swallowed. Each secondary is
    then called in order; a secondary's exception is caught, logged, and (when
    `on_error` is given) reported through it, but never stops the remaining
    secondaries and never propagates out of `MultiSink` -- a broken GitHub token
    must not lose the Jira comment.
    """

    def __init__(
        self,
        primary: Sink,
        others: Sequence[Sink] = (),
        on_error: Callable[[Sink, str, Exception], None] | None = None,
    ) -> None:
        self.primary = primary
        self.others: list[Sink] = list(others)
        self.on_error = on_error

    def describe(self) -> str:
        if not self.others:
            return self.primary.describe()
        return f"{self.primary.describe()} (+{', '.join(o.describe() for o in self.others)})"

    def _fan_out(self, method: str, *args: object, **kwargs: object) -> None:
        getattr(self.primary, method)(*args, **kwargs)  # propagates on failure
        for sink in self.others:
            try:
                getattr(sink, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - a secondary failure must never lose the primary's result
                logger.warning(
                    "MultiSink: secondary sink %s failed on %s(): %s", sink.describe(), method, exc
                )
                if self.on_error is not None:
                    self.on_error(sink, method, exc)

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None:
        self._fan_out("comment", item, markdown, attachments)

    def transition(self, item: WorkItem, state: str) -> None:
        self._fan_out("transition", item, state)

    def unassign(self, item: WorkItem) -> None:
        self._fan_out("unassign", item)

    def link(self, item: WorkItem, url: str, title: str) -> None:
        self._fan_out("link", item, url, title)

    def set_pr_url(self, url: str) -> None:
        """Hand the freshly-opened pull request's URL to every wrapped sink that
        wants one -- only `GithubPrSink` does, because it posts ONTO that PR and
        drops every comment until it knows which one.

        Deliberately not part of the `Sink` protocol and not routed through
        `_fan_out`: a sink without the method is skipped rather than raising
        `AttributeError`, and a failure here is logged rather than propagated --
        it is a hand-off before the report, not the report itself.
        """
        for sink in (self.primary, *self.others):
            setter = getattr(sink, "set_pr_url", None)
            if setter is None:
                continue
            try:
                setter(url)
            except Exception as exc:  # noqa: BLE001 - never lose the report over a hand-off
                logger.warning("MultiSink: %s.set_pr_url() failed: %s", sink.describe(), exc)
                if self.on_error is not None:
                    self.on_error(sink, "set_pr_url", exc)

    def close(self) -> None:
        """Close every sink, best-effort -- one sink's close() failing must not
        stop the others from releasing their resources."""
        for sink in (self.primary, *self.others):
            try:
                sink.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MultiSink: %s.close() failed: %s", sink.describe(), exc)


class DryRunSink:
    """Wraps a sink; records intended calls instead of performing them.

    Appends one line per call to `self.calls` and (when `log_path` is set) to
    that file. The wrapped sink's methods are never called -- only its
    `describe()` (read-only) is used to label the log line -- so it stays
    completely untouched.
    """

    def __init__(self, inner: Sink, log_path: Path | None = None) -> None:
        self.inner = inner
        self.log_path = log_path
        self.calls: list[str] = []

    def describe(self) -> str:
        return f"dry-run({self.inner.describe()})"

    def _record(self, line: str) -> None:
        self.calls.append(line)
        if self.log_path is not None:
            path = Path(self.log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None:
        self._record(
            f"sink.comment {self.inner.describe()} {item.key} "
            f"({len(markdown)} chars, {len(attachments)} attachments)"
        )

    def transition(self, item: WorkItem, state: str) -> None:
        self._record(f"sink.transition {self.inner.describe()} {item.key} -> {state}")

    def unassign(self, item: WorkItem) -> None:
        self._record(f"sink.unassign {self.inner.describe()} {item.key}")

    def link(self, item: WorkItem, url: str, title: str) -> None:
        self._record(f"sink.link {self.inner.describe()} {item.key} -> {title} ({url})")

    def close(self) -> None:
        pass  # nothing was ever opened for real; the inner sink stays untouched
