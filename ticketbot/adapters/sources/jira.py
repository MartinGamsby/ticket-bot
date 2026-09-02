"""`JiraConnection` (the one place that builds the Jira Cloud REST v3 `httpx`
client, shared by `JiraSource` here and `JiraSink` in `adapters.sinks.jira`) plus
`JiraSource` itself.

Credentials (`base_url`, `email`, `token`) are `${ENV}` references expanded at
construction and registered with the redactor immediately, so they can never leak
into a log line, an artifact, or an error message -- including the `Authorization`
header, which is never read back or logged anywhere in this module. Every non-2xx
response raises with the status, Jira's `errorMessages` when present, and a
redacted body snippet.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...config.loader import expand_env
from ...config.redact import redact, register_secret
from ...config.schema import AdapterConfig
from ...core.workitem import Attachment, Comment, WorkItem
from ..sinks.adf import adf_to_text
from .base import SourceError, WorkItemNotFound

logger = logging.getLogger(__name__)

JIRA_API_PREFIX = "/rest/api/3"

DEFAULT_MAX_ATTACHMENT_BYTES = 25_000_000  # 25 MB safety cap for download_attachment()

_ACCEPTANCE_HEADING = re.compile(r"acceptance criteria", re.IGNORECASE)


class JiraConnection:
    """Owns the one `httpx.Client` used for Jira Cloud REST v3 calls. Accepts an
    injected `client` kwarg so tests can pass
    `httpx.Client(transport=httpx.MockTransport(handler))`; every request URL is
    still built as an absolute string from `base_url` (never relying on the
    client's own `base_url` join), matching `models/openai_compat.py`.
    """

    def __init__(self, cfg: AdapterConfig, *, client: httpx.Client | None = None) -> None:
        base_url = expand_env(str(cfg.opt("base_url", "")))
        email = expand_env(str(cfg.opt("email", "")))
        token = expand_env(str(cfg.opt("token", "")))
        # `email` and `token` are the credential pair and are registered. The
        # `base_url` deliberately is NOT: it is the tenant's public host, it is a
        # substring of every ticket URL (`{base_url}/browse/KEY`), and registering
        # it as a literal secret rewrites that URL to `***REDACTED***` in every
        # artifact -- and, now that outbound comments are scrubbed too, in the
        # comment posted back to the ticket. A host name is not a credential.
        register_secret(email)
        register_secret(token)

        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(cfg.opt("timeout_s", 30))
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                auth=httpx.BasicAuth(email, token),
                timeout=self.timeout_s,
                headers={"Accept": "application/json"},
            )
        )

    def host(self) -> str:
        """`acme.atlassian.net` from `https://acme.atlassian.net`, for describe()."""
        return urlsplit(self.base_url).netloc or self.base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request(self, method: str, path: str, *, error_cls: type[Exception], **kwargs: Any) -> httpx.Response:
        """`path` is relative to `base_url` (e.g. `/rest/api/3/issue/ENG-1`).
        Raises `error_cls` (never leaking the token/email) for a transport
        failure or a non-2xx response; the raised exception carries a
        `status_code` attribute so callers can special-case e.g. 404.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise error_cls(f"jira: {method} {path} failed: {redact(str(exc))}") from exc
        if response.status_code >= 300:
            err = error_cls(_format_error(method, path, response))
            err.status_code = response.status_code  # type: ignore[attr-defined]
            raise err
        return response

    def get_absolute(self, url: str, *, error_cls: type[Exception]) -> httpx.Response:
        """GET an already-absolute URL (e.g. an attachment's `content` link)
        through the same authenticated client.
        """
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise error_cls(f"jira: GET {redact(url)} failed: {redact(str(exc))}") from exc
        if response.status_code >= 300:
            err = error_cls(_format_error("GET", url, response))
            err.status_code = response.status_code  # type: ignore[attr-defined]
            raise err
        return response


def _format_error(method: str, path: str, response: httpx.Response) -> str:
    status = response.status_code
    messages: list[str] | None = None
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        raw_messages = data.get("errorMessages")
        if isinstance(raw_messages, list) and raw_messages:
            messages = [str(m) for m in raw_messages]
    snippet = redact(response.text[:500])
    if messages:
        return f"jira: {method} {path} -> HTTP {status}: {'; '.join(messages)} ({snippet})"
    return f"jira: {method} {path} -> HTTP {status}: {snippet}"


def find_transition(transitions: list[dict], target_status: str) -> dict | None:
    """The transition whose `to.name` matches `target_status`, case-insensitively."""
    target = target_status.strip().lower()
    for t in transitions:
        to_name = str((t.get("to") or {}).get("name", ""))
        if to_name.strip().lower() == target:
            return t
    return None


def _parse_jira_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    # Jira sometimes emits a zone offset with no colon ("+0000"); retry with one
    # inserted rather than giving up.
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", text)
    if m:
        try:
            return datetime.fromisoformat(f"{m.group(1)}:{m.group(2)}")
        except ValueError:
            return None
    return None


def _extract_acceptance_from_adf(doc: Any) -> str:
    """The text under an h2/h3 heading matching 'acceptance criteria'
    (case-insensitive) in the description ADF, else ''. Operates on the raw ADF
    tree (not the flattened text) because headings aren't distinguishable once
    flattened.
    """
    if not isinstance(doc, dict):
        return ""
    content = doc.get("content")
    if not isinstance(content, list):
        return ""

    collecting = False
    collected: list[Any] = []
    for node in content:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "heading":
            if collecting:
                break  # the next heading ends the acceptance-criteria section
            level = (node.get("attrs") or {}).get("level")
            heading_text = adf_to_text({"type": "doc", "version": 1, "content": [node]})
            if level in (2, 3) and _ACCEPTANCE_HEADING.search(heading_text or ""):
                collecting = True
            continue
        if collecting:
            collected.append(node)

    if not collected:
        return ""
    return adf_to_text({"type": "doc", "version": 1, "content": collected})


def map_issue_to_workitem(issue: dict, *, base_url: str, points_field: str, source_ref: str = "") -> WorkItem:
    fields = issue.get("fields") or {}
    key = issue.get("key") or ""
    raw_description = fields.get("description")
    description_text = adf_to_text(raw_description)

    assignee = fields.get("assignee") or {}
    issuetype = fields.get("issuetype") or {}
    status = fields.get("status") or {}

    story_points: float | None = None
    if points_field:
        raw_points = fields.get(points_field)
        if isinstance(raw_points, (int, float)) and not isinstance(raw_points, bool):
            story_points = float(raw_points)

    comments = []
    for c in (fields.get("comment") or {}).get("comments") or []:
        comments.append(
            Comment(
                author=(c.get("author") or {}).get("displayName", ""),
                body=adf_to_text(c.get("body")),
                created_at=_parse_jira_datetime(c.get("created")),
                id=c.get("id"),
            )
        )

    attachments = [
        Attachment(filename=a.get("filename", ""), content_type=a.get("mimeType", "application/octet-stream"))
        for a in fields.get("attachment") or []
    ]

    return WorkItem(
        id=key or "",
        title=fields.get("summary") or "",
        description=description_text,
        external_id=key or None,
        issue_type=issuetype.get("name") or "Task",
        story_points=story_points,
        labels=list(fields.get("labels") or []),
        acceptance=_extract_acceptance_from_adf(raw_description),
        status=status.get("name"),
        assignee=assignee.get("displayName"),
        url=f"{base_url}/browse/{key}" if key else None,
        comments=comments,
        attachments=attachments,
        source_ref=source_ref,
        raw=issue,
    )


class JiraSource:
    def __init__(
        self,
        cfg: AdapterConfig,
        *,
        client: httpx.Client | None = None,
        connection: JiraConnection | None = None,
    ) -> None:
        self._conn = connection if connection is not None else JiraConnection(cfg, client=client)
        self.jql = str(cfg.opt("jql", ""))
        self.poll_seconds = int(cfg.opt("poll_seconds", 60))
        self.points_field = str(cfg.opt("points_field") or "")
        self.max_results = int(cfg.opt("max_results", 50))
        account_id = cfg.opt("account_id")
        self.account_id = str(account_id) if account_id else None
        self.in_progress_status = str(cfg.opt("in_progress_status", "In Progress"))

    def describe(self) -> str:
        return f"Jira ({self._conn.host()})"

    def _fields_list(self) -> list[str]:
        fields = ["summary", "description", "issuetype", "status", "labels", "assignee", "comment", "attachment"]
        if self.points_field:
            fields.append(self.points_field)
        return fields

    def _get_issue(self, key: str) -> dict:
        try:
            response = self._conn.request(
                "GET",
                f"{JIRA_API_PREFIX}/issue/{key}",
                error_cls=SourceError,
                params={"fields": ",".join(self._fields_list())},
            )
        except SourceError as exc:
            if getattr(exc, "status_code", None) == 404:
                raise WorkItemNotFound(f"jira source: issue {key!r} not found") from exc
            raise
        return response.json()

    def fetch(self, external_id: str | None = None) -> WorkItem:
        if not external_id:
            raise WorkItemNotFound("jira source: fetch() requires an issue key")
        issue = self._get_issue(external_id)
        return map_issue_to_workitem(
            issue,
            base_url=self._conn.base_url,
            points_field=self.points_field,
            source_ref=f"{self._conn.base_url}/browse/{external_id}",
        )

    def poll(self) -> Iterator[WorkItem]:
        next_token: str | None = None
        fields = self._fields_list()
        while True:
            body: dict[str, Any] = {"jql": self.jql, "maxResults": self.max_results, "fields": fields}
            if next_token:
                body["nextPageToken"] = next_token
            response = self._conn.request(
                "POST", f"{JIRA_API_PREFIX}/search/jql", error_cls=SourceError, json=body
            )
            data = response.json()
            for issue in data.get("issues", []):
                yield map_issue_to_workitem(
                    issue, base_url=self._conn.base_url, points_field=self.points_field, source_ref=self.jql
                )
            next_token = data.get("nextPageToken")
            if not next_token:
                break

    def claim(self, item: WorkItem) -> bool:
        """1. PUT assignee to `account_id` (skipped, with an advisory warning,
        when unset). 2. Transition to `in_progress_status` (a missing transition
        is a warning, not an error). 3. Returns `False` *without* doing either
        when a fresh re-fetch shows the issue already assigned to someone other
        than `account_id`.
        """
        key = item.key
        fresh = self._get_issue(key)
        current_assignee = ((fresh.get("fields") or {}).get("assignee") or {}).get("accountId")
        if self.account_id and current_assignee and current_assignee != self.account_id:
            return False

        if self.account_id:
            self._conn.request(
                "PUT",
                f"{JIRA_API_PREFIX}/issue/{key}/assignee",
                error_cls=SourceError,
                json={"accountId": self.account_id},
            )
        else:
            logger.warning("jira source: claim() has no account_id configured; assignment is advisory only")

        response = self._conn.request(
            "GET", f"{JIRA_API_PREFIX}/issue/{key}/transitions", error_cls=SourceError
        )
        transitions = response.json().get("transitions", [])
        match = find_transition(transitions, self.in_progress_status)
        if match is None:
            logger.warning(
                "jira source: no transition to %r available for %s; claimed without transitioning",
                self.in_progress_status,
                key,
            )
            return True

        self._conn.request(
            "POST",
            f"{JIRA_API_PREFIX}/issue/{key}/transitions",
            error_cls=SourceError,
            json={"transition": {"id": match["id"]}},
        )
        return True

    def download_attachment(
        self,
        item: WorkItem,
        filename: str,
        dest_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> Attachment:
        """Download one of `item`'s attachments (looked up by filename in the raw
        issue payload's `fields.attachment[]`, since the mapped `Attachment`
        objects deliberately don't carry a content URL) into `dest_dir`.

        Security rails: the local filename is reduced to its basename so a
        Jira-supplied `filename` cannot escape `dest_dir`; the resolved
        destination is re-checked against `dest_dir`; and the download is
        streamed with a hard `max_bytes` cap, so a hostile/huge attachment
        cannot fill the disk or write outside the run directory.
        """
        fields = (item.raw or {}).get("fields") or {}
        entries = fields.get("attachment") or []
        match = next((a for a in entries if isinstance(a, dict) and a.get("filename") == filename), None)
        if match is None:
            raise SourceError(f"jira source: no attachment named {filename!r} on {item.key}")
        content_url = match.get("content")
        if not content_url:
            raise SourceError(f"jira source: attachment {filename!r} on {item.key} has no content URL")

        dest_root = Path(dest_dir).resolve(strict=False)
        dest_root.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename.replace("\\", "/")).name or "attachment"
        dest_path = (dest_root / safe_name).resolve(strict=False)
        if dest_path != dest_root and not dest_path.is_relative_to(dest_root):
            raise SourceError(f"jira source: attachment filename escapes destination directory: {filename!r}")

        response = self._conn.get_absolute(content_url, error_cls=SourceError)
        total = 0
        try:
            with open(dest_path, "wb") as f:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceError(
                            f"jira source: attachment {filename!r} exceeds max size of {max_bytes} bytes"
                        )
                    f.write(chunk)
        except BaseException:
            dest_path.unlink(missing_ok=True)  # never leave a truncated/oversized partial file behind
            raise

        return Attachment(
            filename=safe_name, content_type=match.get("mimeType", "application/octet-stream"), path=dest_path
        )

    def close(self) -> None:
        self._conn.close()
