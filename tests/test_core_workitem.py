from datetime import datetime, timezone
from pathlib import Path

from ticketbot.core.workitem import Ambiguity, Attachment, Comment, Size, WorkItem, slugify

_RESERVED = set('<>:"/\\|?*')


def _make_item(**overrides) -> WorkItem:
    kwargs = dict(id="task-1", title="Add a /health endpoint")
    kwargs.update(overrides)
    return WorkItem(**kwargs)


def test_slug_lowercases_and_collapses_punctuation():
    item = _make_item(title="Login TIMES out on SSO!!")
    assert item.slug() == "login-times-out-on-sso"


def test_slug_empty_title_falls_back_to_task():
    item = _make_item(title="")
    assert item.slug() == "task"


def test_slug_whitespace_only_title_falls_back_to_task():
    item = _make_item(title="   ")
    assert item.slug() == "task"


def test_slug_rejects_dotdot_and_path_separators():
    item = _make_item(title="../../etc/passwd")
    slug = item.slug()
    assert ".." not in slug
    assert "/" not in slug
    assert "\\" not in slug
    assert not slug.startswith("-")
    assert not slug.endswith("-")


def test_slug_strips_windows_reserved_characters():
    item = _make_item(title='con<>:"/\\|?*fig')
    slug = item.slug()
    assert not (_RESERVED & set(slug))


def test_slug_handles_weird_unicode():
    item = _make_item(title="Café ☕ — login 时区 issue")
    slug = item.slug()
    assert slug != ""
    assert all(ch.isascii() for ch in slug)
    assert not slug.startswith("-")
    assert not slug.endswith("-")


def test_slug_fully_non_ascii_title_falls_back_to_task():
    item = _make_item(title="日本語のタイトル")
    assert item.slug() == "task"


def test_slug_truncates_on_word_boundary_for_long_title():
    item = _make_item(title=("word " * 80).strip())
    slug = item.slug(max_len=40)
    assert len(slug) <= 40
    assert not slug.endswith("-")
    # truncation must not cut a word in half: every char is from the source words
    assert set(slug.replace("-", "")) <= set("word")


def test_slug_max_len_is_respected_for_a_300_char_title():
    item = _make_item(title="x" * 300)
    slug = item.slug(max_len=40)
    assert len(slug) <= 40


def test_slugify_helper_matches_workitem_slug():
    assert slugify("ENG-1842") == "eng-1842"


def test_size_boundaries():
    assert _make_item(story_points=None).size() == Size.XS
    assert _make_item(story_points=1).size() == Size.XS
    assert _make_item(story_points=1.5).size() == Size.S
    assert _make_item(story_points=2).size() == Size.S
    assert _make_item(story_points=2.5).size() == Size.M
    assert _make_item(story_points=5).size() == Size.M
    assert _make_item(story_points=5.5).size() == Size.L
    assert _make_item(story_points=8).size() == Size.L
    assert _make_item(story_points=8.5).size() == Size.XL
    assert _make_item(story_points=13).size() == Size.XL


def test_key_prefers_external_id_over_id():
    item = _make_item(id="internal-slug", external_id="ENG-1842")
    assert item.key == "ENG-1842"


def test_key_falls_back_to_id_when_no_external_id():
    item = _make_item(id="internal-slug", external_id=None)
    assert item.key == "internal-slug"


def test_to_dict_from_dict_round_trip_preserves_comments_attachments_ambiguity():
    created = datetime(2026, 9, 1, 14, 43, tzinfo=timezone.utc)
    item = WorkItem(
        id="eng-1842",
        title="Login times out on SSO",
        description="Users see a spinner forever.",
        external_id="ENG-1842",
        issue_type="Bug",
        story_points=5,
        labels=["agent", "sso"],
        acceptance="Login completes within 5s.",
        status="In Progress",
        assignee="martin",
        url="https://example.atlassian.net/browse/ENG-1842",
        comments=[
            Comment(author="alice", body="Repro'd on staging.", created_at=created, id="c1"),
            Comment(author="bob", body="Assigning to backend."),
        ],
        attachments=[
            Attachment(filename="trace.txt", content_type="text/plain", data=b"hello world"),
            Attachment(filename="local.png", path=Path("screenshots/local.png")),
        ],
        ambiguity=Ambiguity.MEDIUM,
        source_ref="jql:assignee=me",
        raw={"fields": {"summary": "Login times out on SSO"}},
    )

    restored = WorkItem.from_dict(item.to_dict())

    assert restored.id == item.id
    assert restored.title == item.title
    assert restored.external_id == item.external_id
    assert restored.story_points == item.story_points
    assert restored.labels == item.labels
    assert restored.ambiguity == Ambiguity.MEDIUM
    assert len(restored.comments) == 2
    assert restored.comments[0].author == "alice"
    assert restored.comments[0].created_at == created
    assert restored.comments[1].created_at is None
    assert len(restored.attachments) == 2
    assert restored.attachments[0].read_bytes() == b"hello world"
    assert restored.attachments[1].path == Path("screenshots/local.png")
    assert restored.raw == item.raw


def test_to_dict_is_json_safe():
    import json

    item = WorkItem(
        id="x",
        title="t",
        comments=[Comment(author="a", body="b", created_at=datetime(2026, 1, 1))],
        attachments=[Attachment(filename="f.bin", data=b"\x00\x01\x02")],
    )
    # must not raise
    json.dumps(item.to_dict())


def test_as_context_returns_plain_strings_for_enums():
    item = _make_item(story_points=3, ambiguity=Ambiguity.HIGH, issue_type="Bug")
    ctx = item.as_context()

    assert ctx["ambiguity"] == "high"
    assert isinstance(ctx["ambiguity"], str)
    assert ctx["size"] == "m"
    assert isinstance(ctx["size"], str)


def test_as_context_ambiguity_none_when_unset():
    item = _make_item()
    ctx = item.as_context()
    assert ctx["ambiguity"] is None


def test_as_context_has_expected_keys():
    item = _make_item(external_id="ENG-1", comments=[Comment(author="a", body="b")])
    ctx = item.as_context()
    expected_keys = {
        "key", "external_id", "title", "description", "issue_type", "story_points",
        "labels", "acceptance", "status", "ambiguity", "size", "url", "comment_count",
    }
    assert set(ctx.keys()) == expected_keys
    assert ctx["comment_count"] == 1
    assert ctx["key"] == "ENG-1"


def test_attachment_read_bytes_prefers_data_over_path(tmp_path):
    file_path = tmp_path / "on-disk.txt"
    file_path.write_bytes(b"from disk")
    att = Attachment(filename="a.txt", data=b"from memory", path=file_path)
    assert att.read_bytes() == b"from memory"


def test_attachment_read_bytes_reads_from_path_when_no_data(tmp_path):
    file_path = tmp_path / "on-disk.txt"
    file_path.write_bytes(b"from disk")
    att = Attachment(filename="a.txt", path=file_path)
    assert att.read_bytes() == b"from disk"
