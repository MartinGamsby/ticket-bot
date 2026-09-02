"""`GithubRepo` -- no real network. The REST path is driven through
`httpx.MockTransport`; the `gh` CLI path is driven through a monkeypatched
`subprocess.run` that captures argv, exactly as `test_sink_github_pr.py` does for
the section-6 sink. `ensure_clone()`/`checkout()` DO run real (local, no-network)
git: `path` is pre-seeded with a throwaway repo from the `git_repo` fixture and
given itself as the `origin` remote, so `git fetch` succeeds without ever touching
the network. Never creates or lands a PR merge -- asserted at the module-source
level too.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from ticketbot.adapters.repos.base import RepoError
from ticketbot.adapters.repos.github import GithubRepo
from ticketbot.config.schema import AdapterConfig


def _cfg(**opts) -> AdapterConfig:
    return AdapterConfig(type="github", **opts)


def _add_self_origin(git_repo: Path) -> None:
    """Point 'origin' at the fixture repo itself, so `ensure_clone()`'s `git fetch`
    is a real, local, no-network git operation."""
    subprocess.run(
        ["git", "-C", str(git_repo), "remote", "add", "origin", str(git_repo)],
        check=True, capture_output=True,
    )


def _client_and_capture(status_code: int = 201, json_body: dict | list | None = None, text: str | None = None):
    captured: dict = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        captured["json"] = json.loads(request.content) if request.content else None
        if text is not None:
            return httpx.Response(status_code, text=text)
        body = json_body if json_body is not None else {"html_url": "https://github.com/acme/app/pull/5"}
        return httpx.Response(status_code, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), captured


# ---- owner/repo parsing --------------------------------------------------------


def test_parses_ssh_clone_url():
    repo = GithubRepo(_cfg(clone="git@github.com:acme/app.git"), base_dir=Path("."))
    assert repo.owner == "acme"
    assert repo.repo_name == "app"


def test_parses_https_clone_url():
    repo = GithubRepo(_cfg(clone="https://github.com/acme/app"), base_dir=Path("."))
    assert repo.owner == "acme"
    assert repo.repo_name == "app"


def test_parses_https_clone_url_with_dot_git_suffix():
    repo = GithubRepo(_cfg(clone="https://github.com/acme/app.git"), base_dir=Path("."))
    assert repo.owner == "acme"
    assert repo.repo_name == "app"


def test_bogus_clone_url_raises():
    with pytest.raises(RepoError):
        GithubRepo(_cfg(clone="not-a-url-at-all"), base_dir=Path("."))


def test_missing_clone_raises():
    with pytest.raises(RepoError):
        GithubRepo(_cfg(), base_dir=Path("."))


def test_describe_uses_owner_repo_and_branch(git_repo):
    _add_self_origin(git_repo)
    repo = GithubRepo(_cfg(clone="git@github.com:acme/app.git", path=str(git_repo)))
    repo.checkout("agent/eng-1-test")
    assert repo.describe() == "acme/app @ agent/eng-1-test"


# ---- open_pr via gh -------------------------------------------------------------


def test_open_pr_via_gh_uses_body_file_never_inline_and_right_flags(monkeypatch, git_repo):
    _add_self_origin(git_repo)
    cfg = _cfg(
        clone="git@github.com:acme/app.git", path=str(git_repo),
        prefer_gh=True, draft_pr=True, base_branch="main",
    )
    repo = GithubRepo(cfg)
    repo.checkout("agent/eng-2-test")

    monkeypatch.setattr(shutil, "which", lambda name: "gh" if name == "gh" else None)

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        body_file = argv[argv.index("--body-file") + 1]
        captured["body"] = Path(body_file).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, stdout=b"Creating pull request\nhttps://github.com/acme/app/pull/7\n", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    url = repo.open_pr("Fix login timeout", "This fixes it.\nSecond line.")

    argv = captured["argv"]
    assert argv[0] == "gh"
    assert argv[1:3] == ["pr", "create"]
    assert "--body-file" in argv
    assert "--body" not in argv  # never an inline body
    assert captured["body"] == "This fixes it.\nSecond line."
    assert "--draft" in argv
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == "agent/eng-2-test"
    assert captured["kwargs"]["shell"] is False
    assert url == "https://github.com/acme/app/pull/7"


def test_open_pr_via_gh_omits_draft_when_not_configured(monkeypatch, git_repo):
    _add_self_origin(git_repo)
    cfg = _cfg(
        clone="git@github.com:acme/app.git", path=str(git_repo),
        prefer_gh=True, draft_pr=False, base_branch="main",
    )
    repo = GithubRepo(cfg)
    repo.checkout("agent/eng-2b-test")

    monkeypatch.setattr(shutil, "which", lambda name: "gh" if name == "gh" else None)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=b"https://github.com/acme/app/pull/8\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.open_pr("title", "body")


def test_open_pr_via_gh_already_exists_falls_back_to_lookup(monkeypatch, git_repo):
    _add_self_origin(git_repo)
    cfg = _cfg(clone="git@github.com:acme/app.git", path=str(git_repo), prefer_gh=True, base_branch="main")
    repo = GithubRepo(cfg)
    repo.checkout("agent/eng-3-test")

    monkeypatch.setattr(shutil, "which", lambda name: "gh" if name == "gh" else None)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["pr", "create"]:
            return subprocess.CompletedProcess(
                argv, 1, stdout=b"", stderr=b'a pull request for branch "agent/eng-3-test" already exists\n'
            )
        if argv[1:3] == ["pr", "view"]:
            payload = json.dumps({"url": "https://github.com/acme/app/pull/9"}).encode("utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=b"")
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    url = repo.open_pr("title", "body")

    assert url == "https://github.com/acme/app/pull/9"
    assert any(c[1:3] == ["pr", "view"] for c in calls)


def test_open_pr_via_gh_nonzero_exit_raises(monkeypatch, git_repo):
    _add_self_origin(git_repo)
    cfg = _cfg(clone="git@github.com:acme/app.git", path=str(git_repo), prefer_gh=True, base_branch="main")
    repo = GithubRepo(cfg)
    repo.checkout("agent/eng-3b-test")

    monkeypatch.setattr(shutil, "which", lambda name: "gh" if name == "gh" else None)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"boom\n"),
    )

    with pytest.raises(RepoError):
        repo.open_pr("title", "body")


# ---- open_pr via REST -----------------------------------------------------------


def test_open_pr_via_rest_sends_headers_url_and_body(git_repo):
    _add_self_origin(git_repo)
    client, captured = _client_and_capture(json_body={"html_url": "https://github.com/acme/app/pull/5"})
    cfg = _cfg(
        clone="git@github.com:acme/app.git", path=str(git_repo), prefer_gh=False,
        base_branch="main", token="ghp_1234567890abcdef", draft_pr=True,
    )
    repo = GithubRepo(cfg, client=client)
    repo.checkout("agent/eng-4-test")

    url = repo.open_pr("Fix login timeout", "This fixes it.")

    req = captured["requests"][0]
    assert str(req.url) == "https://api.github.com/repos/acme/app/pulls"
    assert req.headers["authorization"] == "Bearer ghp_1234567890abcdef"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert captured["json"] == {
        "title": "Fix login timeout", "body": "This fixes it.",
        "head": "agent/eng-4-test", "base": "main", "draft": True,
    }
    assert url == "https://github.com/acme/app/pull/5"


def test_open_pr_via_rest_omits_authorization_when_no_token(git_repo):
    _add_self_origin(git_repo)
    client, captured = _client_and_capture()
    cfg = _cfg(clone="git@github.com:acme/app.git", path=str(git_repo), prefer_gh=False, base_branch="main")
    repo = GithubRepo(cfg, client=client)
    repo.checkout("agent/eng-4b-test")

    repo.open_pr("title", "body")

    assert "authorization" not in captured["requests"][0].headers


def test_open_pr_via_rest_422_already_exists_falls_back_to_lookup(git_repo):
    _add_self_origin(git_repo)

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for acme:agent/eng-5-test."}],
                },
            )
        return httpx.Response(200, json=[{"html_url": "https://github.com/acme/app/pull/11"}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = _cfg(clone="git@github.com:acme/app.git", path=str(git_repo), prefer_gh=False, base_branch="main")
    repo = GithubRepo(cfg, client=client)
    repo.checkout("agent/eng-5-test")

    url = repo.open_pr("title", "body")

    assert url == "https://github.com/acme/app/pull/11"
    assert any(r.method == "GET" for r in calls)


def test_open_pr_via_rest_non_2xx_raises_with_redacted_token(git_repo):
    token = "ghp_leaktestleaktestleak1234"
    client, _ = _client_and_capture(status_code=500, text=f"boom Authorization: Bearer {token}")
    _add_self_origin(git_repo)
    cfg = _cfg(
        clone="git@github.com:acme/app.git", path=str(git_repo), prefer_gh=False,
        base_branch="main", token=token,
    )
    repo = GithubRepo(cfg, client=client)
    repo.checkout("agent/eng-5b-test")

    with pytest.raises(RepoError) as exc_info:
        repo.open_pr("title", "body")
    assert token not in str(exc_info.value)


# ---- push ---------------------------------------------------------------------


def test_push_builds_expected_argv_and_never_forces(monkeypatch, git_repo):
    _add_self_origin(git_repo)
    cfg = _cfg(clone="git@github.com:acme/app.git", path=str(git_repo))
    repo = GithubRepo(cfg)
    repo.checkout("agent/eng-6-test")

    captured: dict = {}

    def fake_run_git(args, *, cwd, timeout=120, check=True):
        captured["args"] = args
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr("ticketbot.adapters.repos.github.run_git", fake_run_git)

    repo.push()

    assert captured["args"] == ["push", "-u", "origin", "agent/eng-6-test"]
    assert "--force" not in captured["args"]
    assert "-f" not in captured["args"]


def test_push_before_checkout_raises(git_repo):
    _add_self_origin(git_repo)
    cfg = _cfg(clone="git@github.com:acme/app.git", path=str(git_repo))
    repo = GithubRepo(cfg)
    with pytest.raises(RepoError):
        repo.push()


# ---- describe / no-merge, no-force-push, no-auto-merge ------------------------


def test_module_source_contains_no_merge_or_force_push():
    import ticketbot.adapters.repos.github as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "pr merge" not in lowered
    assert "/merge" not in lowered
    assert "auto-merge" not in lowered
    assert "--auto" not in lowered
    assert "--force" not in lowered
