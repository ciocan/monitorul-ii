"""End-to-end tests for the report_facsimile extractor + per-fixture
coverage assertions.

Nine fixtures span the cohort eras, institutional sources, and surface
forms (v0.2.0 added five fixtures for the recovered title/issuing-body
variants):

  - 2014/1R    — CSAT 2010 report; image-only PDF (page-residue body); the
                 edge case for content-span coverage.
  - 2014/13R   — SRI 2007 report; modern body with H2 headings.
  - 2017/1R    — Consiliul Legislativ 2010 report; mid-cohort, cleaner
                 heading outline.
  - 2024/1R    — ANCOM 2019 report; modern Camera-published, 100+ headings,
                 full prose body. Hardest layout for the heading detector.

  v0.2.0 additions (regression guards for the recovered surface forms):

  - 2014/19R   — AEP 2012 local-elections; SUMAR ends in `din iunie 2012`
                 (month-only date form) + `Raportul X asupra <topic>`
                 issuing-body pattern.
  - 2014/21R   — AEP 2012 referendum; SUMAR ends in `din 29 iulie 2012`
                 (DD-month date form) + `Raportul X privind <topic>`
                 issuing-body pattern. Body is mojibake (image-only OCR)
                 — recovery happens entirely from the clean SUMAR row.
  - 2016/4R    — ANCOM 2010 report; SUMAR ends in `pentru anul 2010` and
                 the body is mojibake. Pre-v0.2 the title regex rejected
                 the SUMAR row tail, fell through to a deeper mojibake
                 line; v0.2 picks the SUMAR row cleanly.
  - 2016/13R   — ANRE 2013 report; SUMAR title body has `<br>pe anul 2013`
                 — `<br>` linebreak between body and tail. Pre-v0.2 the
                 issuing_body regex's `\\s+` boundary rejected it; v0.2
                 strips `<br>` in `_clean_title` and `_extract_issuing_body`.
  - 2016/19R   — AEP 2013 activity; SUMAR uses `Raportul privind
                 activitatea X` (without the SRI-form `desfășurată de`).
                 v0.2 added pattern 2 (negative-lookahead) to handle this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.extraction import extract
from monitorul_ii.extraction.extractors.report_facsimile import (
    EXTRACTOR_LABEL,
    EXTRACTOR_VERSION,
)
from monitorul_ii.extraction.schema import validate

REPORT_FACSIMILE_FIXTURES = (
    "2014-01-20_MO-PII-1R-2014.md",
    "2014-04-24_MO-PII-13R-2014.md",
    "2014-06-16_MO-PII-19R-2014.md",
    "2014-06-17_MO-PII-21R-2014.md",
    "2016-05-23_MO-PII-4R-2016.md",
    "2016-05-30_MO-PII-13R-2016.md",
    "2016-07-27_MO-PII-19R-2016.md",
    "2017-10-24_MO-PII-1R-2017.md",
    "2024-04-09_MO-PII-1R-2024.md",
)

COVERAGE_FLOOR = 0.80


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "report_facsimile" / name


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
    assert EXTRACTOR_LABEL.startswith("regex@report_facsimile@")


@pytest.mark.parametrize("md_name", REPORT_FACSIMILE_FIXTURES)
def test_fixture_extracts(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    assert r.status == "extract", r.reason
    assert r.doc_type == "report_facsimile", f"{md_name} mis-classified as {r.doc_type}"


@pytest.mark.parametrize("md_name", REPORT_FACSIMILE_FIXTURES)
def test_fixture_validates(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    assert r.sidecar is not None
    validate(r.sidecar)


@pytest.mark.parametrize("md_name", REPORT_FACSIMILE_FIXTURES)
def test_fixture_coverage_floor(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    assert r.coverage_pct is not None
    assert r.coverage_pct >= COVERAGE_FLOOR, (
        f"{md_name}: coverage {r.coverage_pct:.4f} below {COVERAGE_FLOOR}"
    )


@pytest.mark.parametrize("md_name", REPORT_FACSIMILE_FIXTURES)
def test_fixture_envelope_shape(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    sc = r.sidecar
    assert sc["document_type"] == "report_facsimile"
    body = sc["body"]
    assert set(body.keys()) == {"report", "headings", "raw_markdown_excerpt"}
    rep = body["report"]
    assert set(rep.keys()) == {
        "title",
        "issuing_body",
        "issuing_body_normalized",
        "reporting_period",
        "received_at",
    }


@pytest.mark.parametrize("md_name", REPORT_FACSIMILE_FIXTURES)
def test_fixture_versions_block(md_name: str, tmp_path: Path):
    md_path = _isolated(md_name, tmp_path)
    r = extract(md_path, write=False)
    versions = r.sidecar["extraction"]["extractor_versions"]
    assert "report_facsimile" in versions
    assert versions["report_facsimile"] == EXTRACTOR_VERSION


def test_2014_csat_specifics(tmp_path: Path):
    """2014/1R: CSAT report on 2010 activity, received in joint session
    on 2013-12-04. Image-only body — heading list is empty."""
    md_path = _isolated("2014-01-20_MO-PII-1R-2014.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert "Consiliului Suprem de Apărare" in rep["title"]
    assert rep["issuing_body"] == "Consiliului Suprem de Apărare a Țării"
    assert rep["reporting_period"] == {"start": "2010-01-01", "end": "2010-12-31"}
    assert rep["received_at"]["session_kind"] == "joint"
    assert rep["received_at"]["session_date"] == "2013-12-04"
    # raw_markdown_excerpt is the first 500 chars of body
    assert len(r.sidecar["body"]["raw_markdown_excerpt"]) <= 500


def test_2017_consiliul_legislativ_specifics(tmp_path: Path):
    """2017/1R: Consiliul Legislativ 2010 report; uses
    `Raport asupra activității desfășurate de X` form."""
    md_path = _isolated("2017-10-24_MO-PII-1R-2017.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert rep["issuing_body"] == "Consiliul Legislativ"
    assert rep["reporting_period"]["start"] == "2010-01-01"


def test_2024_ancom_specifics(tmp_path: Path):
    """2024/1R: ANCOM 2019 report; modern doc with rich heading outline."""
    md_path = _isolated("2024-04-09_MO-PII-1R-2024.md", tmp_path)
    r = extract(md_path, write=False)
    body = r.sidecar["body"]
    rep = body["report"]
    assert "ANCOM" in rep["issuing_body"] or "Comunicații" in rep["issuing_body"]
    assert rep["reporting_period"] == {"start": "2019-01-01", "end": "2019-12-31"}
    # Modern doc has lots of headings
    assert len(body["headings"]) >= 20


def test_2014_sri_has_headings(tmp_path: Path):
    """2014/13R: SRI 2007 report has H2 headings (CAPITOLUL I, etc.) — the
    heading extractor should pick them up."""
    md_path = _isolated("2014-04-24_MO-PII-13R-2014.md", tmp_path)
    r = extract(md_path, write=False)
    body = r.sidecar["body"]
    assert len(body["headings"]) >= 5
    # The heading list should not include the reception session line
    texts = " ".join(h["text"] for h in body["headings"])
    assert "Ședința din ziua de" not in texts
    assert "EDITOR" not in texts


# -- v0.2.0 recovered-surface-form fixtures ------------------------------


def test_2014_aep_19R_din_month_year_form(tmp_path: Path):
    """2014/19R: AEP local-elections 2012; SUMAR `din iunie 2012` form +
    `Raportul X asupra <topic>` issuing-body pattern. v0.2.1's date-form
    fallback recovers the reporting year from the title's date."""
    md_path = _isolated("2014-06-16_MO-PII-19R-2014.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert "Autorității Electorale Permanente" in rep["title"]
    assert "din iunie 2012" in rep["title"]
    assert rep["issuing_body"] == "Autorității Electorale Permanente"
    assert rep["reporting_period"] == {"start": "2012-01-01", "end": "2012-12-31"}


def test_2014_aep_21R_din_dd_month_year_form(tmp_path: Path):
    """2014/21R: AEP referendum 2012; SUMAR `din 29 iulie 2012` form +
    `Raportul X privind <topic>` issuing-body pattern. The body is
    mojibake (image-only OCR) — recovery is entirely from the SUMAR row."""
    md_path = _isolated("2014-06-17_MO-PII-21R-2014.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert rep["title"] is not None
    assert "din 29 iulie 2012" in rep["title"]
    assert rep["issuing_body"] == "Autorității Electorale Permanente"
    assert rep["reporting_period"] == {"start": "2012-01-01", "end": "2012-12-31"}


def test_2016_ancom_4R_pentru_anul_form(tmp_path: Path):
    """2016/4R: ANCOM 2010 report; SUMAR ends in `pentru anul 2010` and the
    body is mojibake. Pre-v0.2 the title regex rejected the SUMAR tail and
    fell through to a deeper mojibake line; v0.2 picks the SUMAR cleanly."""
    md_path = _isolated("2016-05-23_MO-PII-4R-2016.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert rep["title"] is not None
    assert rep["title"].endswith("pentru anul 2010")
    assert "ANCOM" in rep["issuing_body"] or "Comunicații" in rep["issuing_body"]
    # Reporting period uses `pentru anul YYYY` form (v0.2.0 extension).
    assert rep["reporting_period"] == {"start": "2010-01-01", "end": "2010-12-31"}


def test_2016_anre_13R_br_residue_strip(tmp_path: Path):
    """2016/13R: ANRE 2013 report; SUMAR title body has `<br>pe anul 2013`
    — `<br>` linebreak between body and tail. v0.2 strips `<br>` in
    `_clean_title` and `_extract_issuing_body` so neither the canonical
    title nor the issuing-body regex carry the HTML residue."""
    md_path = _isolated("2016-05-30_MO-PII-13R-2016.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert rep["title"] is not None
    assert "<br>" not in rep["title"]
    assert "Domeniul Energiei" in rep["issuing_body"]


def test_2016_aep_19R_privind_activitatea_form(tmp_path: Path):
    """2016/19R: AEP 2013 activity report; uses `Raportul privind
    activitatea X în anul Y` *without* the SRI-form `desfășurată de`.
    v0.2 pattern 2 (negative-lookahead) catches this."""
    md_path = _isolated("2016-07-27_MO-PII-19R-2016.md", tmp_path)
    r = extract(md_path, write=False)
    rep = r.sidecar["body"]["report"]
    assert (
        rep["title"]
        == "Raportul privind activitatea Autorității Electorale Permanente în anul 2013"
    )
    assert rep["issuing_body"] == "Autorității Electorale Permanente"
