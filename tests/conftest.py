"""Shared pytest fixtures.

load_fixture reads a file under tests/fixtures/ and returns its content
completely unmodified: no strip(), no encoding conversion, no blank-line
removal. Parser correctness (including whitespace/blank-line handling) must
be exercised against exactly what's on disk, not a cleaned-up copy of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(relative_path: str) -> str:
        return (FIXTURES_DIR / relative_path).read_text(encoding="utf-8")

    return _load
