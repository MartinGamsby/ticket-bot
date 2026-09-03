from ticketbot.config.schema import AdapterConfig
from ticketbot.models.base import ProviderMessage, ToolCall
from ticketbot.models.fake import FakeModelProvider


def test_script_entries_returned_in_order():
    provider = FakeModelProvider(script=["first", "second", "third"])

    assert provider.complete(system="", messages=[]).text == "first"
    assert provider.complete(system="", messages=[]).text == "second"
    assert provider.complete(system="", messages=[]).text == "third"


def test_last_entry_repeats_once_script_is_exhausted():
    provider = FakeModelProvider(script=["only"])

    assert provider.complete(system="", messages=[]).text == "only"
    assert provider.complete(system="", messages=[]).text == "only"
    assert provider.complete(system="", messages=[]).text == "only"


def test_plain_str_entry_becomes_end_turn_provider_message():
    provider = FakeModelProvider(script=["hi"])
    result = provider.complete(system="", messages=[])
    assert result == ProviderMessage(
        text="hi", tool_calls=[], stop_reason="end_turn", usage=result.usage
    )
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 10


def test_calls_records_every_complete_invocation():
    provider = FakeModelProvider(script=["a", "b"])
    provider.complete(system="sys1", messages=["m1"], tools=None, max_tokens=100)
    provider.complete(system="sys2", messages=["m2"], tools=["t"], max_tokens=200)

    assert provider.calls == [
        {"system": "sys1", "messages": ["m1"], "tools": None, "max_tokens": 100},
        {"system": "sys2", "messages": ["m2"], "tools": ["t"], "max_tokens": 200},
    ]


def test_tool_call_missing_id_gets_a_fresh_toolu_id():
    scripted = ProviderMessage(
        text="",
        tool_calls=[ToolCall(id="", name="fs.read", input={"path": "a.py"})],
        stop_reason="tool_use",
    )
    provider = FakeModelProvider(script=[scripted])

    result = provider.complete(system="", messages=[])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "toolu_0"
    assert result.tool_calls[0].name == "fs.read"


def test_tool_call_with_existing_id_is_left_alone():
    scripted = ProviderMessage(
        text="",
        tool_calls=[ToolCall(id="toolu_custom", name="fs.read", input={})],
        stop_reason="tool_use",
    )
    provider = FakeModelProvider(script=[scripted])

    result = provider.complete(system="", messages=[])

    assert result.tool_calls[0].id == "toolu_custom"


def test_fresh_tool_ids_increment_across_multiple_missing_calls():
    scripted = ProviderMessage(
        text="",
        tool_calls=[
            ToolCall(id="", name="fs.read", input={}),
            ToolCall(id="", name="fs.write", input={}),
        ],
        stop_reason="tool_use",
    )
    provider = FakeModelProvider(script=[scripted])

    result = provider.complete(system="", messages=[])

    assert [tc.id for tc in result.tool_calls] == ["toolu_0", "toolu_1"]


def test_describe_uses_name():
    provider = FakeModelProvider(script=["x"], name="my-fake")
    assert provider.describe() == "my-fake (fake)"


def test_default_name_is_fake_model():
    provider = FakeModelProvider(script=["x"])
    assert provider.describe() == "fake-model (fake)"


def test_empty_script_still_returns_an_end_turn_message():
    provider = FakeModelProvider(script=[])
    result = provider.complete(system="", messages=[])
    assert result.text == ""
    assert result.stop_reason == "end_turn"


def test_constructible_from_adapter_config_with_str_and_dict_script_entries():
    cfg = AdapterConfig(
        type="fake",
        script=[
            "plain text turn",
            {"text": "", "tool_calls": [{"name": "fs.read", "input": {"path": "a.py"}}]},
        ],
    )
    provider = FakeModelProvider(cfg)

    first = provider.complete(system="", messages=[])
    assert first.text == "plain text turn"
    assert first.stop_reason == "end_turn"

    second = provider.complete(system="", messages=[])
    assert second.stop_reason == "tool_use"
    assert second.tool_calls[0].name == "fs.read"
    assert second.tool_calls[0].input == {"path": "a.py"}
    assert second.tool_calls[0].id  # a fresh id was assigned


def test_adapter_config_script_entry_with_explicit_stop_reason():
    cfg = AdapterConfig(type="fake", script=[{"text": "done", "stop_reason": "max_tokens"}])
    provider = FakeModelProvider(cfg)

    result = provider.complete(system="", messages=[])
    assert result.text == "done"
    assert result.stop_reason == "max_tokens"


def test_explicit_script_kwarg_overrides_adapter_config_script():
    cfg = AdapterConfig(type="fake", script=["from config"])
    provider = FakeModelProvider(cfg, script=["from kwarg"])

    result = provider.complete(system="", messages=[])
    assert result.text == "from kwarg"


def test_provider_id_is_fake():
    assert FakeModelProvider.provider_id == "fake"
