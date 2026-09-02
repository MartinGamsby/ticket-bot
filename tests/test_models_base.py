from ticketbot.models.base import (
    TextBlock,
    ToolUseBlock,
    Usage,
    blocks_to_text,
    display_name,
    estimate_cost,
)


def test_usage_add_is_field_wise():
    a = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4, cost_usd=0.5)
    b = Usage(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40, cost_usd=1.5)

    total = a + b

    assert total == Usage(
        input_tokens=11, output_tokens=22, cache_read_tokens=33, cache_write_tokens=44, cost_usd=2.0
    )


def test_estimate_cost_claude_opus_5_one_million_in_and_out():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost("claude-opus-5", usage) == 30.0


def test_estimate_cost_unknown_model_is_zero():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost("some-model-nobody-heard-of", usage) == 0.0


def test_estimate_cost_cached_reads_bill_at_ten_percent_of_input_rate():
    usage = Usage(cache_read_tokens=1_000_000)
    # claude-opus-5 input rate is $5.00 / 1M tokens -> 10% = $0.50
    assert estimate_cost("claude-opus-5", usage) == 0.5


def test_estimate_cost_cache_writes_bill_at_125_percent_of_input_rate():
    usage = Usage(cache_write_tokens=1_000_000)
    # claude-opus-5 input rate is $5.00 / 1M tokens -> 125% = $6.25
    assert estimate_cost("claude-opus-5", usage) == 6.25


def test_estimate_cost_zero_usage_is_zero():
    assert estimate_cost("claude-opus-5", Usage()) == 0.0


def test_display_name_known_model():
    assert display_name("claude-opus-5") == "Claude Opus 5"


def test_display_name_falls_back_to_raw_id_for_unknown_model():
    assert display_name("some-unlisted-model") == "some-unlisted-model"


def test_blocks_to_text_concatenates_only_text_blocks():
    content = [
        TextBlock(text="hello "),
        ToolUseBlock(id="toolu_1", name="fs.read", input={"path": "a.py"}),
        TextBlock(text="world"),
    ]
    assert blocks_to_text(content) == "hello world"


def test_blocks_to_text_passes_through_plain_string():
    assert blocks_to_text("already text") == "already text"


def test_blocks_to_text_empty_list_is_empty_string():
    assert blocks_to_text([]) == ""
