"""`JiraSource` -- no network: every test drives it through
`httpx.MockTransport` and asserts the exact request. Covers the section-6
security rails: credentials never leak into an error message, and attachment
downloads are size-capped and jailed to the destination directory.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ticketbot.adapters.sources.base import SourceError, WorkItemNotFound
from ticketbot.adapters.sources.jira import JiraSource
from ticketbot.config.schema import AdapterConfig

BASE_URL = "https://acme.atlassian.net"


def _cfg(**opts) -> AdapterConfig:
    opts.setdefault("base_url", BASE_URL)
    opts.setdefault("email", "bot@acme.com")
    opts.setdefault("token", "tok1234567890")
    return AdapterConfig(type="jira", **opts)


def _issue(key="ENG-1", **field_overrides) -> dict:
    fields = {
        "summary": "Login times out",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Users see a spinner."}]}],
        },
        "issuetype": {"name": "Bug"},
        "status": {"name": "To Do"},
        "labels": ["agent", "sso"],
        "assignee": None,
        "comment": {"comments": []},
        "attachment": [],
    }
    fields.update(field_overrides)
    return {"key": key, "fields": fields}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- search / poll --------------------------------------------------------------


def test_poll_posts_to_search_jql_with_configured_jql():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"issues": [_issue()]})

    source = JiraSource(_cfg(jql='assignee = currentUser() AND status = "To Do"'), client=_client(handler))
    items = list(source.poll())

    assert captured["url"].endswith("/rest/api/3/search/jql")
    assert captured["json"]["jql"] == 'assignee = currentUser() AND status = "To Do"'
    assert len(items) == 1
    assert items[0].external_id == "ENG-1"


def test_poll_pages_on_next_page_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "nextPageToken" not in body:
            return httpx.Response(200, json={"issues": [_issue("ENG-1")], "nextPageToken": "page-2"})
        return httpx.Response(200, json={"issues": [_issue("ENG-2")]})

    source = JiraSource(_cfg(jql="x"), client=_client(handler))
    items = list(source.poll())

    assert [i.external_id for i in items] == ["ENG-1", "ENG-2"]
    assert len(calls) == 2
    assert calls[1]["nextPageToken"] == "page-2"


# ---- mapping --------------------------------------------------------------------


def test_issue_maps_to_workitem_with_flattened_description_and_configured_points_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_issue(
                "ENG-9",
                summary="Do the thing",
                customfield_10099=8.0,
                assignee={"displayName": "Ada Lovelace", "accountId": "acc-1"},
            ),
        )

    source = JiraSource(_cfg(points_field="customfield_10099"), client=_client(handler))
    item = source.fetch("ENG-9")

    assert item.title == "Do the thing"
    assert item.description == "Users see a spinner."
    assert item.issue_type == "Bug"
    assert item.status == "To Do"
    assert item.labels == ["agent", "sso"]
    assert item.assignee == "Ada Lovelace"
    assert item.story_points == 8.0
    assert item.url == f"{BASE_URL}/browse/ENG-9"
    assert item.external_id == "ENG-9"


def test_comments_and_attachments_are_mapped():
    issue = _issue(
        comment={
            "comments": [
                {
                    "id": "10001",
                    "author": {"displayName": "Ada Lovelace"},
                    "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Clarify please"}]}]},
                    "created": "2024-01-15T10:30:00.000+0000",
                }
            ]
        },
        attachment=[{"filename": "trace.log", "mimeType": "text/plain", "content": f"{BASE_URL}/secure/attachment/1/trace.log"}],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=issue)

    source = JiraSource(_cfg(), client=_client(handler))
    item = source.fetch("ENG-1")

    assert len(item.comments) == 1
    assert item.comments[0].author == "Ada Lovelace"
    assert item.comments[0].body == "Clarify please"
    assert item.comments[0].created_at is not None

    assert len(item.attachments) == 1
    assert item.attachments[0].filename == "trace.log"
    assert item.attachments[0].content_type == "text/plain"


def test_acceptance_extracted_from_h2_heading_in_description():
    issue = _issue(
        description={
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Intro text."}]},
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance Criteria"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Must work offline."}]},
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Notes"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Not part of acceptance."}]},
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=issue)

    source = JiraSource(_cfg(), client=_client(handler))
    item = source.fetch("ENG-1")

    assert "Must work offline." in item.acceptance
    assert "Not part of acceptance." not in item.acceptance


def test_no_acceptance_heading_yields_empty_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_issue())

    source = JiraSource(_cfg(), client=_client(handler))
    item = source.fetch("ENG-1")
    assert item.acceptance == ""


def test_fetch_uses_get_issue_with_configured_fields_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_issue())

    source = JiraSource(_cfg(points_field="customfield_10016"), client=_client(handler))
    source.fetch("ENG-1")

    assert "/rest/api/3/issue/ENG-1" in captured["url"]
    assert "customfield_10016" in captured["url"]


def test_fetch_without_external_id_raises_work_item_not_found():
    source = JiraSource(_cfg(), client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(WorkItemNotFound):
        source.fetch(None)


def test_fetch_404_raises_work_item_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    source = JiraSource(_cfg(), client=_client(handler))
    with pytest.raises(WorkItemNotFound):
        source.fetch("ENG-404")


# ---- claim() --------------------------------------------------------------------


def test_claim_puts_assignee_then_transitions():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET" and "/issue/ENG-1" in str(request.url) and "transitions" not in str(request.url):
            return httpx.Response(200, json=_issue(assignee=None))
        if request.method == "PUT":
            assert json.loads(request.content) == {"accountId": "bot-account"}
            return httpx.Response(204)
        if request.method == "GET" and "transitions" in str(request.url):
            return httpx.Response(
                200, json={"transitions": [{"id": "21", "to": {"name": "In Progress"}}, {"id": "31", "to": {"name": "Done"}}]}
            )
        if request.method == "POST" and "transitions" in str(request.url):
            assert json.loads(request.content) == {"transition": {"id": "21"}}
            return httpx.Response(204)
        raise AssertionError(f"unexpected call {request.method} {request.url}")

    from ticketbot.core.workitem import WorkItem

    source = JiraSource(_cfg(account_id="bot-account", in_progress_status="In Progress"), client=_client(handler))
    result = source.claim(WorkItem(id="ENG-1", title="T", external_id="ENG-1"))

    assert result is True
    methods = [m for m, _ in calls]
    assert methods == ["GET", "PUT", "GET", "POST"]


def test_claim_returns_false_when_already_assigned_to_someone_else():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_issue(assignee={"displayName": "Someone Else", "accountId": "other-account"}))
        raise AssertionError("claim() must not PUT/transition when already assigned to someone else")

    from ticketbot.core.workitem import WorkItem

    source = JiraSource(_cfg(account_id="bot-account"), client=_client(handler))
    result = source.claim(WorkItem(id="ENG-1", title="T", external_id="ENG-1"))
    assert result is False


def test_claim_without_account_id_skips_assign_and_warns(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "transitions" not in str(request.url):
            return httpx.Response(200, json=_issue(assignee=None))
        if request.method == "PUT":
            raise AssertionError("must not PUT assignee when account_id is unset")
        if request.method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "21", "to": {"name": "In Progress"}}]})
        if request.method == "POST":
            return httpx.Response(204)
        raise AssertionError("unexpected")

    from ticketbot.core.workitem import WorkItem

    source = JiraSource(_cfg(), client=_client(handler))
    result = source.claim(WorkItem(id="ENG-1", title="T", external_id="ENG-1"))
    assert result is True


def test_claim_missing_transition_is_a_warning_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "transitions" not in str(request.url):
            return httpx.Response(200, json=_issue(assignee=None))
        if request.method == "PUT":
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "99", "to": {"name": "Blocked"}}]})
        raise AssertionError("must not POST a transition when none matches")

    from ticketbot.core.workitem import WorkItem

    source = JiraSource(_cfg(account_id="bot-account", in_progress_status="In Progress"), client=_client(handler))
    result = source.claim(WorkItem(id="ENG-1", title="T", external_id="ENG-1"))
    assert result is True  # still claimed (assigned); just not transitioned


# ---- error handling / credential redaction ---------------------------------------


def test_401_raises_source_error_without_leaking_token_or_email():
    email = "secret-bot@acme.com"
    token = "supersecrettoken1234"
    leaky_body = f"unauthorized for {email} with token {token}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=leaky_body)

    source = JiraSource(_cfg(email=email, token=token), client=_client(handler))
    with pytest.raises(SourceError) as exc_info:
        source.fetch("ENG-1")

    message = str(exc_info.value)
    assert token not in message
    assert email not in message


def test_describe_includes_host():
    source = JiraSource(_cfg(), client=_client(lambda r: httpx.Response(200, json={})))
    assert source.describe() == "Jira (acme.atlassian.net)"


# ---- download_attachment() ---------------------------------------------------------


def test_download_attachment_writes_inside_dest_dir_and_caps_size(tmp_path):
    payload = b"x" * 100
    issue = _issue(
        attachment=[{"filename": "log.txt", "mimeType": "text/plain", "content": f"{BASE_URL}/secure/attachment/9/log.txt"}]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    source = JiraSource(_cfg(), client=_client(handler))
    from ticketbot.core.workitem import WorkItem

    item = WorkItem(id="ENG-1", title="T", external_id="ENG-1", raw=issue)
    attachment = source.download_attachment(item, "log.txt", tmp_path)

    assert attachment.path is not None
    assert attachment.path.read_bytes() == payload
    assert attachment.path.parent == tmp_path.resolve()


def test_download_attachment_enforces_max_bytes(tmp_path):
    issue = _issue(
        attachment=[{"filename": "huge.bin", "mimeType": "application/octet-stream", "content": f"{BASE_URL}/secure/attachment/9/huge.bin"}]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 1000)

    source = JiraSource(_cfg(), client=_client(handler))
    from ticketbot.core.workitem import WorkItem

    item = WorkItem(id="ENG-1", title="T", external_id="ENG-1", raw=issue)

    with pytest.raises(SourceError):
        source.download_attachment(item, "huge.bin", tmp_path, max_bytes=100)

    # no truncated/oversized partial file left behind
    assert not (tmp_path / "huge.bin").exists()


def test_download_attachment_rejects_path_traversal_filename(tmp_path):
    issue = _issue(
        attachment=[
            {
                "filename": "../../evil.txt",
                "mimeType": "text/plain",
                "content": f"{BASE_URL}/secure/attachment/9/evil.txt",
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"pwned")

    source = JiraSource(_cfg(), client=_client(handler))
    from ticketbot.core.workitem import WorkItem

    item = WorkItem(id="ENG-1", title="T", external_id="ENG-1", raw=issue)
    dest_dir = tmp_path / "run" / "attachments"

    attachment = source.download_attachment(item, "../../evil.txt", dest_dir)
    # the traversal must be neutralised: the file lands inside dest_dir, never above it
    assert attachment.path.parent == dest_dir.resolve()
    assert not (tmp_path / "evil.txt").exists()


def test_download_attachment_unknown_filename_raises(tmp_path):
    from ticketbot.core.workitem import WorkItem

    source = JiraSource(_cfg(), client=_client(lambda r: httpx.Response(200)))
    item = WorkItem(id="ENG-1", title="T", external_id="ENG-1", raw=_issue())
    with pytest.raises(SourceError):
        source.download_attachment(item, "nope.txt", tmp_path)
