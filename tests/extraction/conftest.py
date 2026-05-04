from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def isolated_md(tmp_path: Path) -> "callable":
    """Copy a fixture MD into `tmp_path` so the extractor can write its
    sidecar next to it without polluting the fixtures dir.
    """

    def _make(name: str) -> Path:
        src = FIXTURES_DIR / name
        dst = tmp_path / src.name
        shutil.copy(src, dst)
        return dst

    return _make
