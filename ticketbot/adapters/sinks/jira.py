"""`JiraSink` -- posts comments (as ADF), transitions, unassigns, and remote-links
back to Jira Cloud REST v3. Shares its HTTP client construction and error handling
with `JiraSource` via `adapters.sources.jira.JiraConnection` -- see that module's
docstring for the security rails around credentials (the one place the client is
built, per the section-6 spec).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from ...config.schema import AdapterConfig
from ...core.workitem import Attachment, WorkItem
from ..sources.jira import JIRA_API_PREFIX, JiraConnection, find_transition
from .adf import markdown_to_adf
from .base import SinkError

logger = logging.getLogger(__name__)


class JiraSink:
    def __init__(
        self,
        cfg: AdapterConfig,
        *,
        client: httpx.Client | None = None,
        connection: JiraConnection | None = None,
    ) -> None:
        self._conn = connection if connection is not None else JiraConnection(cfg, client=client)

    def describe(self) -> str:
        return f"jira ({self._conn.host()})"

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None:
        """Uploads each attachment FIRST (so the comment can reference them), then
        posts the comment. An attachment upload failure is logged and appended to
        the comment as a line rather than aborting the comment.
        """
        key = item.key
        failure_notes: list[str] = []
        for att in attachments:
            try:
                self._upload_attachment(key, att)
            except Exception as exc:  # noqa: BLE001 - an upload failure must never lose the comment
                logger.warning("jira sink: attachment %r failed to upload for %s: %s", att.filename, key, exc)
                failure_notes.append(f"_(attachment {att.filename} failed to upload)_")

        body = markdown if not failure_notes else markdown + "\n\n" + "\n".join(failure_notes)
        self._conn.request(
            "POST",
            f"{JIRA_API_PREFIX}/issue/{key}/comment",
            error_cls=SinkError,
            json={"body": markdown_to_adf(body)},
        )

    def _upload_attachment(self, key: str, attachment: Attachment) -> None:
        self._conn.request(
            "POST",
            f"{JIRA_API_PREFIX}/issue/{key}/attachments",
            error_cls=SinkError,
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (attachment.filename, attachment.read_bytes(), attachment.content_type)},
        )

    def transition(self, item: WorkItem, state: str) -> None:
        key = item.key
        response = self._conn.request(
            "GET", f"{JIRA_API_PREFIX}/issue/{key}/transitions", error_cls=SinkError
        )
        transitions = response.json().get("transitions", [])
        match = find_transition(transitions, state)
        if match is None:
            available = ", ".join(sorted({str((t.get("to") or {}).get("name", "")) for t in transitions})) or "(none)"
            raise SinkError(f"jira sink: no transition to {state!r} on {key} (available: {available})")
        self._conn.request(
            "POST",
            f"{JIRA_API_PREFIX}/issue/{key}/transitions",
            error_cls=SinkError,
            json={"transition": {"id": match["id"]}},
        )

    def unassign(self, item: WorkItem) -> None:
        self._conn.request(
            "PUT",
            f"{JIRA_API_PREFIX}/issue/{item.key}/assignee",
            error_cls=SinkError,
            json={"accountId": None},
        )

    def link(self, item: WorkItem, url: str, title: str) -> None:
        self._conn.request(
            "POST",
            f"{JIRA_API_PREFIX}/issue/{item.key}/remotelink",
            error_cls=SinkError,
            json={"object": {"url": url, "title": title}},
        )

    def close(self) -> None:
        self._conn.close()
