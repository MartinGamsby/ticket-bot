"""`MultiSink` (primary + `also:` fan-out, secondary failures logged not raised)
and `DryRunSink` (records instead of calling). No network -- driven entirely by
`tests.fakes.FakeSink`.
"""

from __future__ import annotations

import pytest

from ticketbot.adapters.sinks.base import DryRunSink, MultiSink, SinkError
from ticketbot.core.workitem import WorkItem
from tests.fakes import FakeSink


def _item() -> WorkItem:
    return WorkItem(id="ENG-1", title="T", external_id="ENG-1")


# ---- MultiSink --------------------------------------------------------------


def test_primary_is_called_first_and_receives_the_call():
    primary = FakeSink()
    secondary = FakeSink()
    multi = MultiSink(primary, [secondary])

    multi.comment(_item(), "hello", ())

    assert primary.comments == [("ENG-1", "hello", ())]
    assert secondary.comments == [("ENG-1", "hello", ())]


def test_secondary_raising_is_caught_and_does_not_stop_remaining_secondaries():
    primary = FakeSink()
    broken = FakeSink(fail_on={"comment"})
    healthy = FakeSink()
    multi = MultiSink(primary, [broken, healthy])

    multi.comment(_item(), "hello", ())  # must not raise

    assert primary.comments == [("ENG-1", "hello", ())]
    assert healthy.comments == [("ENG-1", "hello", ())]


def test_secondary_failure_is_reported_via_on_error():
    primary = FakeSink()
    broken = FakeSink(fail_on={"transition"})
    reports = []

    multi = MultiSink(primary, [broken], on_error=lambda sink, method, exc: reports.append((method, str(exc))))
    multi.transition(_item(), "In Review")

    assert len(reports) == 1
    assert reports[0][0] == "transition"


def test_primary_raising_propagates_and_secondary_is_not_called():
    primary = FakeSink(fail_on={"comment"})
    secondary = FakeSink()
    multi = MultiSink(primary, [secondary])

    with pytest.raises(SinkError):
        multi.comment(_item(), "hello", ())

    assert secondary.comments == []


def test_unassign_and_link_fan_out_too():
    primary = FakeSink()
    secondary = FakeSink()
    multi = MultiSink(primary, [secondary])

    multi.unassign(_item())
    multi.link(_item(), "https://x", "title")

    assert primary.unassigned == ["ENG-1"]
    assert secondary.unassigned == ["ENG-1"]
    assert primary.links == [("ENG-1", "https://x", "title")]
    assert secondary.links == [("ENG-1", "https://x", "title")]


def test_describe_lists_primary_and_secondaries():
    primary = FakeSink()
    secondary1 = FakeSink()
    secondary2 = FakeSink()
    multi = MultiSink(primary, [secondary1, secondary2])
    assert multi.describe() == "fake (+fake, fake)"


def test_describe_with_no_secondaries_is_just_the_primary():
    assert MultiSink(FakeSink(), []).describe() == "fake"


def test_close_closes_every_sink_even_if_one_fails():
    class BrokenClose(FakeSink):
        def close(self) -> None:
            raise RuntimeError("close boom")

    primary = FakeSink()
    broken = BrokenClose()
    healthy = FakeSink()
    multi = MultiSink(primary, [broken, healthy])

    multi.close()  # must not raise

    assert primary.closed is True
    assert healthy.closed is True


# ---- DryRunSink ---------------------------------------------------------------


def test_dry_run_records_instead_of_calling_and_inner_stays_untouched():
    inner = FakeSink()
    dry = DryRunSink(inner)

    dry.comment(_item(), "x" * 10, attachments=("a", "b"))
    dry.transition(_item(), "In Review")
    dry.unassign(_item())
    dry.link(_item(), "https://x", "title")

    assert inner.comments == []
    assert inner.transitions == []
    assert inner.unassigned == []
    assert inner.links == []

    assert len(dry.calls) == 4
    assert dry.calls[0] == "sink.comment fake ENG-1 (10 chars, 2 attachments)"
    assert dry.calls[1] == "sink.transition fake ENG-1 -> In Review"
    assert dry.calls[2] == "sink.unassign fake ENG-1"
    assert dry.calls[3] == "sink.link fake ENG-1 -> title (https://x)"


def test_dry_run_writes_to_log_path_when_given(tmp_path):
    log_path = tmp_path / "dry-run.log"
    dry = DryRunSink(FakeSink(), log_path=log_path)

    dry.transition(_item(), "In Review")

    text = log_path.read_text(encoding="utf-8")
    assert text == "sink.transition fake ENG-1 -> In Review\n"


def test_dry_run_describe_wraps_inner():
    assert DryRunSink(FakeSink()).describe() == "dry-run(fake)"
