"""`NoneRuntime` -- the default: no shell commands, no screenshots, no session,
but `screenshot()`/`preview_url()` return `None` rather than raising so a
pipeline configured with `screenshot_on:` still completes when `runtime: none`.
"""

from __future__ import annotations

import pytest

from ticketbot.adapters.runtimes.base import RuntimeUnavailable
from ticketbot.adapters.runtimes.none import NoneRuntime
from ticketbot.config.schema import AdapterConfig


def _runtime() -> NoneRuntime:
    return NoneRuntime(AdapterConfig(type="none"))


def test_describe_is_none():
    assert _runtime().describe() == "none"


def test_screenshot_returns_none_not_an_error():
    assert _runtime().screenshot() is None


def test_preview_url_returns_none():
    assert _runtime().preview_url(3000) is None


def test_declares_that_it_cannot_execute_commands():
    """The flag callers read INSTEAD of `runtime is not None` -- this runtime is a
    real object, so a null check says "route commands here" and every `shell.run`
    then dies on `exec()`'s `RuntimeUnavailable`. `executors/tools.py` reads this
    and runs the command locally instead.
    """
    assert _runtime().can_exec is False


def test_exec_raises_runtime_unavailable():
    with pytest.raises(RuntimeUnavailable):
        _runtime().exec([])


def test_read_file_raises_runtime_unavailable():
    with pytest.raises(RuntimeUnavailable):
        _runtime().read_file("anything.txt")


def test_write_file_raises_runtime_unavailable():
    with pytest.raises(RuntimeUnavailable):
        _runtime().write_file("anything.txt", b"data")


def test_start_and_stop_are_idempotent():
    runtime = _runtime()
    runtime.start()
    runtime.start()
    runtime.stop()
    runtime.stop()


def test_stop_without_start_does_not_raise():
    _runtime().stop()


def test_constructs_without_a_config():
    runtime = NoneRuntime()
    assert runtime.describe() == "none"


def test_context_manager_form_starts_and_stops():
    with _runtime() as runtime:
        assert runtime.describe() == "none"
