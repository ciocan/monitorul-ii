from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.db import DB


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


@pytest.fixture
def db(db_path: Path):
    with DB(db_path) as d:
        yield d
