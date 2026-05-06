"""End-to-end tests for the plenary_stenogram extractor + per-fixture
coverage assertions per Q9 (test floor 0.80).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.extraction import extract
from monitorul_ii.extraction.extractors.plenary import (
    EXTRACTOR_LABEL,
    EXTRACTOR_VERSION,
)
from monitorul_ii.extraction.schema import validate

# Single-chamber plenary fixtures only — joint sessions are covered by
# test_plenary_joint_session.py
PLENARY_STENOGRAM_FIXTURES = (
    "2024-04-22_MO-PII-53-2024.md",  # Camera Deputaților, modern
    "2025-11-28_MO-PII-150-2025.md",  # Senat, modern
    "2017-01-12_MO-PII-6-2017.md",  # Mid-corpus 2017
)

# Pre-2010 fixtures exercise the v0.1.x mojibake-tolerance + implicit
# single-item agenda fallback added for the discovery-loop coverage
# recovery sweep. They use a lower coverage floor (0.50) because their
# layouts are intentionally more lossy than the modern set — no SUMAR
# ordinals, mojibake characters, body without `## **N. Title**` markers.
LEGACY_PLENARY_FIXTURES = (
    "2000-02-11_MO-PII-2-2000.md",  # Senat, mojibake (Ñ Þ ª ã)
    "2005-02-11_MO-PII-2-2005.md",  # Senat, 2005 mojibake
    "2008-09-12_MO-PII-73-2008.md",  # Senat 2008, no body N. markers
)

COVERAGE_FLOOR = 0.80
LEGACY_COVERAGE_FLOOR = 0.50


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "plenary" / name


def test_extractor_version_format():
    parts = EXTRACTOR_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_extractor_label_format():
    assert EXTRACTOR_LABEL.startswith("regex@plenary_stenogram@")


@pytest.mark.parametrize("md_name", PLENARY_STENOGRAM_FIXTURES)
def test_plenary_fixture_extracts(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    # PDF doesn't need to exist — the extractor only reads the MD
    r = extract(dst, write=False)
    assert r.status == "extract", r.reason
    assert r.doc_type == "plenary_stenogram", (
        f"{md_name} mis-classified as {r.doc_type}"
    )


@pytest.mark.parametrize("md_name", PLENARY_STENOGRAM_FIXTURES)
def test_plenary_fixture_validates(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.sidecar is not None
    validate(r.sidecar)  # raises on violation


@pytest.mark.parametrize("md_name", PLENARY_STENOGRAM_FIXTURES)
def test_plenary_fixture_coverage_floor(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.coverage_pct is not None
    assert r.coverage_pct >= COVERAGE_FLOOR, (
        f"{md_name}: coverage {r.coverage_pct:.4f} below {COVERAGE_FLOOR}"
    )


@pytest.mark.parametrize("md_name", PLENARY_STENOGRAM_FIXTURES)
def test_plenary_fixture_envelope_shape(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sc = r.sidecar
    assert sc["document_type"] == "plenary_stenogram"
    body = sc["body"]
    assert "session" in body
    assert "agenda_items" in body
    assert "interpellations" in body
    sess = body["session"]
    # PlenarySession (no chambers_present)
    assert "chambers_present" not in sess


def test_2025_11_28_senat_specifics(tmp_path: Path):
    src = _fixture_path("2025-11-28_MO-PII-150-2025.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sc = r.sidecar
    assert sc["metadata"]["chamber"] == "Senatul"
    # Should have at least one agenda item with non-empty title
    items = sc["body"]["agenda_items"]
    assert len(items) >= 1
    assert items[0]["title"]


def test_2024_04_22_chair_segments(tmp_path: Path):
    """The 2024-04-22 sample has a single-segment chair narrative."""
    src = _fixture_path("2024-04-22_MO-PII-53-2024.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sess = r.sidecar["body"]["session"]
    # Single chair: VasileDaniel Suciu
    assert any(c["name"] and "Suciu" in c["name"] for c in sess["chair"])
    # Two secretaries
    assert len(sess["secretaries"]) >= 1


def test_2017_fixture_has_outcome(tmp_path: Path):
    src = _fixture_path("2017-01-12_MO-PII-6-2017.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sess = r.sidecar["body"]["session"]
    # Either completed or some other outcome — should not be None for a
    # well-formed session
    assert sess["outcome"] is not None or sess["closed_at"] is not None


# -- legacy (pre-2010) fixtures --------------------------------------------
#
# Cover the v0.1.x discovery-loop recovery: pre-2010 docs exercise mojibake
# tolerance and the implicit-single-item agenda fallback. Floor is 0.50 (vs
# 0.80 on modern fixtures) because their layouts are intentionally lossier.


@pytest.mark.parametrize("md_name", LEGACY_PLENARY_FIXTURES)
def test_legacy_plenary_fixture_extracts(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.status == "extract", r.reason
    assert r.doc_type in ("plenary_stenogram", "plenary_joint_session"), (
        f"{md_name} unexpectedly classified as {r.doc_type}"
    )


@pytest.mark.parametrize("md_name", LEGACY_PLENARY_FIXTURES)
def test_legacy_plenary_fixture_validates(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.sidecar is not None
    validate(r.sidecar)


@pytest.mark.parametrize("md_name", LEGACY_PLENARY_FIXTURES)
def test_legacy_plenary_fixture_coverage_floor(md_name: str, tmp_path: Path):
    src = _fixture_path(md_name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    assert r.coverage_pct is not None
    assert r.coverage_pct >= LEGACY_COVERAGE_FLOOR, (
        f"{md_name}: coverage {r.coverage_pct:.4f} below {LEGACY_COVERAGE_FLOOR}"
    )


def test_2000_fixture_handles_mojibake(tmp_path: Path):
    """The 2000 doc carries `Ñ Þ þ ª º ã` mojibake throughout. The extractor
    must still produce a non-empty body via the implicit single-item
    fallback (no `## **N.**` body markers in this layout)."""
    src = _fixture_path("2000-02-11_MO-PII-2-2000.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    items = r.sidecar["body"]["agenda_items"]
    assert len(items) >= 1
    # The implicit fallback wraps the body as a single `category="other"`
    # item; a SUMAR-driven path on this doc would have multiple items.
    # Either is acceptable for the assertion.
    assert any(it["activities"] for it in items)


def test_2008_fixture_has_chair_block(tmp_path: Path):
    """2008-era docs use `Lucrările ședinței au fost conduse de...` (the
    `ședinței` insertion). The relaxed chair-block regex must catch it."""
    src = _fixture_path("2008-09-12_MO-PII-73-2008.md")
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = extract(dst, write=False)
    sess = r.sidecar["body"]["session"]
    # Chair block detected → at least one chair entry
    assert len(sess["chair"]) >= 1
