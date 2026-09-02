"""Secret scrubbing for every artifact and log line: pattern-based masking of known
token shapes, plus explicitly registered literal secret values (e.g. resolved
`${ENV}` values) that don't match any pattern.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("solari", re.compile(r"slr_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("atlassian", re.compile(r"ATATT[A-Za-z0-9_\-=]{16,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("bearer", re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+")),
]

_MIN_SECRET_LEN = 8


class Redactor:
    """Pattern-based scrubbing plus explicitly registered literal secret values."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, secret: str | None) -> None:
        """Remember a live secret value (>= 8 chars) so it is masked even if it
        matches no pattern. No-op for None/short/whitespace values."""
        if secret is None:
            return
        if len(secret) < _MIN_SECRET_LEN or not secret.strip():
            return
        self._secrets.add(secret)

    def scrub(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        result = text
        for _name, pattern in PATTERNS:
            if pattern.groups:
                result = pattern.sub(lambda m: m.group(1) + REDACTED, result)
            else:
                result = pattern.sub(REDACTED, result)
        for secret in self._secrets:
            result = result.replace(secret, REDACTED)
        return result

    def scrub_obj(self, obj: Any) -> Any:
        """Recursively scrub str values inside dict/list/tuple structures."""
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub_obj(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.scrub_obj(v) for v in obj)
        if isinstance(obj, str):
            return self.scrub(obj)
        return obj


_default = Redactor()


def default_redactor() -> Redactor:
    """The process-wide `Redactor` that `register_secret()` populates and `redact()`
    scrubs through.

    Anything that scrubs on its own (rather than calling `redact()`) must take THIS
    instance, not a fresh `Redactor()` -- a private one has no registered secrets,
    so it falls back to pattern matching alone and misses every literal credential
    an adapter expanded from a `${ENV}` ref. `core.run.RunStore` is the caller that
    needs it, since it scrubs every artifact and log line it writes.

    Returned by call, never imported as a module global, so a test that swaps
    `_default` out (see `tests/test_config_redact.py`) still isolates correctly.
    """
    return _default


def redact(text: str) -> str:
    return _default.scrub(text)


def register_secret(value: str | None) -> None:
    _default.register(value)
