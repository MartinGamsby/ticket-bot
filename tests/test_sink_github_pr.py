"""`GithubPrSink` -- no network: the REST path is driven through
`httpx.MockTransport`; the `gh` CLI path is driven through a fake executable on
PATH so no real `gh` is required. Never creates or lands a PR -- asserted at the
module-source level too.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import httpx
import pytest

from ticketbot.adapters.sinks.base import SinkError
from ticketbot.adapters.sinks.github_pr import GithubPrSink
from ticketbot.config.schema import AdapterConfig
from ticketbot.core.workitem import Attachment, WorkItem

PR_URL = "https://github.com/acme/app/pull/42"


def _cfg(**opts) -> AdapterConfig:
    opts.setdefault("prefer_gh", False)  # tests opt into `gh` explicitly
    return AdapterConfig(type="github_pr", **opts)


def _item() -> WorkItem:
    return WorkItem(id="ENG-1", title="T", external_id="ENG-1")


def _client_and_capture(status_code: int = 201, json_body: dict | None = None, text: str | None = None):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["json"] = json.loads(request.content) if request.content else None
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=json_body if json_body is not None else {"id": 1})

    return httpx.Client(transport=httpx.MockTransport(handler)), captured


# ---- no PR url yet ----------------------------------------------------------


def test_no_pr_url_is_a_silent_noop():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)
    sink.comment(_item(), "hello")  # must not raise
    assert "request" not in captured


def test_pr_url_getter_supplies_the_url_lazily():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(token="tkn12345678"), pr_url_getter=lambda: PR_URL, client=client)
    sink.comment(_item(), "hello")
    assert captured["request"] is not None


def test_set_pr_url_wins_over_getter():
    client, captured = _client_and_capture()
    calls = []

    def getter():
        calls.append(1)
        return "https://github.com/other/repo/pull/1"

    sink = GithubPrSink(_cfg(token="tkn12345678"), pr_url_getter=getter, client=client)
    sink.set_pr_url(PR_URL)
    sink.comment(_item(), "hello")

    assert str(captured["request"].url).startswith("https://api.github.com/repos/acme/app/")
    assert calls == []


# ---- REST path ----------------------------------------------------------------


def test_rest_path_sends_required_headers_and_url():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(token="ghp_1234567890abcdef"), client=client)
    sink.set_pr_url(PR_URL)

    sink.comment(_item(), "great work")

    req = captured["request"]
    assert str(req.url) == "https://api.github.com/repos/acme/app/issues/42/comments"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert req.headers["authorization"] == "Bearer ghp_1234567890abcdef"
    assert captured["json"] == {"body": "great work"}


def test_rest_path_omits_authorization_when_no_token():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)
    sink.set_pr_url(PR_URL)
    sink.comment(_item(), "hello")
    assert "authorization" not in captured["request"].headers


def test_non_pr_url_raises():
    client, _ = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)
    sink.set_pr_url("https://github.com/acme/app/issues/9")
    with pytest.raises(SinkError):
        sink.comment(_item(), "hello")


def test_non_2xx_raises_sink_error_with_redacted_token():
    token = "ghp_leaktestleaktestleak1234"
    client, _ = _client_and_capture(status_code=500, text=f"boom Authorization: Bearer {token}")
    sink = GithubPrSink(_cfg(token=token), client=client)
    sink.set_pr_url(PR_URL)

    with pytest.raises(SinkError) as exc_info:
        sink.comment(_item(), "hello")
    assert token not in str(exc_info.value)


def test_attachments_are_referenced_by_path_in_the_comment_body():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)
    sink.set_pr_url(PR_URL)

    sink.comment(_item(), "see screenshot", attachments=[Attachment(filename="s.png", path=Path("runs/1/s.png"))])

    body = captured["json"]["body"]
    assert "see screenshot" in body
    assert "s.png" in body


def test_link_posts_as_a_comment():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)
    sink.set_pr_url(PR_URL)

    sink.link(_item(), "https://jira.example.com/browse/ENG-1", "Ticket")

    assert "[Ticket](https://jira.example.com/browse/ENG-1)" in captured["json"]["body"]


def test_transition_and_unassign_are_noops():
    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)
    sink.set_pr_url(PR_URL)

    sink.transition(_item(), "In Review")
    sink.unassign(_item())

    assert "request" not in captured


# ---- gh CLI path ----------------------------------------------------------------


def _make_fake_gh(tmp_path: Path, *, exit_code: int = 0, record_path: Path | None = None) -> Path:
    """A fake `gh` on PATH: records its argv (and the body-file contents) to
    `record_path` as JSON, so the test never needs a real `gh` or network.
    """
    script = tmp_path / "fake_gh.py"
    record = record_path or (tmp_path / "gh-call.json")
    script.write_text(
        "import sys, json, pathlib\n"
        "argv = sys.argv[1:]\n"
        "body_file = argv[argv.index('--body-file') + 1]\n"
        "body = pathlib.Path(body_file).read_text(encoding='utf-8')\n"
        f"pathlib.Path(r'{record}').write_text(json.dumps({{'argv': argv, 'body': body}}), encoding='utf-8')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        wrapper = tmp_path / "gh.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "gh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


def test_gh_cli_path_used_when_preferred_and_on_path(tmp_path, monkeypatch):
    record = tmp_path / "gh-call.json"
    _make_fake_gh(tmp_path, record_path=record)
    monkeypatch.setenv("PATH", str(tmp_path) + (";" if sys.platform == "win32" else ":") + __import__("os").environ.get("PATH", ""))

    sink = GithubPrSink(AdapterConfig(type="github_pr", prefer_gh=True))
    sink.set_pr_url(PR_URL)
    sink.comment(_item(), "via gh")

    call = json.loads(record.read_text(encoding="utf-8"))
    assert call["argv"][:2] == ["pr", "comment"]
    assert PR_URL in call["argv"]
    assert call["body"] == "via gh"


def test_gh_cli_failure_raises_sink_error(tmp_path, monkeypatch):
    _make_fake_gh(tmp_path, exit_code=1)
    monkeypatch.setenv("PATH", str(tmp_path) + (";" if sys.platform == "win32" else ":") + __import__("os").environ.get("PATH", ""))

    sink = GithubPrSink(AdapterConfig(type="github_pr", prefer_gh=True))
    sink.set_pr_url(PR_URL)
    with pytest.raises(SinkError):
        sink.comment(_item(), "will fail")


def test_prefer_gh_false_uses_rest_even_when_gh_is_on_path(tmp_path, monkeypatch):
    record = tmp_path / "gh-call.json"
    _make_fake_gh(tmp_path, record_path=record)
    monkeypatch.setenv("PATH", str(tmp_path) + (";" if sys.platform == "win32" else ":") + __import__("os").environ.get("PATH", ""))

    client, captured = _client_and_capture()
    sink = GithubPrSink(_cfg(), client=client)  # prefer_gh=False
    sink.set_pr_url(PR_URL)
    sink.comment(_item(), "via rest")

    assert not record.exists()
    assert captured["json"] == {"body": "via rest"}


# ---- describe / no-merge -----------------------------------------------------------


def test_describe():
    assert GithubPrSink(_cfg()).describe() == "github_pr"


def test_module_source_contains_no_merge_call():
    import ticketbot.adapters.sinks.github_pr as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "merge" not in source.lower()
