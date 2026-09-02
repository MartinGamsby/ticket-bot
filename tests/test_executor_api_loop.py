"""`ApiLoopExecutor` -- the tool loop over a `ModelProvider`, exercised entirely
against `FakeModelProvider` (no network, no subprocess).
"""

from __future__ import annotations

from ticketbot.config.schema import AdapterConfig
from ticketbot.executors.api_loop import ApiLoopExecutor
from ticketbot.executors.base import ExecRequest
from ticketbot.models.base import ProviderMessage, ToolCall, Usage
from ticketbot.models.fake import FakeModelProvider

from tests.fakes import fake_provider, text_turn, tool_turn


def _dirs(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return ws, artifacts


def _cfg(**overrides) -> AdapterConfig:
    data = {"type": "api", "model": "test-model", "max_iterations": 40, "max_tokens": 1000}
    data.update(overrides)
    return AdapterConfig(**data)


def _req(ws, artifacts, *, prompt="do the thing", tools=None, **kw) -> ExecRequest:
    return ExecRequest(
        system="system prompt",
        prompt=prompt,
        workspace=ws,
        artifacts_dir=artifacts,
        tools=list(tools or []),
        **kw,
    )


class _CostlyProvider:
    """A minimal ModelProvider double that reports a real, nonzero cost per turn --
    FakeModelProvider always reports Usage with cost_usd=0.0, which can never trip
    a max_cost_usd cap, so this is needed for that one test.
    """

    provider_id = "fake"

    def __init__(self, cost_per_call: float) -> None:
        self.cost_per_call = cost_per_call
        self.calls = 0

    def describe(self) -> str:
        return "costly (fake)"

    def complete(self, *, system, messages, tools=None, max_tokens=None) -> ProviderMessage:
        self.calls += 1
        return ProviderMessage(
            text="",
            tool_calls=[ToolCall(f"t{self.calls}", "fs_read", {"path": "."})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=100, cost_usd=self.cost_per_call),
        )


def test_single_text_turn_returns_text_and_makes_one_complete_call(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    provider = fake_provider("final answer")
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    result = executor.run(_req(ws, artifacts))

    assert result.text == "final answer"
    assert result.error is None
    assert len(provider.calls) == 1


def test_describe_delegates_to_provider(tmp_path):
    provider = fake_provider("x")
    executor = ApiLoopExecutor(_cfg(), provider=provider)
    assert executor.describe() == f"api: {provider.describe()}"


def test_tool_call_then_text_writes_file_and_sends_one_user_message(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    provider = FakeModelProvider(
        script=[tool_turn("fs_write", {"path": "out.txt", "content": "hello"}), text_turn("done")]
    )
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    result = executor.run(_req(ws, artifacts, tools=["fs.write"]))

    assert (ws / "out.txt").read_text() == "hello"
    assert result.text == "done"
    assert (ws / "out.txt").resolve() in result.files_written

    last_msg = provider.calls[-1]["messages"][-1]
    assert last_msg.role == "user"
    assert isinstance(last_msg.content, list)
    assert len(last_msg.content) == 1
    assert last_msg.content[0].tool_use_id == "toolu_1"
    assert last_msg.content[0].is_error is False


def test_multiple_tool_calls_in_one_turn_produce_one_user_message_with_n_results(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    turn = ProviderMessage(
        text="",
        tool_calls=[
            ToolCall("t1", "fs_write", {"path": "a.txt", "content": "A"}),
            ToolCall("t2", "fs_write", {"path": "b.txt", "content": "B"}),
        ],
        stop_reason="tool_use",
    )
    provider = FakeModelProvider(script=[turn, text_turn("done")])
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    result = executor.run(_req(ws, artifacts, tools=["fs.write"]))

    assert result.text == "done"
    last_msg = provider.calls[-1]["messages"][-1]
    assert last_msg.role == "user"
    assert len(last_msg.content) == 2
    assert {b.tool_use_id for b in last_msg.content} == {"t1", "t2"}
    assert (ws / "a.txt").read_text() == "A"
    assert (ws / "b.txt").read_text() == "B"


def test_max_iterations_reached_ends_with_error_and_does_not_loop_forever(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    # Always returns a tool call -- would loop forever without max_iterations.
    provider = FakeModelProvider(script=[tool_turn("fs_read", {"path": "."})])
    executor = ApiLoopExecutor(_cfg(max_iterations=2), provider=provider)

    result = executor.run(_req(ws, artifacts, tools=["fs.read"]))

    assert result.error is not None
    assert "max_iterations" in result.error
    assert len(provider.calls) == 2


def test_non_allowlisted_tool_call_becomes_error_tool_result_not_an_exception(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    provider = FakeModelProvider(
        script=[tool_turn("shell_run", {"argv": ["python", "--version"]}), text_turn("recovered")]
    )
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    # shell.run is not in the allowlist below.
    result = executor.run(_req(ws, artifacts, tools=["fs.read"]))

    assert result.text == "recovered"
    assert result.error is None
    last_msg = provider.calls[-1]["messages"][-1]
    assert last_msg.content[0].is_error is True


def test_usage_accumulates_across_turns(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    provider = FakeModelProvider(script=[tool_turn("fs_read", {"path": "."}), text_turn("done")])
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    result = executor.run(_req(ws, artifacts, tools=["fs.read"]))

    # FakeModelProvider reports Usage(input_tokens=10, output_tokens=10) per call;
    # two complete() calls were made (tool turn, then the final text turn).
    assert len(provider.calls) == 2
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 20


def test_max_cost_usd_breach_stops_the_loop(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    provider = _CostlyProvider(cost_per_call=2.0)
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    result = executor.run(_req(ws, artifacts, tools=["fs.read"], max_cost_usd=1.0))

    assert result.error is not None
    assert "cost cap" in result.error
    assert provider.calls == 1


def test_assistant_turn_with_native_is_replayed_with_native_provider_fake(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    sentinel = object()
    first_turn = ProviderMessage(
        text="",
        tool_calls=[ToolCall("t1", "fs_read", {"path": "."})],
        stop_reason="tool_use",
        native=sentinel,
    )
    provider = FakeModelProvider(script=[first_turn, text_turn("done")])
    executor = ApiLoopExecutor(_cfg(), provider=provider)

    executor.run(_req(ws, artifacts, tools=["fs.read"]))

    messages = provider.calls[-1]["messages"]
    assistant_msg = next(m for m in messages if m.role == "assistant")
    assert assistant_msg.native is sentinel
    assert assistant_msg.native_provider == "fake"


def test_provider_error_mid_loop_ends_the_run_with_error(tmp_path):
    from ticketbot.models.base import ProviderError

    class _FailingProvider:
        provider_id = "fake"

        def describe(self) -> str:
            return "failing (fake)"

        def complete(self, *, system, messages, tools=None, max_tokens=None):
            raise ProviderError("boom: upstream exploded")

    ws, artifacts = _dirs(tmp_path)
    executor = ApiLoopExecutor(_cfg(), provider=_FailingProvider())

    result = executor.run(_req(ws, artifacts))

    assert result.error is not None
    assert "boom" in result.error


def test_log_path_receives_tool_call_traffic(tmp_path):
    ws, artifacts = _dirs(tmp_path)
    provider = FakeModelProvider(
        script=[tool_turn("fs_write", {"path": "out.txt", "content": "hi"}), text_turn("done")]
    )
    executor = ApiLoopExecutor(_cfg(), provider=provider)
    log_path = tmp_path / "logs" / "step.log"

    executor.run(_req(ws, artifacts, tools=["fs.write"], log_path=log_path))

    logged = log_path.read_text()
    assert "fs_write" in logged
