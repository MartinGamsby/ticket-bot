"""`GithubPrSink` -- reports back onto a pull request as comments (`gh` CLI when on
PATH and preferred, else REST). This sink never opens a pull request itself (the
repo adapter in section 7 does that) and never advances or finalizes one either --
this file's only job is posting comments and links onto a PR that already exists.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from ...config.loader import expand_env
from ...config.redact import redact, register_secret
from ...config.schema import AdapterConfig
from ...core.workitem import Attachment, WorkItem
from .base import SinkError

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$")


def github_rest_headers(token: str | None) -> dict[str, str]:
    """Shared GitHub REST headers -- used by this sink and by the `github` repo
    adapter (section 7) so the two GitHub clients can never diverge on auth/version
    headers.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def write_body_tempfile(body: str, *, suffix: str = ".md") -> str:
    """Write `body` to a UTF-8, BOM-less, LF-newline temp file and return its path.

    Shared by this sink's `gh pr comment --body-file` and the `github` repo
    adapter's `gh pr create --body-file` so both call sites behave identically --
    model-written text never reaches a command line inline. Caller deletes the file
    (`Path(path).unlink(missing_ok=True)`).
    """
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=suffix, delete=False) as f:
        f.write(body)
        return f.name


class GithubPrSink:
    def __init__(
        self,
        cfg: AdapterConfig,
        *,
        pr_url_getter: Callable[[], str | None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """`pr_url_getter` is supplied by the orchestrator and returns
        `run.extra['pr_url']`; the URL can also be set afterwards via
        `set_pr_url(url)` once the repo adapter has opened the PR.
        """
        token = cfg.opt("token")
        self.token: str | None = expand_env(str(token)) if token else None
        register_secret(self.token)

        self.api_url = expand_env(str(cfg.opt("api_url", "https://api.github.com"))).rstrip("/")
        self.prefer_gh = bool(cfg.opt("prefer_gh", True))
        self.timeout_s = float(cfg.opt("timeout_s", 30))

        self._pr_url_getter = pr_url_getter
        self._pr_url: str | None = None
        self._client = client if client is not None else httpx.Client(timeout=self.timeout_s)

    def describe(self) -> str:
        return "github_pr"

    def set_pr_url(self, url: str) -> None:
        self._pr_url = url

    def _current_pr_url(self) -> str | None:
        if self._pr_url:
            return self._pr_url
        if self._pr_url_getter is not None:
            return self._pr_url_getter()
        return None

    def comment(self, item: WorkItem, markdown: str, attachments: Sequence[Attachment] = ()) -> None:
        """The body is `redact()`ed on the way out: it is model-written text and a
        PR comment is world-readable on a public repository. `FileSink` already
        scrubs the same text locally -- the remote copy must not be the leakier one.
        """
        pr_url = self._current_pr_url()
        if not pr_url:
            logger.info("github_pr: no PR URL yet; dropping comment for %s (no-op)", item.key)
            return

        body = markdown
        if attachments:
            # GitHub issue/PR comments cannot carry file uploads via the REST API
            # or `gh pr comment` -- reference each attachment by its local path
            # in the comment text instead of attaching it.
            refs = "\n".join(f"- `{att.path}`" if att.path else f"- {att.filename}" for att in attachments)
            body = f"{markdown}\n\nAttachments (see run directory):\n{refs}"
        body = redact(body)

        gh = shutil.which("gh") if self.prefer_gh else None
        if gh:
            self._comment_via_gh(gh, pr_url, body)
        else:
            self._comment_via_rest(pr_url, body)

    def _comment_via_gh(self, gh: str, pr_url: str, body: str) -> None:
        # `--body-file`, never an inline body: both for PowerShell 5.1 quoting and
        # to avoid argv injection from model-written text.
        body_file = write_body_tempfile(body)
        try:
            result = subprocess.run(
                [gh, "pr", "comment", pr_url, "--body-file", body_file],
                shell=False,
                capture_output=True,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")
                raise SinkError(f"github_pr: gh pr comment failed: {redact(stderr[:500])}")
        except OSError as exc:
            raise SinkError(f"github_pr: failed to run gh: {exc}") from exc
        finally:
            Path(body_file).unlink(missing_ok=True)

    def _parse_pr_url(self, pr_url: str) -> tuple[str, str, str]:
        m = _PR_URL_RE.match(pr_url.strip())
        if not m:
            raise SinkError(f"github_pr: not a GitHub pull request URL: {pr_url!r}")
        return m.group("owner"), m.group("repo"), m.group("number")

    def _comment_via_rest(self, pr_url: str, body: str) -> None:
        owner, repo, number = self._parse_pr_url(pr_url)
        url = f"{self.api_url}/repos/{owner}/{repo}/issues/{number}/comments"
        headers = github_rest_headers(self.token)
        try:
            response = self._client.post(url, headers=headers, json={"body": body})
        except httpx.HTTPError as exc:
            raise SinkError(f"github_pr: request to {url} failed: {redact(str(exc))}") from exc
        if response.status_code >= 300:
            raise SinkError(f"github_pr: HTTP {response.status_code} from {url}: {redact(response.text[:500])}")

    def transition(self, item: WorkItem, state: str) -> None:
        pass  # a PR has no ticket "state" this sink can move

    def unassign(self, item: WorkItem) -> None:
        pass  # nothing to unassign on a PR

    def link(self, item: WorkItem, url: str, title: str) -> None:
        self.comment(item, f"[{title}]({url})")

    def close(self) -> None:
        self._client.close()
