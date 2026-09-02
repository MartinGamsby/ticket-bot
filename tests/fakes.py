"""Shared test fakes. Created in section 3 (`FakeModelProvider` helpers); later
sections append `FakeExecutor`, `FakeRuntime`, `FakeSource`, `FakeSink` here.
"""

from __future__ import annotations

from ticketbot.adapters.runtimes.base import ExecOut
from ticketbot.models.base import ProviderMessage, ToolCall
from ticketbot.models.fake import FakeModelProvider


def fake_provider(*texts: str) -> FakeModelProvider:
    """A `FakeModelProvider` whose script is exactly these texts, in order (each
    becomes an end_turn `ProviderMessage`)."""
    return FakeModelProvider(script=list(texts))


def tool_turn(name: str, input: dict, *, id: str = "toolu_1") -> ProviderMessage:
    return ProviderMessage(text="", tool_calls=[ToolCall(id, name, input)], stop_reason="tool_use")


def text_turn(text: str) -> ProviderMessage:
    return ProviderMessage(text=text, stop_reason="end_turn")


class FakeRuntime:
    """Records calls; returns canned results. Satisfies the `Runtime` protocol
    (`adapters.runtimes.base.Runtime`) without touching a real subprocess or
    cloud session.
    """

    def __init__(
        self,
        *,
        exec_out: ExecOut | None = None,
        png: bytes | None = b"\x89PNG\r\n\x1a\n",
        preview: str | None = None,
        file_data: bytes = b"",
    ) -> None:
        self.exec_out = exec_out if exec_out is not None else ExecOut(exit_code=0, stdout="ok")
        self.png = png
        self.preview = preview
        self.file_data = file_data
        self.calls: list[tuple[str, tuple, dict]] = []
        self.started = False
        self.stopped = False

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        self.calls.append((name, args, kwargs))

    def describe(self) -> str:
        self._record("describe")
        return "fake"

    def start(self) -> None:
        self._record("start")
        self.started = True

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecOut:
        self._record("exec", argv, cwd=cwd, timeout=timeout, env=env)
        return self.exec_out

    def read_file(self, path: str) -> bytes:
        self._record("read_file", path)
        return self.file_data

    def write_file(self, path: str, data: bytes) -> None:
        self._record("write_file", path, data)

    def screenshot(self) -> bytes | None:
        self._record("screenshot")
        return self.png

    def preview_url(self, port: int) -> str | None:
        self._record("preview_url", port)
        return self.preview

    def stop(self) -> None:
        self._record("stop")
        self.stopped = True
