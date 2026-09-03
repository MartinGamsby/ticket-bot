"""`FileSink` -- no network, just the filesystem under a `tmp_path` run dir."""

from __future__ import annotations

from ticketbot.adapters.sinks.file import FileSink
from ticketbot.config.schema import AdapterConfig
from ticketbot.core.workitem import Attachment, WorkItem


def _cfg(**opts) -> AdapterConfig:
    return AdapterConfig(type="file", **opts)


def _item(key: str = "ENG-1") -> WorkItem:
    return WorkItem(id=key, title="Some ticket", external_id=key)


def test_comment_appends_to_ticket_comment_md(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(_item(), "first comment")
    text = (tmp_path / "ticket_comment.md").read_text(encoding="utf-8")
    assert text == "first comment"


def test_second_comment_is_separated_by_triple_dash(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(_item(), "first")
    sink.comment(_item(), "second")
    text = (tmp_path / "ticket_comment.md").read_text(encoding="utf-8")
    assert text == "first\n\n---\n\nsecond"


def test_transition_appends_to_result_md(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.transition(_item(), "In Review")
    text = (tmp_path / "result.md").read_text(encoding="utf-8")
    assert "- transition -> In Review" in text


def test_unassign_appends_to_result_md(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.unassign(_item())
    text = (tmp_path / "result.md").read_text(encoding="utf-8")
    assert "- unassigned" in text


def test_link_appends_markdown_link_to_result_md(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.link(_item(), "https://github.com/acme/app/pull/42", "PR #42")
    text = (tmp_path / "result.md").read_text(encoding="utf-8")
    assert "- link: [PR #42](https://github.com/acme/app/pull/42)" in text


def test_secret_in_comment_markdown_is_redacted_on_disk(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(_item(), "here is a key sk-ant-abcdefghijklmnopqrstuvwx")
    text = (tmp_path / "ticket_comment.md").read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    assert "***REDACTED***" in text


def test_attachments_are_copied_into_attachments_dir(tmp_path):
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(_item(), "see screenshot", attachments=[Attachment(filename="shot.png", path=src)])

    dest = tmp_path / "attachments" / "shot.png"
    assert dest.read_bytes() == src.read_bytes()

    result_text = (tmp_path / "result.md").read_text(encoding="utf-8")
    assert "shot.png" in result_text


def test_attachment_with_in_memory_data_is_written(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(_item(), "inline data", attachments=[Attachment(filename="note.txt", data=b"hello world")])
    dest = tmp_path / "attachments" / "note.txt"
    assert dest.read_bytes() == b"hello world"


def test_malicious_attachment_filename_cannot_escape_attachments_dir(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(
        _item(),
        "path traversal attempt",
        attachments=[Attachment(filename="../../outside-secret.txt", data=b"should not land here")],
    )

    assert not outside.exists()
    # the traversal is stripped to a basename and lands safely inside attachments/
    landed = tmp_path / "attachments" / "outside-secret.txt"
    assert landed.exists()
    assert landed.read_bytes() == b"should not land here"


def test_absolute_windows_style_attachment_path_is_reduced_to_basename(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    sink.comment(
        _item(),
        "abs path attempt",
        attachments=[Attachment(filename="C:\\Windows\\System32\\evil.txt", data=b"x")],
    )
    landed = tmp_path / "attachments" / "evil.txt"
    assert landed.exists()


def test_dir_defaults_to_run_dir_when_not_configured(tmp_path):
    sink = FileSink(_cfg(), run_dir=tmp_path)
    assert sink.dir == tmp_path.resolve()


def test_configured_dir_overrides_run_dir(tmp_path):
    explicit = tmp_path / "explicit-dir"
    sink = FileSink(_cfg(dir=str(explicit)), run_dir=tmp_path / "unused")
    assert sink.dir == explicit.resolve()
    assert explicit.is_dir()


def test_describe(tmp_path):
    assert FileSink(_cfg(), run_dir=tmp_path).describe() == "file"
