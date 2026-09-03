"""Brace-safe `{placeholder}` templating for prompts and comment templates.

`str.format` is forbidden here: prompts and comment templates carry literal braces
(JSON bodies, code fences) that `str.format` would try to interpret as fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\}")

_MISSING = object()  # private sentinel: "no value at this dotted path"


def _lookup_raw(values: Mapping[str, Any], dotted: str) -> Any:
    """Walk `values` by dotted path. Returns the private `_MISSING` sentinel (never
    exposed publicly) the moment any hop is absent, so callers can tell "found None"
    apart from "not found at all".
    """
    current: Any = values
    for part in dotted.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)
    return current


def lookup(values: Mapping[str, Any], dotted: str) -> Any | None:
    """Walk dicts and objects (getattr) by dotted path; None when any hop is missing."""
    result = _lookup_raw(values, dotted)
    return None if result is _MISSING else result


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def render(template: str, values: Mapping[str, Any]) -> str:
    """Replace `{name}` and `{a.b.c}` with the dotted lookup from `values`.

    - `{{` -> literal `{`, `}}` -> literal `}`.
    - An UNKNOWN placeholder is left EXACTLY as written (never raises, never blanks).
    - None renders as ''. Lists render as ', '.join(str(x)). Paths render as str(path).
    - Values are inserted verbatim; no recursive expansion of the substituted text
      (re.sub never rescans its own output).
    """

    def _sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        dotted = match.group(1)
        resolved = _lookup_raw(values, dotted)
        if resolved is _MISSING:
            return token
        return _format_value(resolved)

    return PLACEHOLDER.sub(_sub, template)


def missing_placeholders(template: str, values: Mapping[str, Any]) -> list[str]:
    """Names present in the template but absent from values — for a lint/test."""
    names: list[str] = []
    seen: set[str] = set()
    for match in PLACEHOLDER.finditer(template):
        dotted = match.group(1)
        if dotted is None or dotted in seen:
            continue
        if _lookup_raw(values, dotted) is _MISSING:
            names.append(dotted)
            seen.add(dotted)
    return names
