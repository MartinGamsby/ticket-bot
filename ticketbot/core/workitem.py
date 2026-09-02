"""`WorkItem` and its nested value objects — the provider-neutral shape every
source adapter (`file`, `jira`, ...) produces and every later section consumes.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase, non-alphanumerics collapsed to '-', trimmed, truncated on a word
    boundary. Always non-empty (fallback: 'task'). ASCII-only and free of '..', path
    separators and Windows-reserved characters, so it is safe in filenames and git
    branch names. Shared by `WorkItem.slug()` and `RunStore.new_id()`.
    """
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    collapsed = _SLUG_JUNK.sub("-", ascii_text.lower()).strip("-")
    if not collapsed:
        return "task"
    if len(collapsed) > max_len:
        cut = collapsed[:max_len]
        if "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        collapsed = cut.strip("-")
    return collapsed or "task"


class Size(str, Enum):
    XS = "xs"
    S = "s"
    M = "m"
    L = "l"
    XL = "xl"


class Ambiguity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Comment:
    author: str
    body: str
    created_at: datetime | None = None
    id: str | None = None


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str = "application/octet-stream"
    path: Path | None = None  # local file, preferred
    data: bytes | None = None  # in-memory alternative

    def read_bytes(self) -> bytes:
        """Return the attachment's bytes: `data` if set, else read from `path`."""
        if self.data is not None:
            return self.data
        if self.path is not None:
            return Path(self.path).read_bytes()
        raise ValueError(f"attachment {self.filename!r} has neither data nor path")


def _comment_to_dict(c: Comment) -> dict[str, Any]:
    return {
        "author": c.author,
        "body": c.body,
        "created_at": c.created_at.isoformat() if c.created_at is not None else None,
        "id": c.id,
    }


def _comment_from_dict(d: dict[str, Any]) -> Comment:
    created_at = d.get("created_at")
    return Comment(
        author=d["author"],
        body=d["body"],
        created_at=datetime.fromisoformat(created_at) if created_at else None,
        id=d.get("id"),
    )


def _attachment_to_dict(a: Attachment) -> dict[str, Any]:
    return {
        "filename": a.filename,
        "content_type": a.content_type,
        "path": str(a.path) if a.path is not None else None,
        "data_b64": base64.b64encode(a.data).decode("ascii") if a.data is not None else None,
    }


def _attachment_from_dict(d: dict[str, Any]) -> Attachment:
    data_b64 = d.get("data_b64")
    path = d.get("path")
    return Attachment(
        filename=d["filename"],
        content_type=d.get("content_type", "application/octet-stream"),
        path=Path(path) if path is not None else None,
        data=base64.b64decode(data_b64) if data_b64 is not None else None,
    )


@dataclass
class WorkItem:
    id: str  # stable internal id (slug of external_id or of title)
    title: str
    description: str = ""
    external_id: str | None = None  # "ENG-1842" for Jira, None for ad-hoc text
    issue_type: str = "Task"
    story_points: float | None = None
    labels: list[str] = field(default_factory=list)
    acceptance: str = ""
    status: str | None = None
    assignee: str | None = None
    url: str | None = None
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    ambiguity: Ambiguity | None = None  # set by the `ingest` step, read by `when:` rules
    source_ref: str = ""  # e.g. the input file path or the JQL that found it
    raw: dict[str, Any] = field(default_factory=dict)  # provider payload, for debugging

    @property
    def key(self) -> str:
        """external_id if present else id — used in run ids and branch names."""
        return self.external_id if self.external_id else self.id

    def slug(self, max_len: int = 40) -> str:
        """Lowercase, non-alphanumerics collapsed to '-', trimmed, truncated on a
        word boundary. Derived from `title`. Always non-empty (fallback: 'task').

        ASCII-only and free of '..', path separators and Windows-reserved
        characters, so it is safe to use in both a git branch name and a filename.
        """
        return slugify(self.title, max_len=max_len)

    def size(self) -> Size:
        """story_points -> Size. None or <=1 -> XS, <=2 -> S, <=5 -> M, <=8 -> L, else XL."""
        points = self.story_points
        if points is None or points <= 1:
            return Size.XS
        if points <= 2:
            return Size.S
        if points <= 5:
            return Size.M
        if points <= 8:
            return Size.L
        return Size.XL

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation; datetimes -> isoformat, Path -> str, bytes -> base64."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "external_id": self.external_id,
            "issue_type": self.issue_type,
            "story_points": self.story_points,
            "labels": list(self.labels),
            "acceptance": self.acceptance,
            "status": self.status,
            "assignee": self.assignee,
            "url": self.url,
            "comments": [_comment_to_dict(c) for c in self.comments],
            "attachments": [_attachment_to_dict(a) for a in self.attachments],
            "ambiguity": self.ambiguity.value if self.ambiguity is not None else None,
            "source_ref": self.source_ref,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkItem":
        ambiguity = d.get("ambiguity")
        return cls(
            id=d["id"],
            title=d["title"],
            description=d.get("description", ""),
            external_id=d.get("external_id"),
            issue_type=d.get("issue_type", "Task"),
            story_points=d.get("story_points"),
            labels=list(d.get("labels", [])),
            acceptance=d.get("acceptance", ""),
            status=d.get("status"),
            assignee=d.get("assignee"),
            url=d.get("url"),
            comments=[_comment_from_dict(c) for c in d.get("comments", [])],
            attachments=[_attachment_from_dict(a) for a in d.get("attachments", [])],
            ambiguity=Ambiguity(ambiguity) if ambiguity is not None else None,
            source_ref=d.get("source_ref", ""),
            raw=dict(d.get("raw", {})),
        )

    def as_context(self) -> dict[str, Any]:
        """Flat mapping for predicates and templates. `ambiguity` and `size` are the
        plain string values, not enum members.
        """
        return {
            "key": self.key,
            "external_id": self.external_id,
            "title": self.title,
            "description": self.description,
            "issue_type": self.issue_type,
            "story_points": self.story_points,
            "labels": list(self.labels),
            "acceptance": self.acceptance,
            "status": self.status,
            "ambiguity": self.ambiguity.value if self.ambiguity is not None else None,
            "size": self.size().value,
            "url": self.url,
            "comment_count": len(self.comments),
        }
