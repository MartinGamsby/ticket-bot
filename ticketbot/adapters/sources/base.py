"""`Source` -- where a `WorkItem` comes from (`file`, `jira`, ...) -- plus the
shared error types every concrete source raises.

`claim()` means "assign this to the bot and move it to In Progress"; it returns
`False` when someone else already claimed the item, which is how two pollers
avoid duplicating work. `FileSource.claim()` always returns `True` -- there is no
contention on a local file.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from ...core.workitem import WorkItem


class SourceError(RuntimeError):
    """A source failed to produce a work item: bad config, a network failure, or
    unparsable input. Never crashes the poller -- callers catch this per item.
    """


class WorkItemNotFound(SourceError):
    """No work item could be located for the given reference (a missing file, an
    unknown Jira key, ...).
    """


@runtime_checkable
class Source(Protocol):
    def describe(self) -> str: ...  # 'Jira (acme.atlassian.net)' | 'file'

    def fetch(self, external_id: str | None = None) -> WorkItem: ...

    def poll(self) -> Iterator[WorkItem]: ...

    def claim(self, item: WorkItem) -> bool: ...

    def close(self) -> None: ...  # default no-op for sources with nothing to release
