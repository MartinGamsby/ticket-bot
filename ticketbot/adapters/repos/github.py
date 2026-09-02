"""`GithubRepo` -- everything `GitLocalRepo` does, plus `ensure_clone()`, `push()`
and `open_pr()` against a real GitHub remote.

Reuses `ticketbot.adapters.sinks.github_pr`'s `github_rest_headers()` and
`write_body_tempfile()` helpers so this repo adapter and the section-6 PR-comment
sink can never diverge on how they talk to GitHub (same auth/version headers, same
"never an inline `--body`" rule for `gh`).

**`open_pr` never merges.** There is no merge subcommand invocation, no PUT to a
merge endpoint, and no automatic-merge flag anywhere in this file -- verified by a
source-level test.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import httpx

from ...config.loader import expand_env
from ...config.redact import redact, register_secret
from ...config.schema import AdapterConfig
from ..sinks.github_pr import github_rest_headers, write_body_tempfile
from .base import RepoError, run_git
from .git_local import GitLocalRepo

logger = logging.getLogger(__name__)

_SSH_CLONE_RE = re.compile(r"^git@[^:/]+:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$")
_HTTPS_CLONE_RE = re.compile(r"^https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$")
_PR_URL_TOKEN_RE = re.compile(r"https?://github\.com/\S+")

_AUTH_ERROR_HINTS = (
    "authentication failed",
    "permission denied",
    "could not read username",
    "could not read password",
    "403",
)


def _parse_owner_repo(clone_url: str) -> tuple[str, str]:
    """`owner/repo` from an SSH (`git@github.com:acme/app.git`) or HTTPS
    (`https://github.com/acme/app[.git]`) clone URL. Anything else raises."""
    url = clone_url.strip()
    m = _SSH_CLONE_RE.match(url) or _HTTPS_CLONE_RE.match(url)
    if not m:
        raise RepoError(f"cannot parse owner/repo from clone URL: {clone_url!r}")
    return m.group("owner"), m.group("repo")


def _last_pr_url(text: str) -> str | None:
    matches = _PR_URL_TOKEN_RE.findall(text)
    return matches[-1].rstrip(").,\"'") if matches else None


def _looks_like_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _AUTH_ERROR_HINTS)


class GithubRepo(GitLocalRepo):
    """Everything `GitLocalRepo` does, plus clone / push / open_pr."""

    def __init__(
        self,
        cfg: AdapterConfig,
        *,
        base_dir: Path | None = None,
        run_dir: Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        clone = cfg.opt("clone")
        if not clone:
            raise RepoError("repo type=github requires 'clone' (a git@ or https:// URL)")
        self.clone_url: str = expand_env(str(clone))
        self.owner, self.repo_name = _parse_owner_repo(self.clone_url)

        super().__init__(cfg, base_dir=base_dir, run_dir=run_dir)

        if cfg.opt("path") is None:
            # GitLocalRepo defaulted `path` to base_dir; a github repo instead
            # defaults to a per-repo cache directory next to the profile.
            base = Path(base_dir) if base_dir is not None else Path(".")
            self.path = (base / f"{self.owner}-{self.repo_name}").resolve()
            if cfg.opt("worktrees_dir") is None:
                self.worktrees_dir = (self.path.parent / ".ticketbot-worktrees").resolve()

        self.remote: str = str(cfg.opt("remote", "origin"))
        token = cfg.opt("token")
        self.token: str | None = expand_env(str(token)) if token else None
        register_secret(self.token)
        self.api_url: str = expand_env(str(cfg.opt("api_url", "https://api.github.com"))).rstrip("/")
        self.prefer_gh: bool = bool(cfg.opt("prefer_gh", True))
        self.draft_pr: bool = bool(cfg.opt("draft_pr", True))

        self._client: httpx.Client | None = client

    def describe(self) -> str:
        branch = self._branch or self.base_branch or "?"
        return f"{self.owner}/{self.repo_name} @ {branch}"

    # ------------------------------------------------------------------ #
    # clone / checkout
    # ------------------------------------------------------------------ #

    def ensure_clone(self) -> Path:
        exists_and_nonempty = self.path.exists() and self.path.is_dir() and any(self.path.iterdir())
        if not exists_and_nonempty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            run_git(["clone", self.clone_url, str(self.path)], cwd=self.path.parent, timeout=600)
        else:
            # Never `git pull` -- no merge surprises in a repo we don't own the state of.
            run_git(["fetch", self.remote, "--prune"], cwd=self.path, timeout=600)
        return self.path

    def checkout(self, branch: str) -> Path:
        self.ensure_clone()
        return super().checkout(branch)

    # ------------------------------------------------------------------ #
    # push
    # ------------------------------------------------------------------ #

    def push(self) -> None:
        ws = self.workspace()
        branch = self._branch
        if not branch:
            raise RepoError("push() called before checkout()")
        try:
            run_git(["push", "-u", self.remote, branch], cwd=ws)
        except RepoError as exc:
            message = str(exc)
            if _looks_like_auth_error(message):
                raise RepoError(
                    f"{message}\npush failed with what looks like an authentication error; "
                    "ensure a credential helper or SSH agent is configured (the token is "
                    "never embedded in the remote URL)"
                ) from exc
            raise

    # ------------------------------------------------------------------ #
    # open_pr -- gh CLI when on PATH and preferred, else REST. Never merges.
    # ------------------------------------------------------------------ #

    def open_pr(self, title: str, body: str) -> str | None:
        """`title` comes from untrusted ticket text and `body` from the reporter's
        model-written `pr.md`; both are `redact()`ed before they leave the machine,
        since a pull request is world-readable on a public repository.
        """
        branch = self._branch
        if not branch:
            raise RepoError("open_pr() called before checkout()")
        title = redact(title)
        body = redact(body)
        base = self.base_branch or "main"

        gh = shutil.which("gh") if self.prefer_gh else None
        if gh:
            return self._open_pr_via_gh(gh, title, body, base, branch)
        return self._open_pr_via_rest(title, body, base, branch)

    def _open_pr_via_gh(self, gh: str, title: str, body: str, base: str, branch: str) -> str | None:
        body_file = write_body_tempfile(body)
        try:
            argv = [
                gh, "pr", "create",
                "--title", title,
                "--body-file", body_file,
                "--base", base,
                "--head", branch,
            ]
            if self.draft_pr:
                argv.append("--draft")
            try:
                result = subprocess.run(argv, cwd=str(self.workspace()), shell=False, capture_output=True)
            except OSError as exc:
                raise RepoError(f"failed to run gh: {exc}") from exc
        finally:
            Path(body_file).unlink(missing_ok=True)

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode != 0:
            if "already exists" in (stdout + stderr).lower():
                existing = self._lookup_existing_pr_via_gh(branch)
                if existing is not None:
                    return existing
            raise RepoError(f"gh pr create failed: {redact((stderr or stdout)[:500])}")

        url = _last_pr_url(stdout)
        if url is None:
            raise RepoError(f"gh pr create succeeded but no PR URL found in output: {redact(stdout[:500])}")
        return url

    def _lookup_existing_pr_via_gh(self, branch: str) -> str | None:
        gh = shutil.which("gh")
        if gh is None:
            return None
        try:
            result = subprocess.run(
                [gh, "pr", "view", branch, "--json", "url"],
                cwd=str(self.workspace()), shell=False, capture_output=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return data.get("url")

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def _open_pr_via_rest(self, title: str, body: str, base: str, branch: str) -> str | None:
        client = self._get_client()
        url = f"{self.api_url}/repos/{self.owner}/{self.repo_name}/pulls"
        headers = github_rest_headers(self.token)
        payload = {"title": title, "body": body, "head": branch, "base": base, "draft": self.draft_pr}
        try:
            response = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise RepoError(f"request to {url} failed: {redact(str(exc))}") from exc

        if response.status_code == 422 and "already exists" in response.text.lower():
            existing = self._lookup_existing_pr_via_rest(branch)
            if existing is not None:
                return existing
        if response.status_code >= 300:
            raise RepoError(f"HTTP {response.status_code} from {url}: {redact(response.text[:500])}")
        return response.json()["html_url"]

    def _lookup_existing_pr_via_rest(self, branch: str) -> str | None:
        client = self._get_client()
        url = f"{self.api_url}/repos/{self.owner}/{self.repo_name}/pulls"
        params = {"head": f"{self.owner}:{branch}"}
        try:
            response = client.get(url, headers=github_rest_headers(self.token), params=params)
        except httpx.HTTPError:
            return None
        if response.status_code >= 300:
            return None
        data = response.json()
        if isinstance(data, list) and data:
            return data[0].get("html_url")
        return None

    # ------------------------------------------------------------------ #
    # cleanup
    # ------------------------------------------------------------------ #

    def cleanup(self) -> None:
        super().cleanup()
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask a real error
                logger.warning("github: failed to close httpx client: %s", exc)
