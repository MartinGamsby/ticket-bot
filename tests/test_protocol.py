from ticketbot.engine.protocol import (
    has_question,
    parse_defers,
    parse_question,
    strip_protocol,
)


def test_question_block_captures_marker_line_and_everything_after():
    text = "Some report text.\n\nQUESTION: which database should we target?\nOption A or B?"
    assert parse_question(text) == (
        "QUESTION: which database should we target?\nOption A or B?"
    )


def test_only_first_question_line_starts_the_block():
    text = "QUESTION: first one\nmore context\nQUESTION: second one is just content"
    assert parse_question(text) == (
        "QUESTION: first one\nmore context\nQUESTION: second one is just content"
    )


def test_question_inside_fenced_code_block_is_ignored():
    text = (
        "Report body.\n"
        "```\n"
        "QUESTION: this is example output, not a real question\n"
        "```\n"
        "No real question here."
    )
    assert parse_question(text) is None


def test_question_after_a_fence_is_still_detected():
    text = (
        "```\n"
        "some code\n"
        "```\n"
        "QUESTION: real question after the fence\n"
    )
    assert parse_question(text) == "QUESTION: real question after the fence"


def test_multiple_defer_lines_returned_in_order():
    text = (
        "Review notes.\n"
        "DEFER: tighten input validation on the webhook handler\n"
        "Some other line.\n"
        "DEFER: add a regression test for the race condition\n"
    )
    assert parse_defers(text) == [
        "tighten input validation on the webhook handler",
        "add a regression test for the race condition",
    ]


def test_defer_inside_fenced_code_block_is_ignored():
    text = (
        "```\n"
        "DEFER: this is example output, not a real defer\n"
        "```\n"
        "DEFER: this one is real\n"
    )
    assert parse_defers(text) == ["this one is real"]


def test_empty_defer_payload_is_dropped():
    text = "DEFER:\nDEFER:   \nDEFER: a real one\n"
    assert parse_defers(text) == ["a real one"]


def test_strip_protocol_removes_question_but_keeps_defers():
    text = (
        "Review notes.\n"
        "DEFER: follow up later\n"
        "QUESTION: what should we do?\n"
        "more question context"
    )
    stripped = strip_protocol(text)
    assert "QUESTION:" not in stripped
    assert "DEFER: follow up later" in stripped
    assert "Review notes." in stripped


def test_strip_protocol_is_identity_when_no_question():
    text = "Review notes.\nDEFER: follow up later\n"
    assert strip_protocol(text) == text


def test_text_with_neither_marker_returns_none_and_empty_list():
    text = "Just a plain report with no markers at all."
    assert parse_question(text) is None
    assert parse_defers(text) == []


def test_has_question_true_and_false():
    assert has_question("QUESTION: pick one") is True
    assert has_question("no markers here") is False
