"""`JiraSink` -- no network: every test drives it through `httpx.MockTransport`
and asserts the exact request body (ADF comment shape, transition lookup-then-
POST, unassign, attachment upload headers/ordering).
"""

from __future__ import annotations

import json

import httpx
import pytest

from ticketbot.adapters.sinks.base import SinkError
from ticketbot.adapters.sinks.jira import JiraSink
from ticketbot.config.schema import AdapterConfig
from ticketbot.core.workitem import Attachment, WorkItem

BASE_URL = "https://acme.atlassian.net"


def _cfg(**opts) -> AdapterConfig:
    opts.setdefault("base_url", BASE_URL)
    opts.setdefault("email", "bot@acme.com")
    opts.setdefault("token", "tok1234567890")
    return AdapterConfig(type="jira", **opts)


def _item() -> WorkItem:
    return WorkItem(id="ENG-1", title="T", external_id="ENG-1")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- comment() ------------------------------------------------------------------


def test_comment_posts_exact_adf_for_two_paragraph_input():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "1"})

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.comment(_item(), "first paragraph\n\nsecond paragraph")

    assert captured["url"] == f"{BASE_URL}/rest/api/3/issue/ENG-1/comment"
    body = captured["json"]["body"]
    assert body["type"] == "doc"
    assert body["version"] == 1
    assert body["content"] == [
        {"type": "paragraph", "content": [{"type": "text", "text": "first paragraph"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "second paragraph"}]},
    ]


def test_attachments_upload_before_comment_with_no_check_header():
    order = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/attachments"):
            order.append("attachment")
            assert request.headers["x-atlassian-token"] == "no-check"
            return httpx.Response(200, json=[{"id": "1"}])
        if url.endswith("/comment"):
            order.append("comment")
            return httpx.Response(201, json={"id": "1"})
        raise AssertionError(f"unexpected request to {url}")

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.comment(
        _item(),
        "see attached",
        attachments=[Attachment(filename="log.txt", content_type="text/plain", data=b"log contents")],
    )

    assert order == ["attachment", "comment"]


def test_attachment_upload_failure_is_logged_and_noted_in_comment_not_aborted():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/attachments"):
            return httpx.Response(500, text="upload failed")
        if url.endswith("/comment"):
            return httpx.Response(201, json={"id": "1"})
        raise AssertionError(f"unexpected request to {url}")

    captured = {}
    orig_handler = handler

    def wrapped(request: httpx.Request) -> httpx.Response:
        response = orig_handler(request)
        if str(request.url).endswith("/comment"):
            captured["json"] = json.loads(request.content)
        return response

    sink = JiraSink(_cfg(), client=_client(wrapped))
    sink.comment(
        _item(),
        "see attached",
        attachments=[Attachment(filename="log.txt", content_type="text/plain", data=b"data")],
    )  # must not raise

    from ticketbot.adapters.sinks.adf import adf_to_text

    text = adf_to_text(captured["json"]["body"])
    assert "attachment log.txt failed to upload" in text


# ---- unassign() -----------------------------------------------------------------


def test_unassign_puts_account_id_null():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(204)

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.unassign(_item())

    assert captured["method"] == "PUT"
    assert captured["url"] == f"{BASE_URL}/rest/api/3/issue/ENG-1/assignee"
    assert captured["json"] == {"accountId": None}


# ---- transition() ---------------------------------------------------------------


def test_transition_does_get_then_post_with_matching_id():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "to": {"name": "To Do"}},
                        {"id": "21", "to": {"name": "In Review"}},
                    ]
                },
            )
        assert json.loads(request.content) == {"transition": {"id": "21"}}
        return httpx.Response(204)

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.transition(_item(), "in review")  # case-insensitive match

    assert [m for m, _ in calls] == ["GET", "POST"]
    assert calls[0][1] == f"{BASE_URL}/rest/api/3/issue/ENG-1/transitions"


def test_transition_unknown_target_raises_sink_error_listing_available():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transitions": [{"id": "11", "to": {"name": "To Do"}}]})

    sink = JiraSink(_cfg(), client=_client(handler))
    with pytest.raises(SinkError) as exc_info:
        sink.transition(_item(), "Nonexistent Status")
    assert "To Do" in str(exc_info.value)


# ---- link() -------------------------------------------------------------------


def test_link_posts_to_remotelink():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.link(_item(), "https://github.com/acme/app/pull/42", "PR #42")

    assert captured["url"] == f"{BASE_URL}/rest/api/3/issue/ENG-1/remotelink"
    assert captured["json"] == {"object": {"url": "https://github.com/acme/app/pull/42", "title": "PR #42"}}


# ---- errors / credentials --------------------------------------------------------


def test_non_2xx_raises_sink_error_without_leaking_credentials():
    token = "leaktoken1234567890"
    email = "leak@acme.com"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=f"forbidden for {email} token {token}")

    sink = JiraSink(_cfg(email=email, token=token), client=_client(handler))
    with pytest.raises(SinkError) as exc_info:
        sink.unassign(_item())

    message = str(exc_info.value)
    assert token not in message
    assert email not in message


def test_describe_includes_host():
    sink = JiraSink(_cfg(), client=_client(lambda r: httpx.Response(200, json={})))
    assert sink.describe() == "jira (acme.atlassian.net)"


# ---- outbound redaction ----------------------------------------------------------


def test_comment_body_is_redacted_before_it_reaches_jira(monkeypatch):
    """A ticket is readable by whoever filed it. The comment body is model-written
    text, so a credential quoted into it must be masked on the way out -- exactly
    as `FileSink` already masks the same text on the way to disk.
    """
    from ticketbot.config import redact as redact_module

    monkeypatch.setattr(redact_module, "_default", redact_module.Redactor())
    redact_module.register_secret("supersecretvalue123")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "1"})

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.comment(_item(), "the key is supersecretvalue123 and also sk-ant-aaaaaaaaaaaaaaaaaaaa")

    text = json.dumps(captured["json"])
    assert "supersecretvalue123" not in text
    assert "sk-ant-" not in text
    # The scrub runs BEFORE markdown_to_adf, so the `***REDACTED***` marker is then
    # re-read as markdown bold and lands as a `strong` text node saying "REDACTED".
    # That ordering is deliberate -- see JiraSink.comment's docstring: scrubbing the
    # finished ADF instead would miss a token the inline parser had already split on
    # its own `_`/`*` characters.
    assert "REDACTED" in text


def test_a_secret_containing_markdown_inline_characters_is_still_scrubbed():
    """The reason the scrub precedes `markdown_to_adf`: `github_pat_...` contains
    underscores the inline parser reads as italic delimiters, so a post-conversion
    scrub would be looking at two halves of a token that no pattern matches.
    """
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "1"})

    sink = JiraSink(_cfg(), client=_client(handler))
    sink.comment(_item(), "leaked github_pat_11ABCDEFG0aaaaaaaaaaaa_bbbbbbbbbbbb here")

    text = json.dumps(captured["json"])
    assert "github_pat_" not in text


def test_base_url_is_not_registered_as_a_secret(monkeypatch):
    """The tenant host is a substring of every `{base_url}/browse/KEY` ticket URL.
    Registering it as a literal secret rewrote that URL to `***REDACTED***` in
    artifacts and, now that comments are scrubbed, in the comment itself.
    """
    from ticketbot.config import redact as redact_module

    monkeypatch.setattr(redact_module, "_default", redact_module.Redactor())
    JiraSink(_cfg(), client=_client(lambda r: httpx.Response(200, json={})))

    assert redact_module.redact(f"see {BASE_URL}/browse/ENG-1") == f"see {BASE_URL}/browse/ENG-1"
