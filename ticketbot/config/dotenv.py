"""`.env` loading for local development.

Profiles reference credentials as `${ENV_NAME}`, and `expand_env()` resolves those
from `os.environ` alone -- so without this module a `.env` file would sit there
being ignored, which is worse than not having one. `load_dotenv()` reads the file
into the process environment before any command runs.

Two rules shape the design:

**The real environment always wins.** A name already set in `os.environ` is never
overwritten (`override=True` is opt-in and nothing in the CLI passes it). CI sets
secrets as real environment variables; a stale `.env` left in a working copy must
not silently shadow them.

**Loaded credentials are registered with the redactor.** A value whose NAME reads
like a credential is handed to `register_secret()` at load time, so it is masked in
`runs/<id>/` artifacts and logs even when it matches no pattern in `PATTERNS`.
Matching is on the name, not the value: `SOLARI_BASE_URL=https://api.getsolari.com`
must not become a redaction pattern applied to every line in the process.

The file itself is git-ignored (see `.gitignore`); `.env.example` is the tracked
template listing the names the shipped profiles use.
"""

from __future__ import annotations

import os
from pathlib import Path

from .redact import is_secret_name, register_secret

DEFAULT_ENV_FILENAME = ".env"

# Values are read as literal text: no `$VAR` expansion, no command substitution.
# A `.env` is a credential file, and interpolating it would let one entry pull in
# arbitrary process state -- `expand_env()` is the one place references resolve.
_QUOTES = ("'", '"')


class DotenvError(Exception):
    """A `.env` file exists but could not be parsed."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse `.env` text into a name -> value mapping.

    Accepts `KEY=value`, a leading `export `, surrounding single or double quotes,
    blank lines, and `#` comments (whole-line, or trailing on an unquoted value).
    A line with no `=` is an error rather than a silent skip -- a typo'd credential
    line that vanishes quietly is exactly the failure this file must not have.
    """
    values: dict[str, str] = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        name, sep, value = line.partition("=")
        if not sep:
            raise DotenvError(f"line {lineno}: expected NAME=value, got {raw.strip()!r}")

        name = name.strip()
        if not name:
            raise DotenvError(f"line {lineno}: empty name")

        value = value.strip()
        if len(value) >= 2 and value[0] in _QUOTES and value[-1] == value[0]:
            value = value[1:-1]
        else:
            # Trailing comment, only when the value is unquoted: a token is never
            # written with a bare ` #` inside it, but a quoted value might be.
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()

        values[name] = value

    return values


def find_dotenv(start: Path | None = None) -> Path | None:
    """The `.env` in `start` (default: the current directory), or None.

    Deliberately does NOT walk up the tree: an ancestor directory's `.env` silently
    applying to a run in a subdirectory is a surprise, and one wrong enough to
    misdirect a credential. Pass `--env-file` for anything outside the CWD.
    """
    directory = Path(start) if start is not None else Path.cwd()
    candidate = directory / DEFAULT_ENV_FILENAME
    return candidate if candidate.is_file() else None


def load_dotenv(
    path: Path | None = None,
    *,
    override: bool = False,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Load `path` (default: `./.env`) into the environment; return the names set.

    Names already present are skipped unless `override=True`. Returns only the names
    this call actually set, so a caller can report "loaded 3 names from .env" without
    ever touching the values. Missing file -> `[]`. A malformed file raises
    `DotenvError`: a credential file that half-loads is worse than one that fails.
    """
    target = os.environ if env is None else env

    resolved = find_dotenv() if path is None else Path(path)
    if resolved is None or not resolved.is_file():
        return []

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as e:
        raise DotenvError(f"could not read {resolved}: {e}") from e

    values = parse_dotenv(text)

    applied: list[str] = []
    for name, value in values.items():
        if not override and target.get(name):
            continue
        target[name] = value
        applied.append(name)
        if is_secret_name(name):
            register_secret(value)

    return applied
