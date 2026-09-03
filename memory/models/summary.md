# Models: the `ModelProvider` layer

`ticketbot/models/` is the ONLY place a model vendor SDK is imported. Three providers are registered:
`anthropic`, `openai_compat`, `fake`. A provider turns "system + messages [+ tools]" into
"text + tool calls + usage" through the provider-neutral types in `models/base.py`.

## Neutral types

`TextBlock`, `ToolUseBlock`, `ToolResultBlock` (union `Block`); `Msg(role, content, native,
native_provider)`; `ToolDef(name, description, input_schema)`; `ToolCall(id, name, input)`;
`Usage(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)` with `__add__`;
`ProviderMessage(text, tool_calls, stop_reason, usage, native, raw)`.

`Msg.native` / `native_provider` is the escape hatch that lets a provider replay ITS OWN previously
returned content verbatim — Anthropic thinking blocks must be echoed back unchanged on the same
model — without any other provider needing to know what that content looks like. A provider consults
`native` only when `native_provider` matches its own `provider_id`.

## `AnthropicProvider` — `models/anthropic.py`

`import anthropic` happens LAZILY inside functions, so importing this module succeeds without the SDK
installed; the registry only resolves it when a profile selects `type: anthropic`.

Request shape is fixed and NOT negotiable (it mirrors the `claude-api` reference):

```python
with client.messages.stream(
    model=self.model,
    max_tokens=max_tokens or self.max_tokens,
    system=system or anthropic.NOT_GIVEN,
    messages=[...],
    thinking={"type": "adaptive"},
    output_config={"effort": self.effort},   # effort goes HERE, never top-level
    tools=api_tools or anthropic.NOT_GIVEN,
) as stream:
    response = stream.get_final_message()
```

Always stream (a large `max_tokens` risks an HTTP timeout on a non-streaming call). Never a legacy
fixed thinking-token budget, never `temperature`/`top_p`/`top_k`, never an assistant prefill.

Options: `model` (default `claude-opus-5`), `effort` (default `high`), `max_tokens` (32000),
`display`, `api_key` (an `${ENV}` ref, expanded and registered), `base_url`, `timeout_s` (900),
`max_retries` (2). With no `api_key` in the profile the SDK does its own `ANTHROPIC_API_KEY` lookup.

Every SDK exception class is mapped to a `ProviderError` with a readable message (auth, permission,
not-found, rate limit with `retry-after`, status, timeout, connection). `stop_reason == "refusal"`
raises `ProviderRefusal`, reading `stop_details` with `getattr` so a response object without that
attribute still surfaces as a refusal rather than an `AttributeError`.

## `OpenAICompatProvider` — `models/openai_compat.py`

Raw `httpx` against `<base_url>/chat/completions` — deliberately NOT the `openai` package, so "any
base_url that speaks the OpenAI chat-completions shape" stays literally true with no extra
dependency. `client` is injectable for `httpx.MockTransport` tests.

One `Msg` can expand into SEVERAL OpenAI messages: an assistant turn with `content` and/or
`tool_calls`, followed by one `role: tool` message per `ToolResultBlock` (OpenAI has no multi-result
tool message). `finish_reason` maps `tool_calls -> tool_use`, `length -> max_tokens`. Malformed tool
arguments warn and become `{}` rather than raising. `cost_usd` is 0.0 — pricing for an arbitrary peer
endpoint is unknown. Options: `model`, `base_url`, `api_key`, `max_tokens` (16000), `timeout_s`,
`max_completion_tokens_field`, `extra_body`.

## `FakeModelProvider` — `models/fake.py`

Scripted, deterministic, no network. Entries are returned in order; when the script runs out the LAST
entry repeats, so an executor's tool loop terminates instead of hanging on `StopIteration`.
Constructible directly (`script=[...]`) or from an `AdapterConfig` `{type: fake, script: [...]}`
where an entry is a string or `{text, tool_calls: [{name, input}], stop_reason}`. It is a registered
provider, not just a test helper — a profile can select it.

## Pricing

`models/base.py: PRICING` maps model id to (input, output) USD per 1M tokens; cached reads bill at
10% of the input rate and cache writes at 125%. `estimate_cost` returns 0.0 for an unknown id.
`DISPLAY_NAMES` backs `display_name()` and therefore `describe()` and the banner. Keep both tables in
sync with the current model table when model ids change.
