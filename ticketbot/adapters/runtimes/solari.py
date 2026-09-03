"""`SolariRuntime` -- cloud sandboxes, browsers and desktops behind one
`SOLARI_API_KEY`. **Solari is not a model.** It is where a step's shell commands
run (`mode: sandbox`/`desktop`) and where its screenshots come from
(`mode: desktop`/`browser`); the coding itself is still done by whatever
`ModelProvider`/`Executor` the profile configures.

The Solari SDKs (`solari_sandbox`, `solari_desktop`, `solari_browser`) are async
and imported LAZILY, inside `start()`, so `import ticketbot.adapters.runtimes.solari`
succeeds even when the `solari` extra is not installed -- exactly the pattern
`models/anthropic.py` uses for the `anthropic` package.

Because the SDKs are async and this runtime exposes the SYNC `Runtime` protocol,
every call is dispatched onto one dedicated asyncio event loop that lives on one
daemon thread for the runtime's whole life (`_LoopThread`). Calling `asyncio.run()`
per method would create and destroy a loop per call and invalidate the SDK's
session objects -- do not do that.

Lifecycle traps encoded here, not left as comments:
  - sandbox teardown is `kill()`, never `close()` -- `close()` only drops the
    local control channel and the VM keeps running (and billing) until its idle
    timeout.
  - desktop teardown is `close()` **and** `client.destroy(session_id)`.
  - `timeout_ms` is a rolling IDLE window, not a deadline -- it resets on every
    call. Bounding a step's wall-clock time is `engine/budget.py`'s job, not this
    class's.
  - sandbox `run_code` results are a list of items with `.type`/`.text` -- there
    is no top-level `.stdout`.
  - sandbox commands are not shell-interpreted -- argv goes through `args`, never
    through a formatted shell string.
  - the Python `commands.run`/`previewUrl` surface is unconfirmed (it only
    appears in the TS examples), so both are feature-detected with `getattr`/
    `hasattr`, never hard-coded, with a `run_code` fallback for command execution.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import threading
from typing import Any

from ...config.loader import expand_env
from ...config.redact import register_secret
from ...config.schema import AdapterConfig
from .base import BaseRuntime, ExecOut, RuntimeAdapterError, RuntimeUnavailable

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.getsolari.com"

# mode -> (module name, class name). Imported lazily inside start().
_SDK_TARGETS: dict[str, tuple[str, str]] = {
    "sandbox": ("solari_sandbox", "SandboxClient"),
    "desktop": ("solari_desktop", "DesktopClient"),
    "browser": ("solari_browser", "Solari"),
}

# A JSON payload is embedded as a Python string LITERAL (via json.dumps of the
# already-JSON-encoded payload), never by formatting argv/paths into shell or
# Python source -- that is what keeps model-supplied strings from ever being
# interpreted as code.
_RUN_ARGV_SNIPPET = (
    "import json, subprocess\n"
    "_p = json.loads({payload})\n"
    "_r = subprocess.run(_p['argv'], cwd=_p.get('cwd'), capture_output=True, "
    "timeout=_p.get('timeout'))\n"
    "print(json.dumps({{'exit_code': _r.returncode, "
    "'stdout': _r.stdout.decode('utf-8', 'replace'), "
    "'stderr': _r.stderr.decode('utf-8', 'replace')}}))\n"
)

_READ_FILE_SNIPPET = (
    "import base64, json\n"
    "_p = json.loads({payload})\n"
    "with open(_p['path'], 'rb') as _f:\n"
    "    print(base64.b64encode(_f.read()).decode('ascii'))\n"
)

_WRITE_FILE_SNIPPET = (
    "import base64, json\n"
    "_p = json.loads({payload})\n"
    "with open(_p['path'], 'wb') as _f:\n"
    "    _f.write(base64.b64decode(_p['data_b64']))\n"
    "print('ok')\n"
)


def _embed_payload(payload: dict[str, Any]) -> str:
    """A JSON literal, safe to splice into a `{payload}` template slot: `json.dumps`
    twice -- once for the payload, once more so the result is a quoted, escaped
    string literal that is valid to both `json.loads` and the Python parser.
    """
    return json.dumps(json.dumps(payload))


def _stdout_text(result: Any) -> str:
    """Concatenate the `.text` of every `results` item whose `.type == "stdout"`.
    There is no top-level `.stdout` on a `run_code` result -- only this list.
    """
    text = ""
    for item in getattr(result, "results", None) or []:
        if getattr(item, "type", None) == "stdout":
            text += getattr(item, "text", "") or ""
    return text


class _LoopThread:
    """One asyncio event loop pinned to one daemon thread for the runtime's whole
    life. Do NOT call asyncio.run() per method -- that creates and destroys a loop
    per call and invalidates the SDK's session objects.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="ticketbot-solari"
        )
        self._thread.start()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class SolariRuntime(BaseRuntime):
    MODES = ("sandbox", "browser", "desktop")

    def __init__(self, cfg: AdapterConfig) -> None:
        mode = cfg.opt("mode")
        if mode not in self.MODES:
            raise RuntimeAdapterError(
                f"runtime type=solari: 'mode' must be one of {self.MODES}, got {mode!r}"
            )
        self.mode: str = mode

        # `${SOLARI_API_KEY}` is kept UNEXPANDED here -- expand_env() raises
        # MissingEnvError for an unset variable, and __init__ must not raise just
        # because the key isn't set yet (so `ticketbot validate` works without it).
        self.api_key_ref: str = str(cfg.opt("api_key", "${SOLARI_API_KEY}"))
        self.base_url: str = str(cfg.opt("base_url", DEFAULT_BASE_URL))

        default_template = "default" if mode == "desktop" else "base"
        self.template: str = str(cfg.opt("template", default_template))
        self.resolution: str = str(cfg.opt("resolution", "1280x720"))
        self.timeout_ms: int = int(cfg.opt("timeout_ms", 600_000))
        self.recording: bool = bool(cfg.opt("recording", False))
        self.ready_timeout_s: int = int(cfg.opt("ready_timeout_s", 30))

        # Populated by start(); stop() must be able to tear these down even when
        # start() only got partway before failing, so they are never gated behind
        # `_started`.
        self._loop: _LoopThread | None = None
        self._client: Any = None
        self._client_entered: bool = False
        self._session: Any = None
        self._page: Any = None
        self._code_ctx: Any = None

    @property
    def can_exec(self) -> bool:  # type: ignore[override]
        """Only the sandbox mode has a command-execution surface -- `exec()` below
        raises `RuntimeUnavailable` for `browser` and `desktop`. `executors/tools.py:
        _shell_run` reads this and runs the command locally in those modes rather
        than failing every `shell.run` the coder and tester make.
        """
        return self.mode == "sandbox"

    def describe(self) -> str:
        if self.mode == "desktop":
            return f"Solari desktop {self.resolution}"
        if self.mode == "sandbox":
            return f"Solari sandbox ({self.template})"
        return "Solari browser"

    # ------------------------------------------------------------------ #
    # start / stop
    # ------------------------------------------------------------------ #

    def _import_client_class(self) -> Any:
        module_name, class_name = _SDK_TARGETS[self.mode]
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise RuntimeAdapterError(
                f"runtime 'solari' mode {self.mode!r} needs: pip install ticketbot[solari] "
                f"({exc})"
            ) from exc
        return getattr(module, class_name)

    def start(self) -> None:
        """Idempotent. Imports the SDK lazily, opens the loop thread, enters the
        client's async context manager, creates + connects the session, and (for
        `mode: desktop`) polls `health().ready` up to `ready_timeout_s`. If any
        step after the loop is created fails, whatever got created is left in
        place for `stop()` to tear down -- `_started` is only set True on full
        success.
        """
        if self._started:
            return

        client_cls = self._import_client_class()
        api_key = expand_env(self.api_key_ref)
        register_secret(api_key)

        if self._loop is None:
            self._loop = _LoopThread()
        self._loop.run(self._async_start(client_cls, api_key))
        self._started = True

    async def _async_start(self, client_cls: Any, api_key: str) -> None:
        if self.mode == "sandbox":
            client = client_cls(api_key=api_key, base_url=self.base_url)
            await client.__aenter__()
            self._client = client
            self._client_entered = True
            sandbox = await client.create(template=self.template, timeout_ms=self.timeout_ms)
            await sandbox.connect()
            self._session = sandbox

        elif self.mode == "desktop":
            client = client_cls(api_key=api_key, base_url=self.base_url)
            await client.__aenter__()
            self._client = client
            self._client_entered = True
            desktop = await client.create(
                template=self.template, resolution=self.resolution, timeout_ms=self.timeout_ms
            )
            await desktop.connect()
            self._session = desktop

            ready = False
            for _ in range(max(self.ready_timeout_s, 0)):
                health = await desktop.health()
                if getattr(health, "ready", False):
                    ready = True
                    break
                await asyncio.sleep(1)
            if not ready:
                raise RuntimeAdapterError(
                    f"solari desktop: not ready after {self.ready_timeout_s}s "
                    "(health().ready never became true)"
                )

        else:  # browser
            solari = client_cls(api_key=api_key)
            self._client = solari
            browser = await solari.launch(recording=self.recording)
            self._session = browser

    def stop(self) -> None:
        """Idempotent, and runs even after a partial `start()` -- a leaked cloud
        VM keeps billing until its idle timeout, so teardown must always be
        attempted. Each step is wrapped individually so a failure in one (e.g.
        `close()`) never skips the next (e.g. `client.destroy(...)`), and a
        teardown error is logged, never raised -- it must not mask whatever
        error brought us here.
        """
        loop = self._loop
        if loop is not None and (self._session is not None or self._client is not None):
            try:
                loop.run(self._async_stop())
            except Exception:
                logger.exception("solari runtime (%s): error during teardown", self.mode)

        self._session = None
        self._client = None
        self._client_entered = False
        self._page = None
        self._code_ctx = None

        if loop is not None:
            try:
                loop.close()
            except Exception:
                logger.exception("solari runtime (%s): error closing the loop thread", self.mode)
        self._loop = None
        self._started = False

    async def _async_stop(self) -> None:
        if self.mode == "sandbox":
            if self._session is not None:
                try:
                    await self._session.kill()
                except Exception:
                    logger.exception("solari sandbox: kill() failed")
            await self._exit_client()

        elif self.mode == "desktop":
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    logger.exception("solari desktop: close() failed")
                try:
                    if self._client is not None:
                        await self._client.destroy(self._session.sessionId)
                except Exception:
                    logger.exception("solari desktop: client.destroy() failed")
            await self._exit_client()

        else:  # browser
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    logger.exception("solari browser: close() failed")

    async def _exit_client(self) -> None:
        if self._client is not None and self._client_entered:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                logger.exception("solari runtime (%s): client teardown failed", self.mode)

    # ------------------------------------------------------------------ #
    # exec
    # ------------------------------------------------------------------ #

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecOut:
        if self.mode == "browser":
            raise RuntimeUnavailable("solari runtime mode 'browser' cannot execute commands")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise RuntimeAdapterError("solari exec requires a non-empty 'argv' list of strings")
        if self._loop is None or self._session is None:
            raise RuntimeAdapterError("solari runtime: exec() called before start()")

        timeout_s = float(timeout) if timeout is not None else (self.timeout_ms / 1000.0)
        return self._loop.run(self._exec_async(list(argv), cwd, timeout_s))

    async def _exec_async(self, argv: list[str], cwd: str | None, timeout: float) -> ExecOut:
        sb = self._session
        cmds = getattr(sb, "commands", None)
        if cmds is not None and hasattr(cmds, "run"):
            out = await cmds.run(argv[0], args=list(argv[1:]), cwd=cwd, timeout_ms=int(timeout * 1000))
            return ExecOut(
                exit_code=getattr(out, "exitCode", getattr(out, "exit_code", 0)) or 0,
                stdout=getattr(out, "stdout", "") or "",
                stderr=getattr(out, "stderr", "") or "",
            )

        if not hasattr(sb, "run_code"):
            raise RuntimeUnavailable(
                f"solari runtime mode {self.mode!r} has no command-execution surface"
            )

        # Fallback: drive subprocess from inside the kernel and print a JSON
        # envelope. The payload is a JSON literal -- never a formatted shell string.
        payload = _embed_payload({"argv": argv, "cwd": cwd, "timeout": timeout})
        src = _RUN_ARGV_SNIPPET.format(payload=payload)
        ctx = await self._get_code_ctx()
        result = await sb.run_code(src, context_id=ctx)

        error = getattr(result, "error", None)
        if error:
            return ExecOut(exit_code=-1, stderr=str(error))

        stdout_text = _stdout_text(result).strip()
        try:
            envelope = json.loads(stdout_text)
        except (ValueError, TypeError):
            return ExecOut(
                exit_code=-1,
                stderr=f"solari sandbox: could not parse run_code envelope: {stdout_text!r}",
            )

        return ExecOut(
            exit_code=int(envelope.get("exit_code", -1)),
            stdout=envelope.get("stdout", "") or "",
            stderr=envelope.get("stderr", "") or "",
        )

    async def _get_code_ctx(self) -> Any:
        if self._code_ctx is None:
            self._code_ctx = await self._session.create_code_context("python")
        return self._code_ctx

    # ------------------------------------------------------------------ #
    # files
    # ------------------------------------------------------------------ #

    def read_file(self, path: str) -> bytes:
        if self.mode != "sandbox":
            raise RuntimeUnavailable(f"solari runtime mode {self.mode!r} cannot read files")
        if self._loop is None or self._session is None:
            raise RuntimeAdapterError("solari runtime: read_file() called before start()")
        return self._loop.run(self._read_file_async(path))

    async def _read_file_async(self, path: str) -> bytes:
        sb = self._session
        files = getattr(sb, "files", None)
        if files is not None and hasattr(files, "read"):
            data = await files.read(path)
            return data if isinstance(data, bytes) else bytes(data)

        payload = _embed_payload({"path": path})
        src = _READ_FILE_SNIPPET.format(payload=payload)
        ctx = await self._get_code_ctx()
        result = await sb.run_code(src, context_id=ctx)
        return base64.b64decode(_stdout_text(result).strip())

    def write_file(self, path: str, data: bytes) -> None:
        if self.mode != "sandbox":
            raise RuntimeUnavailable(f"solari runtime mode {self.mode!r} cannot write files")
        if self._loop is None or self._session is None:
            raise RuntimeAdapterError("solari runtime: write_file() called before start()")
        self._loop.run(self._write_file_async(path, data))

    async def _write_file_async(self, path: str, data: bytes) -> None:
        sb = self._session
        files = getattr(sb, "files", None)
        if files is not None and hasattr(files, "write"):
            await files.write(path, data)
            return

        payload = _embed_payload({"path": path, "data_b64": base64.b64encode(data).decode("ascii")})
        src = _WRITE_FILE_SNIPPET.format(payload=payload)
        ctx = await self._get_code_ctx()
        await sb.run_code(src, context_id=ctx)

    # ------------------------------------------------------------------ #
    # screenshot / preview_url
    # ------------------------------------------------------------------ #

    def screenshot(self) -> bytes | None:
        if self.mode == "sandbox":
            return None
        if self._loop is None or self._session is None:
            return None
        if self.mode == "desktop":
            return self._loop.run(self._session.screenshot(format="png"))
        return self._loop.run(self._browser_screenshot_async())

    async def _browser_screenshot_async(self) -> bytes:
        if self._page is None:
            self._page = await self._session.new_page()
        return await self._page.screenshot(type="png")

    def preview_url(self, port: int) -> str | None:
        if self.mode != "sandbox":
            return None
        if self._loop is None or self._session is None:
            return None
        return self._loop.run(self._preview_url_async(port))

    async def _preview_url_async(self, port: int) -> str | None:
        sb = self._session
        fn = getattr(sb, "preview_url", None) or getattr(sb, "previewUrl", None)
        if fn is None:
            return None
        result = await fn(port)
        if isinstance(result, str):
            return result
        url = getattr(result, "url", None)
        return str(url) if url is not None else None
