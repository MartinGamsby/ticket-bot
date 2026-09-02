"""`SolariRuntime` -- the sync facade over the async Solari SDKs.

**No test here may contact Solari.** `SOLARI_API_KEY` is not set in CI, and every
test either leaves it unset (to prove `__init__` tolerates that) or sets a fake
value via `monkeypatch`. Every SDK a test needs is a fake module injected into
`sys.modules` -- `solari_sandbox`/`solari_desktop`/`solari_browser` are never
imported for real, and none of these tests require the `solari` extra installed.
"""

from __future__ import annotations

import ast
import base64
import json
import re
import sys
from types import SimpleNamespace

import pytest

from ticketbot.adapters.runtimes.base import RuntimeAdapterError, RuntimeUnavailable
from ticketbot.adapters.runtimes.solari import SolariRuntime
from ticketbot.config.schema import AdapterConfig

# --------------------------------------------------------------------------- #
# Fake SDK objects -- async, call-recording, behavior tuned via closures.
# --------------------------------------------------------------------------- #


def _run_code_result(text: str = "", error: str | None = None):
    item = SimpleNamespace(type="stdout", text=text)
    return SimpleNamespace(results=[item] if text else [], error=error)


def _async_return(value):
    async def _fn(_port):
        return value

    return _fn


def _extract_payload_dict(src: str) -> dict:
    """Pull the JSON payload literally embedded in a generated kernel snippet and
    parse it back out -- proof it went in as a JSON literal (`json.loads("...")`),
    never as argv/paths formatted straight into shell or Python source.
    """
    match = re.search(r"json\.loads\((.*)\)\n", src)
    assert match is not None, f"no json.loads(...) literal found in:\n{src}"
    inner_json_str = ast.literal_eval(match.group(1))
    return json.loads(inner_json_str)


class _FakeCommands:
    def __init__(self, out=None):
        self.calls: list[tuple] = []
        self._out = out or SimpleNamespace(exitCode=0, stdout="from commands.run\n", stderr="")

    async def run(self, cmd, *, args=None, cwd=None, timeout_ms=None):
        self.calls.append((cmd, args, cwd, timeout_ms))
        return self._out


class _FakeSandboxSession:
    def __init__(self, *, with_commands: bool, run_code_result=None):
        self.connected = False
        self.killed = False
        self.closed = False
        self.commands = _FakeCommands() if with_commands else None
        self.files = None
        self.code_ctx_calls = 0
        self.run_code_calls: list[tuple[str, str | None]] = []
        self._run_code_result = run_code_result if run_code_result is not None else _run_code_result()

    async def connect(self):
        self.connected = True

    async def kill(self):
        self.killed = True

    async def close(self):  # sandbox stop() must NEVER call this
        self.closed = True

    async def create_code_context(self, lang):
        self.code_ctx_calls += 1
        return f"ctx-{self.code_ctx_calls}"

    async def run_code(self, src, context_id=None):
        self.run_code_calls.append((src, context_id))
        return self._run_code_result


def make_sandbox_module(*, with_commands: bool = False, run_code_result=None):
    class FakeSandboxClient:
        def __init__(self, api_key=None, base_url=None):
            self.api_key = api_key
            self.base_url = base_url
            self.entered = False
            self.exited = False
            self.session: _FakeSandboxSession | None = None

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *exc_info):
            self.exited = True
            return False

        async def create(self, *, template, timeout_ms):
            self.session = _FakeSandboxSession(
                with_commands=with_commands, run_code_result=run_code_result
            )
            return self.session

    return SimpleNamespace(SandboxClient=FakeSandboxClient)


class _FakeDesktopSession:
    def __init__(self, *, ready_after: int | None):
        self.sessionId = "sess-123"
        self.streamUrl = "wss://stream.example/sess-123"
        self.connected = False
        self.closed = False
        self.health_calls = 0
        self._ready_after = ready_after
        self.screenshot_calls = 0

    async def connect(self):
        self.connected = True

    async def health(self):
        self.health_calls += 1
        ready = self._ready_after is not None and self.health_calls >= self._ready_after
        return SimpleNamespace(ready=ready)

    async def close(self):
        self.closed = True

    async def screenshot(self, format="png"):
        self.screenshot_calls += 1
        return b"\x89PNG-desktop"


def make_desktop_module(*, ready_after: int | None = 1):
    class FakeDesktopClient:
        def __init__(self, api_key=None, base_url=None):
            self.api_key = api_key
            self.base_url = base_url
            self.entered = False
            self.exited = False
            self.destroyed: list[str] = []
            self.session: _FakeDesktopSession | None = None

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *exc_info):
            self.exited = True
            return False

        async def create(self, *, template, resolution, timeout_ms):
            self.session = _FakeDesktopSession(ready_after=ready_after)
            return self.session

        async def destroy(self, session_id):
            self.destroyed.append(session_id)

    return SimpleNamespace(DesktopClient=FakeDesktopClient)


class _FakePage:
    def __init__(self):
        self.screenshot_calls = 0

    async def screenshot(self, type="png"):
        self.screenshot_calls += 1
        return b"\x89PNG-browser"


class _FakeBrowserSession:
    def __init__(self):
        self.id = "browser-1"
        self.closed = False
        self.pages: list[_FakePage] = []

    async def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


def make_browser_module():
    class FakeSolari:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.launched: _FakeBrowserSession | None = None
            self.recording: bool | None = None

        async def launch(self, recording=False):
            self.launched = _FakeBrowserSession()
            self.recording = recording
            return self.launched

    return SimpleNamespace(Solari=FakeSolari)


# --------------------------------------------------------------------------- #
# __init__
# --------------------------------------------------------------------------- #


def test_init_rejects_a_bogus_mode():
    with pytest.raises(RuntimeAdapterError):
        SolariRuntime(AdapterConfig(type="solari", mode="bogus"))


def test_init_does_not_raise_when_api_key_is_unset(monkeypatch):
    monkeypatch.delenv("SOLARI_API_KEY", raising=False)
    SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))  # must not raise


# --------------------------------------------------------------------------- #
# start() without the SDK installed
# --------------------------------------------------------------------------- #


def test_start_without_sdk_raises_with_pip_install_hint(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", None)  # forces ImportError
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop"))

    with pytest.raises(RuntimeAdapterError) as excinfo:
        runtime.start()
    assert "pip install ticketbot[solari]" in str(excinfo.value)

    runtime.stop()  # must not raise


# --------------------------------------------------------------------------- #
# describe()
# --------------------------------------------------------------------------- #


def test_describe_sandbox():
    assert SolariRuntime(AdapterConfig(type="solari", mode="sandbox")).describe() == "Solari sandbox (base)"


def test_describe_desktop():
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop"))
    assert runtime.describe() == "Solari desktop 1280x720"


def test_describe_browser():
    assert SolariRuntime(AdapterConfig(type="solari", mode="browser")).describe() == "Solari browser"


# --------------------------------------------------------------------------- #
# desktop start()/stop()
# --------------------------------------------------------------------------- #


def test_desktop_start_polls_health_until_ready(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", make_desktop_module(ready_after=1))
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop", ready_timeout_s=5))

    runtime.start()

    assert runtime._session is not None
    assert runtime._session.connected is True
    runtime.stop()


def test_desktop_start_gives_up_after_ready_timeout_s(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", make_desktop_module(ready_after=None))
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop", ready_timeout_s=1))

    with pytest.raises(RuntimeAdapterError):
        runtime.start()

    runtime.stop()  # must not raise


def test_desktop_stop_after_failed_start_tears_down_the_partial_session(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", make_desktop_module(ready_after=None))
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop", ready_timeout_s=1))

    with pytest.raises(RuntimeAdapterError):
        runtime.start()

    session = runtime._session
    client = runtime._client
    assert session is not None  # the partial session really got created

    runtime.stop()

    assert session.closed is True
    assert client.destroyed == [session.sessionId]


def test_desktop_stop_calls_both_close_and_client_destroy(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", make_desktop_module(ready_after=1))
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop"))
    runtime.start()
    session = runtime._session
    client = runtime._client

    runtime.stop()

    assert session.closed is True
    assert client.destroyed == [session.sessionId]
    assert client.exited is True


# --------------------------------------------------------------------------- #
# sandbox stop()
# --------------------------------------------------------------------------- #


def test_sandbox_stop_calls_kill_and_never_only_close(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()
    session = runtime._session
    client = runtime._client

    runtime.stop()

    assert session.killed is True
    assert session.closed is False
    assert client.exited is True


# --------------------------------------------------------------------------- #
# browser stop()
# --------------------------------------------------------------------------- #


def test_browser_stop_calls_browser_close(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_browser", make_browser_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="browser"))
    runtime.start()
    session = runtime._session

    runtime.stop()

    assert session.closed is True


# --------------------------------------------------------------------------- #
# sandbox exec()
# --------------------------------------------------------------------------- #


def test_sandbox_exec_uses_commands_run_when_present(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module(with_commands=True))
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    out = runtime.exec(["python3", "-c", "print(1)"], cwd="/tmp", timeout=5)

    assert out.exit_code == 0
    assert out.stdout == "from commands.run\n"
    assert runtime._session.commands.calls == [("python3", ["-c", "print(1)"], "/tmp", 5000)]
    assert runtime._session.run_code_calls == []
    runtime.stop()


def test_sandbox_exec_falls_back_to_run_code_when_commands_absent(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    envelope = json.dumps({"exit_code": 0, "stdout": "hi\n", "stderr": ""})
    module = make_sandbox_module(with_commands=False, run_code_result=_run_code_result(text=envelope))
    monkeypatch.setitem(sys.modules, "solari_sandbox", module)
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    # Deliberately loaded with shell metacharacters to prove they never get
    # concatenated into a shell command string -- they must survive as one
    # opaque argv element inside the JSON payload.
    argv = ["sh", "-c", 'echo hi; rm -rf "$HOME"']
    out = runtime.exec(argv, cwd="/tmp", timeout=5)

    assert out.exit_code == 0
    assert out.stdout == "hi\n"
    assert len(runtime._session.run_code_calls) == 1
    src, ctx = runtime._session.run_code_calls[0]
    assert ctx == "ctx-1"
    payload = _extract_payload_dict(src)
    assert payload == {"argv": argv, "cwd": "/tmp", "timeout": 5.0}
    runtime.stop()


def test_sandbox_exec_run_code_error_surfaces_as_nonzero_exit(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    module = make_sandbox_module(with_commands=False, run_code_result=_run_code_result(error="boom"))
    monkeypatch.setitem(sys.modules, "solari_sandbox", module)
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    out = runtime.exec(["true"])

    assert out.exit_code == -1
    assert "boom" in out.stderr
    runtime.stop()


def test_browser_exec_raises_runtime_unavailable(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_browser", make_browser_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="browser"))
    runtime.start()

    with pytest.raises(RuntimeUnavailable):
        runtime.exec(["echo", "hi"])
    runtime.stop()


# --------------------------------------------------------------------------- #
# read_file / write_file
# --------------------------------------------------------------------------- #


def test_sandbox_read_write_file_uses_files_surface_when_present(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    store: dict[str, bytes] = {}

    class FakeFiles:
        async def write(self, path, data):
            store[path] = data

        async def read(self, path):
            return store[path]

    runtime._session.files = FakeFiles()

    runtime.write_file("/tmp/hello.txt", b"hello")
    assert runtime.read_file("/tmp/hello.txt") == b"hello"
    assert runtime._session.run_code_calls == []  # files surface used, no fallback
    runtime.stop()


def test_sandbox_write_file_fallback_embeds_base64_payload_as_json_literal(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    module = make_sandbox_module(with_commands=False, run_code_result=_run_code_result(text="ok\n"))
    monkeypatch.setitem(sys.modules, "solari_sandbox", module)
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    runtime.write_file("/tmp/x.bin", b"binary\x00data")

    src, _ctx = runtime._session.run_code_calls[0]
    payload = _extract_payload_dict(src)
    assert payload["path"] == "/tmp/x.bin"
    assert base64.b64decode(payload["data_b64"]) == b"binary\x00data"
    runtime.stop()


def test_sandbox_read_file_fallback_decodes_base64_stdout(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    encoded = base64.b64encode(b"file contents").decode("ascii")
    module = make_sandbox_module(with_commands=False, run_code_result=_run_code_result(text=encoded + "\n"))
    monkeypatch.setitem(sys.modules, "solari_sandbox", module)
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    assert runtime.read_file("/tmp/x.bin") == b"file contents"
    runtime.stop()


def test_desktop_read_file_raises_runtime_unavailable(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", make_desktop_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop"))
    runtime.start()

    with pytest.raises(RuntimeUnavailable):
        runtime.read_file("/tmp/x.txt")
    runtime.stop()


# --------------------------------------------------------------------------- #
# screenshot
# --------------------------------------------------------------------------- #


def test_sandbox_screenshot_is_none(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    assert runtime.screenshot() is None
    runtime.stop()


def test_desktop_screenshot_returns_png_bytes(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_desktop", make_desktop_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="desktop"))
    runtime.start()

    assert runtime.screenshot() == b"\x89PNG-desktop"
    runtime.stop()


def test_browser_screenshot_creates_page_lazily(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_browser", make_browser_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="browser"))
    runtime.start()

    assert runtime.screenshot() == b"\x89PNG-browser"
    assert len(runtime._session.pages) == 1
    runtime.stop()


# --------------------------------------------------------------------------- #
# preview_url
# --------------------------------------------------------------------------- #


def test_preview_url_returns_none_when_neither_name_exists(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()

    assert runtime.preview_url(3000) is None
    runtime.stop()


def test_preview_url_string_form(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()
    runtime._session.preview_url = _async_return("https://3000-abc.preview.getsolari.com")

    assert runtime.preview_url(3000) == "https://3000-abc.preview.getsolari.com"
    runtime.stop()


def test_preview_url_object_with_url_attribute_form(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_sandbox", make_sandbox_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="sandbox"))
    runtime.start()
    runtime._session.previewUrl = _async_return(
        SimpleNamespace(url="https://3000-abc.preview.getsolari.com")
    )

    assert runtime.preview_url(3000) == "https://3000-abc.preview.getsolari.com"
    runtime.stop()


def test_preview_url_none_outside_sandbox_mode(monkeypatch):
    monkeypatch.setenv("SOLARI_API_KEY", "slr_live_testtesttest")
    monkeypatch.setitem(sys.modules, "solari_browser", make_browser_module())
    runtime = SolariRuntime(AdapterConfig(type="solari", mode="browser"))
    runtime.start()

    assert runtime.preview_url(3000) is None
    runtime.stop()
