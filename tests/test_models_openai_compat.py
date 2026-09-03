"""No network: every test drives `OpenAICompatProvider` with an injected
`httpx.Client` wired to `httpx.MockTransport`, and asserts the exact request the
provider sent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ticketbot.config.schema import AdapterConfig
from ticketbot.models.base import Msg, ProviderError, ToolCall, ToolDef, ToolResultBlock, ToolUseBlock
from ticketbot.models.openai_compat import OpenAICompatProvider


def _cfg(**opts) -> AdapterConfig:
    opts.setdefault("base_url", "https://peer.example.com")
    opts.setdefault("model", "gpt-5")
    return AdapterConfig(type="openai_compat", **opts)


def _client_and_capture(response_json: dict | None = None, status_code: int = 200, text: str | None = None):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["json"] = json.loads(request.content) if request.content else None
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=response_json or {"choices": [{"message": {}}]})

    return httpx.Client(transport=httpx.MockTransport(handler)), captured


def _basic_response(**overrides) -> dict:
    message = {"content": "hello", "tool_calls": None}
    message.update(overrides.pop("message", {}))
    choice = {"message": message, "finish_reason": overrides.pop("finish_reason", "stop")}
    body = {"choices": [choice], "usage": overrides.pop("usage", {"prompt_tokens": 1, "completion_tokens": 2})}
    body.update(overrides)
    return body


# ---- request shape --------------------------------------------------------------


def test_request_url_ends_in_chat_completions():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(base_url="https://peer.example.com/v1/"), client)
    provider.complete(system="", messages=[])
    assert str(captured["request"].url) == "https://peer.example.com/v1/chat/completions"


def test_authorization_bearer_header_sent_when_api_key_configured():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(api_key="peer-secret-key-123"), client)
    provider.complete(system="", messages=[])
    assert captured["request"].headers["authorization"] == "Bearer peer-secret-key-123"


def test_authorization_header_omitted_when_no_api_key():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(), client)
    provider.complete(system="", messages=[])
    assert "authorization" not in captured["request"].headers


def test_api_key_env_ref_is_expanded(monkeypatch):
    monkeypatch.setenv("TEST_PEER_KEY", "expanded-peer-key-789")
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(api_key="${TEST_PEER_KEY}"), client)
    provider.complete(system="", messages=[])
    assert captured["request"].headers["authorization"] == "Bearer expanded-peer-key-789"


def test_system_becomes_first_message_when_non_empty():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(), client)
    provider.complete(system="You are helpful", messages=[Msg(role="user", content="hi")])

    messages = captured["json"]["messages"]
    assert messages[0] == {"role": "system", "content": "You are helpful"}
    assert messages[1] == {"role": "user", "content": "hi"}


def test_empty_system_is_omitted():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(), client)
    provider.complete(system="", messages=[Msg(role="user", content="hi")])

    messages = captured["json"]["messages"]
    assert messages == [{"role": "user", "content": "hi"}]


def test_max_tokens_field_by_default():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(max_tokens=1234), client)
    provider.complete(system="", messages=[])
    assert captured["json"]["max_tokens"] == 1234
    assert "max_completion_tokens" not in captured["json"]


def test_max_completion_tokens_field_when_configured():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(max_tokens=1234, max_completion_tokens_field=True), client)
    provider.complete(system="", messages=[])
    assert captured["json"]["max_completion_tokens"] == 1234
    assert "max_tokens" not in captured["json"]


def test_extra_body_is_merged_last_and_can_override():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(max_tokens=1234, extra_body={"max_tokens": 999, "temperature": 0.2}), client)
    provider.complete(system="", messages=[])
    assert captured["json"]["max_tokens"] == 999
    assert captured["json"]["temperature"] == 0.2


def test_tool_definition_shape():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(), client)
    tool = ToolDef(
        name="fs.read",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    provider.complete(system="", messages=[], tools=[tool])

    assert captured["json"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "fs.read",
                "description": "Read a file",
                "parameters": tool.input_schema,
            },
        }
    ]


def test_tool_use_and_tool_result_blocks_translated():
    client, captured = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(), client)
    assistant_msg = Msg(
        role="assistant",
        content=[ToolUseBlock(id="call_1", name="fs.read", input={"path": "a.py"})],
    )
    result_msg = Msg(role="user", content=[ToolResultBlock(tool_use_id="call_1", content="file contents")])

    provider.complete(system="", messages=[assistant_msg, result_msg])

    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "fs.read", "arguments": '{"path": "a.py"}'}}
    ]
    assert messages[1] == {"role": "tool", "tool_call_id": "call_1", "content": "file contents"}


# ---- response translation --------------------------------------------------------


def test_tool_call_response_maps_to_toolcall_with_parsed_input():
    body = _basic_response(
        message={
            "content": None,
            "tool_calls": [
                {"id": "call_9", "type": "function", "function": {"name": "fs.read", "arguments": '{"path": "a.py"}'}}
            ],
        },
        finish_reason="tool_calls",
    )
    client, _ = _client_and_capture(body)
    provider = OpenAICompatProvider(_cfg(), client)

    result = provider.complete(system="", messages=[])

    assert result.tool_calls == [ToolCall(id="call_9", name="fs.read", input={"path": "a.py"})]
    assert result.stop_reason == "tool_use"


def test_malformed_arguments_yield_empty_dict_and_warn():
    body = _basic_response(
        message={
            "content": None,
            "tool_calls": [
                {"id": "call_9", "type": "function", "function": {"name": "fs.read", "arguments": "{not json"}}
            ],
        },
        finish_reason="tool_calls",
    )
    client, _ = _client_and_capture(body)
    provider = OpenAICompatProvider(_cfg(), client)

    with pytest.warns(UserWarning):
        result = provider.complete(system="", messages=[])

    assert result.tool_calls == [ToolCall(id="call_9", name="fs.read", input={})]


@pytest.mark.parametrize(
    "finish_reason,expected_stop_reason",
    [("tool_calls", "tool_use"), ("length", "max_tokens"), ("stop", "end_turn"), (None, "end_turn")],
)
def test_finish_reason_mapping(finish_reason, expected_stop_reason):
    body = _basic_response(finish_reason=finish_reason)
    client, _ = _client_and_capture(body)
    provider = OpenAICompatProvider(_cfg(), client)

    result = provider.complete(system="", messages=[])
    assert result.stop_reason == expected_stop_reason


def test_usage_mapping_and_zero_cost():
    body = _basic_response(usage={"prompt_tokens": 11, "completion_tokens": 22})
    client, _ = _client_and_capture(body)
    provider = OpenAICompatProvider(_cfg(), client)

    result = provider.complete(system="", messages=[])
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 22
    assert result.usage.cost_usd == 0.0


def test_text_content_passed_through():
    body = _basic_response(message={"content": "the answer"})
    client, _ = _client_and_capture(body)
    provider = OpenAICompatProvider(_cfg(), client)

    result = provider.complete(system="", messages=[])
    assert result.text == "the answer"


# ---- errors -----------------------------------------------------------------------


def test_non_2xx_raises_provider_error_without_leaking_the_api_key():
    api_key = "super-secret-peer-key-000"
    leaky_body = f"upstream failed; saw header Authorization: Bearer {api_key}"
    client, _ = _client_and_capture(status_code=500, text=leaky_body)
    provider = OpenAICompatProvider(_cfg(api_key=api_key), client)

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(system="", messages=[])

    message = str(exc_info.value)
    assert "500" in message
    assert api_key not in message


# ---- describe -----------------------------------------------------------------------


def test_describe():
    client, _ = _client_and_capture(_basic_response())
    provider = OpenAICompatProvider(_cfg(model="gpt-5"), client)
    assert provider.describe() == "gpt-5 (openai_compat)"


def test_provider_id_is_openai_compat():
    assert OpenAICompatProvider.provider_id == "openai_compat"
