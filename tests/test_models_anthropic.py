"""No network: every test injects a stub client whose `.messages.stream(...)`
returns a context manager with `get_final_message()` — nothing here ever talks to
the real Anthropic API. `anthropic`'s own exception/sentinel types are used as-is
(they're plain, importable classes) so the tests exercise the real error hierarchy.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import anthropic as real_anthropic
import httpx2
import pytest

import ticketbot.models.anthropic as anthropic_provider_module
from ticketbot.config.schema import AdapterConfig
from ticketbot.models.anthropic import AnthropicProvider
from ticketbot.models.base import Msg, ProviderError, ProviderRefusal, ToolResultBlock


def _cfg(**opts) -> AdapterConfig:
    return AdapterConfig(type="anthropic", **opts)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id: str, name: str, input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def _response(content, stop_reason="end_turn", stop_details=None, usage=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, stop_details=stop_details, usage=usage or _usage()
    )


class _StubStreamCM:
    """Mimics `anthropic`'s `MessageStreamManager`: a context manager around a
    stream object with `.get_final_message()`."""

    def __init__(self, final_message=None, exception: Exception | None = None):
        self._final_message = final_message
        self._exception = exception

    def __enter__(self):
        if self._exception is not None:
            raise self._exception
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_final_message(self):
        return self._final_message


class _StubMessages:
    def __init__(self, response=None, exception: Exception | None = None):
        self._response = response
        self._exception = exception
        self.stream_calls: list[dict] = []
        self.create_calls: list[dict] = []

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return _StubStreamCM(self._response, self._exception)

    def create(self, **kwargs):  # pragma: no cover - must never be called
        self.create_calls.append(kwargs)
        raise AssertionError("AnthropicProvider.complete() must use .stream(), not .create()")


class _StubClient:
    def __init__(self, response=None, exception: Exception | None = None):
        self.messages = _StubMessages(response, exception)


def _provider(response=None, exception=None, **cfg_opts) -> tuple[AnthropicProvider, _StubClient]:
    client = _StubClient(response=response, exception=exception)
    provider = AnthropicProvider(_cfg(**cfg_opts), client=client)
    return provider, client


def _http_error(status_code: int, cls=real_anthropic.APIStatusError, **headers) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status_code, request=request, headers=headers)
    return cls(f"http {status_code}", response=response, body=None)


# ---- request shape ----------------------------------------------------------


def test_uses_stream_not_create():
    provider, client = _provider(response=_response([_text_block("hi")]))
    provider.complete(system="", messages=[])

    assert len(client.messages.stream_calls) == 1
    assert client.messages.create_calls == []


def test_thinking_is_adaptive_and_output_config_carries_effort():
    provider, client = _provider(response=_response([_text_block("hi")]), effort="xhigh")
    provider.complete(system="", messages=[])

    kwargs = client.messages.stream_calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "xhigh"}


def test_display_key_added_only_when_configured():
    provider, client = _provider(response=_response([_text_block("hi")]), display="summarized")
    provider.complete(system="", messages=[])
    assert client.messages.stream_calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_display_key_omitted_by_default():
    provider, client = _provider(response=_response([_text_block("hi")]))
    provider.complete(system="", messages=[])
    assert client.messages.stream_calls[0]["thinking"] == {"type": "adaptive"}


def test_no_forbidden_sampling_or_budget_kwargs():
    provider, client = _provider(response=_response([_text_block("hi")]))
    provider.complete(system="", messages=[])

    kwargs = client.messages.stream_calls[0]
    for forbidden in ("budget_tokens", "temperature", "top_p", "top_k"):
        assert forbidden not in kwargs


def test_empty_system_omitted_via_not_given():
    provider, client = _provider(response=_response([_text_block("hi")]))
    provider.complete(system="", messages=[])
    assert client.messages.stream_calls[0]["system"] is real_anthropic.NOT_GIVEN


def test_no_tools_omitted_via_not_given():
    provider, client = _provider(response=_response([_text_block("hi")]))
    provider.complete(system="sys", messages=[])
    assert client.messages.stream_calls[0]["tools"] is real_anthropic.NOT_GIVEN


def test_max_tokens_defaults_and_override():
    provider, client = _provider(response=_response([_text_block("hi")]))
    provider.complete(system="", messages=[])
    assert client.messages.stream_calls[0]["max_tokens"] == 32000

    provider.complete(system="", messages=[], max_tokens=555)
    assert client.messages.stream_calls[1]["max_tokens"] == 555


# ---- message translation -----------------------------------------------------


def test_native_provider_anthropic_message_replayed_verbatim():
    provider, client = _provider(response=_response([_text_block("hi")]))
    native_blocks = [{"type": "thinking", "thinking": "...", "signature": "sig"}]
    msg = Msg(role="assistant", content="ignored", native=native_blocks, native_provider="anthropic")

    provider.complete(system="", messages=[msg])

    assert client.messages.stream_calls[0]["messages"][0] == {"role": "assistant", "content": native_blocks}


def test_native_message_from_a_different_provider_is_not_replayed():
    provider, client = _provider(response=_response([_text_block("hi")]))
    msg = Msg(role="assistant", content="plain text", native=["something"], native_provider="openai_compat")

    provider.complete(system="", messages=[msg])

    assert client.messages.stream_calls[0]["messages"][0] == {"role": "assistant", "content": "plain text"}


def test_tool_result_blocks_become_one_user_message_with_n_tool_result_blocks():
    provider, client = _provider(response=_response([_text_block("hi")]))
    msg = Msg(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="t1", content="ok"),
            ToolResultBlock(tool_use_id="t2", content="boom", is_error=True),
        ],
    )

    provider.complete(system="", messages=[msg])

    sent = client.messages.stream_calls[0]["messages"][0]
    assert sent["role"] == "user"
    assert sent["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "boom", "is_error": True},
    ]


# ---- response translation -----------------------------------------------------


def test_text_is_concatenated_and_stripped():
    provider, _ = _provider(response=_response([_text_block("  hello "), _text_block("world  ")]))
    result = provider.complete(system="", messages=[])
    assert result.text == "hello world"


def test_tool_use_blocks_become_tool_calls():
    provider, _ = _provider(
        response=_response([_tool_use_block("toolu_1", "fs.read", {"path": "a.py"})], stop_reason="tool_use")
    )
    result = provider.complete(system="", messages=[])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "toolu_1"
    assert result.tool_calls[0].name == "fs.read"
    assert result.tool_calls[0].input == {"path": "a.py"}
    assert result.stop_reason == "tool_use"


def test_usage_and_cost_are_populated():
    provider, _ = _provider(
        response=_response(
            [_text_block("hi")],
            usage=_usage(input_tokens=1000, output_tokens=2000, cache_read_input_tokens=500),
        )
    )
    result = provider.complete(system="", messages=[])
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 2000
    assert result.usage.cache_read_tokens == 500
    assert result.usage.cost_usd > 0


def test_native_carries_the_raw_content_list():
    content = [_text_block("hi")]
    provider, _ = _provider(response=_response(content))
    result = provider.complete(system="", messages=[])
    assert result.native is content


def test_refusal_stop_reason_raises_provider_refusal_with_category():
    stop_details = SimpleNamespace(category="cyber", explanation=None)
    provider, _ = _provider(response=_response([], stop_reason="refusal", stop_details=stop_details))

    with pytest.raises(ProviderRefusal) as exc_info:
        provider.complete(system="", messages=[])

    assert exc_info.value.category == "cyber"


def test_refusal_without_stop_details_has_none_category():
    provider, _ = _provider(response=_response([], stop_reason="refusal", stop_details=None))

    with pytest.raises(ProviderRefusal) as exc_info:
        provider.complete(system="", messages=[])

    assert exc_info.value.category is None


# ---- error mapping -------------------------------------------------------------


def test_rate_limit_error_surfaces_as_provider_error_with_retry_after():
    err = _http_error(429, cls=real_anthropic.RateLimitError, **{"retry-after": "7"})
    provider, _ = _provider(exception=err)

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(system="", messages=[])
    assert "7" in str(exc_info.value)


def test_authentication_error_surfaces_as_provider_error():
    err = _http_error(401, cls=real_anthropic.AuthenticationError)
    provider, _ = _provider(exception=err)
    with pytest.raises(ProviderError):
        provider.complete(system="", messages=[])


def test_permission_denied_error_surfaces_as_provider_error():
    err = _http_error(403, cls=real_anthropic.PermissionDeniedError)
    provider, _ = _provider(exception=err)
    with pytest.raises(ProviderError):
        provider.complete(system="", messages=[])


def test_not_found_error_surfaces_as_provider_error():
    err = _http_error(404, cls=real_anthropic.NotFoundError)
    provider, _ = _provider(exception=err)
    with pytest.raises(ProviderError):
        provider.complete(system="", messages=[])


def test_generic_api_status_error_surfaces_as_provider_error():
    err = _http_error(500, cls=real_anthropic.APIStatusError)
    provider, _ = _provider(exception=err)
    with pytest.raises(ProviderError):
        provider.complete(system="", messages=[])


def test_api_connection_error_surfaces_as_provider_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = real_anthropic.APIConnectionError(request=request)
    provider, _ = _provider(exception=err)
    with pytest.raises(ProviderError):
        provider.complete(system="", messages=[])


def test_api_timeout_error_surfaces_as_provider_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = real_anthropic.APITimeoutError(request=request)
    provider, _ = _provider(exception=err)
    with pytest.raises(ProviderError):
        provider.complete(system="", messages=[])


# ---- describe / config --------------------------------------------------------


def test_describe_default_model_and_effort():
    provider = AnthropicProvider(_cfg(), client=_StubClient())
    assert provider.describe() == "Claude Opus 5 (claude-opus-5) effort=high"


def test_describe_with_configured_effort():
    provider = AnthropicProvider(_cfg(effort="xhigh"), client=_StubClient())
    assert provider.describe() == "Claude Opus 5 (claude-opus-5) effort=xhigh"


def test_provider_id_is_anthropic():
    assert AnthropicProvider.provider_id == "anthropic"


def test_api_key_is_registered_as_a_secret():
    # A value that matches none of redact.py's known key-shape patterns, so the
    # only way it gets scrubbed is via the explicit register_secret() call.
    from ticketbot.config.redact import redact

    literal_key = "totally-custom-anthropic-secret-9999"
    provider = AnthropicProvider(_cfg(api_key=literal_key), client=_StubClient())
    assert provider.api_key == literal_key
    assert literal_key not in redact(f"key={literal_key}")


def test_api_key_env_ref_is_expanded(monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-fromenv1234567890")
    provider = AnthropicProvider(_cfg(api_key="${TEST_ANTHROPIC_KEY}"), client=_StubClient())
    assert provider.api_key == "sk-ant-fromenv1234567890"


# ---- source-level guardrails ---------------------------------------------------


def test_source_has_no_budget_tokens():
    source = Path(anthropic_provider_module.__file__).read_text(encoding="utf-8")
    assert "budget_tokens" not in source


def test_source_does_not_import_httpx():
    source = Path(anthropic_provider_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\bhttpx\b", source)


def test_module_importable_without_touching_the_network():
    # Importing the module (already done at collection time via the top-level
    # `import ticketbot.models.anthropic`) must not construct a client or make a
    # request; the only proof available at this layer is that it succeeded and
    # `AnthropicProvider` is a plain class.
    assert hasattr(anthropic_provider_module, "AnthropicProvider")
