"""YAML profile loading: `extends:` deep-merge, the `builtin:` scheme, profile-relative
path resolution, and `${ENV}` references kept unexpanded until an adapter uses them.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .schema import Profile

BUILTIN_SCHEME = "builtin:"
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised for any problem loading, resolving or merging a profile."""


class MissingEnvError(ConfigError):
    """Raised when an `${ENV}` reference has no usable value at expansion time."""


def builtin_root() -> Path:
    """<package dir>/builtin — resolved relative to this installed package."""
    return (Path(__file__).resolve().parent.parent / "builtin").resolve()


def _has_dotdot(rel: str) -> bool:
    return any(part == ".." for part in Path(rel.replace("\\", "/")).parts)


def resolve_ref(ref: str, base_dir: Path) -> Path:
    """Resolve a config reference to an absolute, existing `Path`.

    `builtin:pipelines/standard.yaml` -> <builtin_root>/pipelines/standard.yaml.
    Anything else -> (base_dir / ref).resolve(). Absolute paths pass through.
    Raises ConfigError if the resolved path does not exist, or if a `builtin:` ref
    contains a `..` segment / escapes builtin_root().
    """
    if ref.startswith(BUILTIN_SCHEME):
        rel = ref[len(BUILTIN_SCHEME):]
        if _has_dotdot(rel):
            raise ConfigError(f"builtin: ref must not contain '..': {ref!r}")
        root = builtin_root()
        resolved = (root / rel).resolve()
        if resolved != root and root not in resolved.parents:
            raise ConfigError(f"builtin: ref escapes the builtin package: {ref!r}")
    else:
        path = Path(ref)
        resolved = path if path.is_absolute() else (Path(base_dir) / path)
        resolved = resolved.resolve()

    if not resolved.exists():
        raise ConfigError(f"config reference does not exist: {ref!r} (resolved to {resolved})")
    return resolved


def load_yaml(path: Path) -> dict:
    """`yaml.safe_load` ONLY. Raises ConfigError on I/O errors, invalid YAML, or a
    non-mapping top-level document (never executes arbitrary tags)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: expected a YAML mapping at the top level, got {type(data).__name__}"
        )
    return data


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge; scalars and LISTS in `override` REPLACE the base value
    (lists are never concatenated). Returns a new, fully independent dict — neither
    `base` nor `override` is mutated, and mutating the result afterwards cannot
    reach back into either input."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = deep_merge(base_value, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_profile_dict(path: str | Path, _seen: set[Path] | None = None) -> tuple[dict, Path]:
    """Resolve `extends:` chains (child over parent) and return (merged_dict, base_dir)
    where base_dir is the directory of the OUTERMOST (child) profile.

    `extends:` is resolved with resolve_ref() relative to the file that declares it.
    Raises ConfigError on a cycle, naming the files involved. Drops the `extends`
    key from the result.
    """
    path = Path(path).resolve()
    own_base_dir = path.parent
    seen = set() if _seen is None else _seen

    if path in seen:
        chain = " -> ".join(str(p) for p in sorted(seen | {path}, key=str))
        raise ConfigError(f"extends: cycle detected: {chain}")
    seen = seen | {path}

    data = load_yaml(path)
    extends_ref = data.pop("extends", None)

    if extends_ref:
        parent_path = resolve_ref(extends_ref, path.parent)
        parent_dict, _ = load_profile_dict(parent_path, seen)
        merged = deep_merge(parent_dict, data)
    else:
        merged = data

    return merged, own_base_dir


def load_profile(path: str | Path) -> Profile:
    """load_profile_dict -> Profile.model_validate -> set base_dir. `${ENV}` refs are
    left UNEXPANDED in the resulting model."""
    merged, base_dir = load_profile_dict(path)
    profile = Profile.model_validate(merged)
    profile.base_dir = base_dir
    return profile


def expand_env(value: str, env: Mapping[str, str] | None = None) -> str:
    """Replace every `${NAME}` with `env[NAME]`. Raises MissingEnvError naming the
    variable if it is unset or empty. `env` defaults to `os.environ`. Non-str input
    is returned unchanged. Called by ADAPTERS at use time, never by the loader."""
    if not isinstance(value, str):
        return value
    if env is None:
        env = os.environ

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        val = env.get(name)
        if not val:
            raise MissingEnvError(
                f"environment variable {name!r} is not set (referenced as ${{{name}}})"
            )
        return val

    return ENV_REF.sub(_sub, value)


def has_env_ref(value: object) -> bool:
    return isinstance(value, str) and bool(ENV_REF.search(value))


def resolved_yaml(profile: Profile) -> str:
    """`yaml.safe_dump` of the profile with `${ENV}` refs still unexpanded, for
    `runs/<id>/config.resolved.yaml`. Callers (e.g. the CLI) must additionally pass
    the result through `redact.redact()` before printing or writing it anywhere."""
    data = profile.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
