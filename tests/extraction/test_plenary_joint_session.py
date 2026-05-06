"""End-to-end tests for the plenary_joint_session extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.extraction import extract
from monitorul_ii.extraction.extractors.plenary_joint_session import (
    EXTRACTOR_LABEL,
    EXTRACTOR_VERSION,
    detect_chambers_present,
)
from monitorul_ii.extraction.schema import validate

JOINT_SESSION_FIXTURES = (
    "2025-10-13_MO-PII-117-2025.md",  # Modern joint session, multi-segment chairs
    "2013-01-30_MO-PII-1-2013.md",  # Mid-corpus older joint
)

COVERAGE_FLOOR = 0.80


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "plenary" / name


def test_extractor_version_format():
    parts = EXTRACTOR_VERSION.split(".")
    assert len(parts) == 3


def test_extractor_label_format():
    assert EXTRACTOR_LABEL.startswith("regex@plenary_joint_session@")


def test_detect_chambers_present_positive():
    body = "Some text\n# **ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI**\n"
    assert detect_chambers_present(body) == ["Camera Deputaților", "Senatul"]


def test_detect_chambers_present_negative():
    body = "Some plain text without the joint header"
    assert detect_chambers_present(body) == []


@pytest.mark.parametrize("md_name", JOINT_SESSION_FIXTURES)
def test_joint_session_extracts(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.status == "extract", r.reason
    assert r.doc_type == "plenary_joint_session", (
        f"{md_name}: classified as {r.doc_type}, expected joint"
    )


@pytest.mark.parametrize("md_name", JOINT_SESSION_FIXTURES)
def test_joint_session_validates(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    validate(r.sidecar)


@pytest.mark.parametrize("md_name", JOINT_SESSION_FIXTURES)
def test_joint_session_coverage_floor(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.coverage_pct >= COVERAGE_FLOOR, (
        f"{md_name}: coverage {r.coverage_pct:.4f} below {COVERAGE_FLOOR}"
    )


@pytest.mark.parametrize("md_name", JOINT_SESSION_FIXTURES)
def test_joint_session_chambers_present(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sess = r.sidecar["body"]["session"]
    assert sess["chambers_present"] == ["Camera Deputaților", "Senatul"]


def test_2025_10_13_multi_segment_chairs(tmp_path: Path):
    """Sample 2025-10-13 has multi-segment chair narrative ('în prima parte',
    'Ultima parte')."""
    src = _fixture_path("2025-10-13_MO-PII-117-2025.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sess = r.sidecar["body"]["session"]
    # Two chairs at minimum (one from each chamber); often more due to
    # multi-segment swaps
    assert len(sess["chair"]) >= 2
    # Chair segments should also be populated (multi-segment case)
    assert len(sess["chair_segments"]) >= 2


def test_2025_10_13_attendance(tmp_path: Path):
    """Joint session attendance counts both chambers."""
    src = _fixture_path("2025-10-13_MO-PII-117-2025.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    att = r.sidecar["body"]["session"]["attendance"]
    assert att["registered"] == 296
    assert att["total_seats"] == 464


def test_2025_10_13_format_mixed(tmp_path: Path):
    src = _fixture_path("2025-10-13_MO-PII-117-2025.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.sidecar["body"]["session"]["format"] == "mixed"
