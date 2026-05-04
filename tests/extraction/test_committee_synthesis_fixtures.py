"""End-to-end tests for the committee_synthesis extractor + per-fixture
coverage assertions per Q9 (test floor 0.80).

Four fixtures span the corpus eras:

  - 2008 (1c/2008)  — pre-pandemic, narrative agenda, 2008-era cedilla
                      glyphs and occasional `�` mojibake on PRE�EDINTE
                      (signature still extracts via the diacritic-tolerant
                      regex).
  - 2018 (1c/2018)  — 20-committee modern docs, narrative agenda, clean
                      Unicode.
  - 2022 (1c/2022)  — pandemic-era, 17 committees, mixed format markers,
                      `audiere ... candidat` purpose markers.
  - 2025 (28c/2025) — heavily tabular agenda + roster, 20 committees, the
                      hardest layout for narrative-tuned extractors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.extraction import extract
from monitorul_ii.extraction.extractors.committee_synthesis import (
    EXTRACTOR_LABEL,
    EXTRACTOR_VERSION,
)
from monitorul_ii.extraction.schema import validate

COMMITTEE_SYNTHESIS_FIXTURES = (
    "2008-02-05_MO-PII-1c-2008.md",
    "2018-01-05_MO-PII-1c-2018.md",
    "2022-01-04_MO-PII-1c-2022.md",
    "2025-08-12_MO-PII-28c-2025.md",
)

COVERAGE_FLOOR = 0.80


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "committee_synthesis" / name


def _isolated(name: str, tmp_path: Path) -> Path:
    src = _fixture_path(name)
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def test_extractor_version_format():
    parts = EXTRACTOR_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_extractor_label_format():
    assert EXTRACTOR_LABEL.startswith("regex@committee_synthesis@")


@pytest.mark.parametrize("md_name", COMMITTEE_SYNTHESIS_FIXTURES)
def test_committee_fixture_extracts(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    assert r.status == "extract", r.reason
    assert r.doc_type == "committee_synthesis", (
        f"{md_name} mis-classified as {r.doc_type}"
    )


@pytest.mark.parametrize("md_name", COMMITTEE_SYNTHESIS_FIXTURES)
def test_committee_fixture_validates(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    assert r.sidecar is not None
    validate(r.sidecar)


@pytest.mark.parametrize("md_name", COMMITTEE_SYNTHESIS_FIXTURES)
def test_committee_fixture_coverage_floor(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    assert r.coverage_pct is not None
    assert r.coverage_pct >= COVERAGE_FLOOR, (
        f"{md_name}: coverage {r.coverage_pct:.4f} below {COVERAGE_FLOOR}"
    )


@pytest.mark.parametrize("md_name", COMMITTEE_SYNTHESIS_FIXTURES)
def test_committee_fixture_envelope_shape(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    sc = r.sidecar
    assert sc["document_type"] == "committee_synthesis"
    body = sc["body"]
    assert "period" in body
    assert "committees" in body
    # Every fixture has at least one committee
    assert len(body["committees"]) >= 1
    # Every committee has the strict required keys
    for c in body["committees"]:
        assert set(c.keys()) >= {
            "name",
            "kind",
            "chair",
            "secretary",
            "meetings",
            "source_span",
            "extraction",
        }
        # exactly one meeting per committee in v0.1
        assert len(c["meetings"]) == 1


@pytest.mark.parametrize("md_name", COMMITTEE_SYNTHESIS_FIXTURES)
def test_committee_fixture_versions_block(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    versions = r.sidecar["extraction"]["extractor_versions"]
    assert "boilerplate" in versions
    assert "speakers" in versions
    assert "references" in versions
    assert "committee_synthesis" in versions
    assert versions["committee_synthesis"] == EXTRACTOR_VERSION


def test_2025_28c_specifics(tmp_path: Path):
    """2025/28c: 20 committees, period 2-5.06.2025, mostly-mixed format,
    chair extracted on most blocks."""
    md_path = _isolated("2025-08-12_MO-PII-28c-2025.md", tmp_path)
    r = extract(md_path, write=False)
    body = r.sidecar["body"]
    assert body["period"]["start"] == "2025-06-02"
    assert body["period"]["end"] == "2025-06-05"
    committees = body["committees"]
    assert len(committees) == 20
    # First committee is buget, finanțe și bănci with chair Bogdan-Iulian Huțucă
    first = committees[0]
    assert "buget" in first["name"].lower()
    assert first["chair"] is not None
    assert first["chair"]["name"] == "Bogdan-Iulian Huțucă"
    # Most blocks should have a chair extracted (≥80% of 20)
    chairs_present = sum(1 for c in committees if c["chair"])
    assert chairs_present >= 16, f"only {chairs_present}/20 chairs extracted"


def test_2018_1c_specifics(tmp_path: Path):
    """2018/1c: 20-committee narrative-agenda doc; the canonical
    structurally-clean modern format. Period 2017-11-20 to 2017-11-23."""
    md_path = _isolated("2018-01-05_MO-PII-1c-2018.md", tmp_path)
    r = extract(md_path, write=False)
    body = r.sidecar["body"]
    assert body["period"]["start"] == "2017-11-20"
    assert body["period"]["end"] == "2017-11-23"
    committees = body["committees"]
    # 20 standing committees in the synthesis
    assert len(committees) == 20
    # The first is `politică economică, reformă și privatizare` with chair
    # Laurențiu Nistor
    first = committees[0]
    assert "politic" in first["name"].lower() and "economic" in first["name"].lower()
    assert first["chair"] is not None
    assert "Laurențiu Nistor" in first["chair"]["name"]


def test_2022_1c_audiere_purpose(tmp_path: Path):
    """2022/1c: pandemic-era doc with `audiere candidat` blocks. The
    purpose detector should pick those up."""
    md_path = _isolated("2022-01-04_MO-PII-1c-2022.md", tmp_path)
    r = extract(md_path, write=False)
    body = r.sidecar["body"]
    purposes = [c["meetings"][0]["purpose"] for c in body["committees"]]
    # At least one block flagged audiere_candidați
    assert "audiere_candidați" in purposes


def test_2008_1c_mojibake_signature(tmp_path: Path):
    """2008/1c: pre-2010 era with `PRE�EDINTE` mojibake. The diacritic-
    tolerant signature regex catches it."""
    md_path = _isolated("2008-02-05_MO-PII-1c-2008.md", tmp_path)
    r = extract(md_path, write=False)
    body = r.sidecar["body"]
    # Most committees have at least a chair OR a secretary — the
    # 13-committee 2008 doc has 9 chairs extracted.
    chairs = sum(1 for c in body["committees"] if c["chair"])
    secretaries = sum(1 for c in body["committees"] if c["secretary"])
    assert chairs + secretaries >= len(body["committees"]) // 2, (
        f"signatures too sparse: {chairs} chairs / {secretaries} secretaries / "
        f"{len(body['committees'])} committees"
    )


def test_committee_synthesis_fixture_kind_classifier(tmp_path: Path):
    """Every fixture's committees should classify into the documented
    enum (mostly `permanent`)."""
    for name in COMMITTEE_SYNTHESIS_FIXTURES:
        md_path = _isolated(name, tmp_path)
        r = extract(md_path, write=False)
        for c in r.sidecar["body"]["committees"]:
            assert c["kind"] in {
                "permanent",
                "special",
                "inquiry",
                "special_joint",
                "inquiry_joint",
                None,
            }, f"{name}: committee {c['name']!r} has kind={c['kind']!r}"


def test_committee_synthesis_fixture_period_present(tmp_path: Path):
    """Every fixture should yield a non-null period (we know the corpus
    has the `Perioada: …` header on every cohort)."""
    for name in COMMITTEE_SYNTHESIS_FIXTURES:
        md_path = _isolated(name, tmp_path)
        r = extract(md_path, write=False)
        period = r.sidecar["body"]["period"]
        assert period["start"] is not None, f"{name}: period.start is null"
        assert period["end"] is not None, f"{name}: period.end is null"
