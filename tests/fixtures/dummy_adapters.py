"""A dummy adapter target used only by tests/test_core_registry.py to exercise
`Registry.get`/`.create` without importing any real (not-yet-written) adapter module.
"""

from __future__ import annotations

from typing import Any


class DummyAdapter:
    def __init__(self, cfg: Any, **kwargs: Any) -> None:
        self.cfg = cfg
        self.kwargs = kwargs
