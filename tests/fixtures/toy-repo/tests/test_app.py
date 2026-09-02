import sys
from pathlib import Path

# `app.py` lives at the repo root, one level up from this test file, and this
# toy project has no packaging of its own -- make it importable regardless of
# where `pytest` is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import add  # noqa: E402


def test_add():
    assert add(2, 3) == 5
