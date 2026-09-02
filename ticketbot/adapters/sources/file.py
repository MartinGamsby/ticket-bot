"""`FileSource` -- the default source: a single file, inline text (`--input-text`),
or a poll glob of front-matter markdown files. This is what keeps the whole
pipeline runnable fully offline.

Front matter is UNTRUSTED input (it may originate from anywhere a file lands in
the inbox): parsed with `yaml.safe_load` only, a non-mapping or parse error raises
`SourceError` naming the file rather than crashing the poller, and unknown keys
land in `WorkItem.raw` -- never on an attribute, and never on a filesystem path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from ...config.schema import AdapterConfig
from ...core.workitem import WorkItem, slugify
from .base import SourceError, WorkItemNotFound

logger = logging.getLogger(__name__)

# The first LEVEL-1 (`# `) heading only, per the title fallback chain -- `## `/`###`
# etc. do not match (the char right after `#` must be whitespace).
_HEADING_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)

_KNOWN_FRONT_MATTER_KEYS = {"key", "title", "type", "points", "labels", "acceptance", "url", "status", "assignee"}


def _clean_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _split_front_matter(raw: str, source_ref: str) -> tuple[dict[str, Any], str]:
    """A leading `---` line, YAML up to the closing `---`, then the body. No
    front matter at all (the file doesn't start with `---`, or the delimiter is
    never closed) is normal -- the whole file is treated as the body.
    """
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, raw

    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        return {}, raw

    fm_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")

    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise SourceError(f"file source: malformed YAML front matter in {source_ref}: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SourceError(
            f"file source: front matter in {source_ref} must be a YAML mapping, got {type(data).__name__}"
        )
    return data, body


def _title_fallback(body: str, source_ref: str) -> str:
    heading_m = _HEADING_RE.search(body)
    if heading_m:
        return heading_m.group(1).strip()
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    if source_ref and source_ref != "--input-text":
        stem = Path(source_ref).stem
        if stem:
            return stem
    return "untitled task"


class FileSource:
    def __init__(self, cfg: AdapterConfig, *, base_dir: Path | None = None) -> None:
        """Relative paths (`path`, `glob`, `processed_dir`) resolve against
        `base_dir` (the profile's directory) when given, else cwd.
        """
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else Path.cwd()
        self.path_opt = cfg.opt("path")
        self.text_opt = cfg.opt("text")
        self.glob_opt = str(cfg.opt("glob", "inbox/*.md"))
        self.processed_dir_opt = str(cfg.opt("processed_dir", "inbox/processed"))
        self.encoding = str(cfg.opt("encoding", "utf-8"))

    def _resolve(self, rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else (self.base_dir / p)

    def describe(self) -> str:
        if self.path_opt:
            return f"file ({Path(str(self.path_opt)).name})"
        return "file"

    def fetch(self, external_id: str | None = None) -> WorkItem:
        """Precedence: `external_id` treated as a path if it exists -> `cfg.path`
        -> `cfg.text`. Nothing usable -> `WorkItemNotFound`.
        """
        if external_id:
            candidate = self._resolve(external_id)
            if candidate.is_file():
                return self._from_file(candidate)
        if self.path_opt:
            path = self._resolve(str(self.path_opt))
            if not path.is_file():
                raise WorkItemNotFound(f"file source: configured path does not exist: {path}")
            return self._from_file(path)
        if self.text_opt:
            return self._from_text(str(self.text_opt))
        raise WorkItemNotFound(
            "file source: fetch() found nothing usable "
            "(no existing external_id path, no configured path, no inline text)"
        )

    def poll(self) -> Iterator[WorkItem]:
        """Globs `glob` (relative to `base_dir`), sorted by mtime, skipping
        anything already under `processed_dir`.
        """
        processed_dir = self._resolve(self.processed_dir_opt).resolve()
        try:
            matches = sorted(self.base_dir.glob(self.glob_opt), key=lambda p: p.stat().st_mtime)
        except OSError as exc:
            raise SourceError(f"file source: cannot glob {self.glob_opt!r} under {self.base_dir}: {exc}") from exc

        for path in matches:
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(processed_dir):
                continue
            yield self._from_file(path)

    def claim(self, item: WorkItem) -> bool:
        return True

    def close(self) -> None:
        pass

    def mark_processed(self, item: WorkItem) -> None:
        """Move the file `item` came from into `processed_dir`. A no-op when the
        item did not come from a real file (`--input-text`) or the file is
        already gone -- the orchestrator decides WHEN to call this, not this class.
        """
        ref = item.source_ref
        if not ref or ref == "--input-text":
            return
        src = Path(ref)
        if not src.is_file():
            return
        dest_dir = self._resolve(self.processed_dir_opt)
        dest_dir.mkdir(parents=True, exist_ok=True)
        src.replace(dest_dir / src.name)

    # -- parsing ---------------------------------------------------------- #

    def _from_text(self, text: str) -> WorkItem:
        return self._build(text, source_ref="--input-text")

    def _from_file(self, path: Path) -> WorkItem:
        try:
            raw = path.read_text(encoding=self.encoding)
        except OSError as exc:
            raise SourceError(f"file source: cannot read {path}: {exc}") from exc
        return self._build(raw, source_ref=str(path.resolve()))

    def _build(self, raw: str, *, source_ref: str) -> WorkItem:
        front_matter, body = _split_front_matter(raw, source_ref)

        external_id = _clean_str(front_matter.get("key"))
        issue_type = _clean_str(front_matter.get("type")) or "Task"

        story_points: float | None = None
        if "points" in front_matter:
            raw_points = front_matter["points"]
            try:
                story_points = float(raw_points)
            except (TypeError, ValueError):
                logger.warning(
                    "file source: %s: front-matter 'points' %r is not numeric; ignoring",
                    source_ref,
                    raw_points,
                )

        labels_raw = front_matter.get("labels")
        if isinstance(labels_raw, str):
            labels = [labels_raw]
        elif isinstance(labels_raw, list):
            labels = [str(x) for x in labels_raw]
        else:
            labels = []

        acceptance = _clean_str(front_matter.get("acceptance")) or ""
        url = _clean_str(front_matter.get("url"))
        status = _clean_str(front_matter.get("status"))
        assignee = _clean_str(front_matter.get("assignee"))

        title = _clean_str(front_matter.get("title")) or _title_fallback(body, source_ref)
        item_id = external_id or slugify(title)
        raw_extra = {k: v for k, v in front_matter.items() if k not in _KNOWN_FRONT_MATTER_KEYS}

        return WorkItem(
            id=item_id,
            title=title,
            description=body.strip(),
            external_id=external_id,
            issue_type=issue_type,
            story_points=story_points,
            labels=labels,
            acceptance=acceptance,
            status=status,
            assignee=assignee,
            url=url,
            source_ref=source_ref,
            raw=raw_extra,
        )
