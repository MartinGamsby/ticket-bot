"""Repo-level sanity checks for the end state this section builds toward: the
Solari cookbook content is gone, and the docs describe ticketbot -- not what used
to be here. Deliberately independent of the rest of the suite (no imports beyond
the standard library) so it still catches a regression even if something else in
`ticketbot/` fails to import.
"""

from __future__ import annotations

from pathlib import Path

from ticketbot.config.redact import PATTERNS

ROOT = Path(__file__).resolve().parent.parent


def test_examples_directory_is_gone() -> None:
    assert not (ROOT / "examples").exists()


def test_license_file_is_gone() -> None:
    assert not (ROOT / "LICENSE").exists()


def test_no_profile_contains_a_secret_shaped_literal() -> None:
    profiles_dir = ROOT / "profiles"
    paths = sorted(profiles_dir.glob("*.yaml"))
    assert paths, f"no profiles found under {profiles_dir}"
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        for name, pattern in PATTERNS:
            assert not pattern.search(raw), f"{path.name} contains what looks like a real {name} secret"


def test_readme_does_not_mention_the_solari_cookbook() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Solari Cookbook" not in readme
