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
