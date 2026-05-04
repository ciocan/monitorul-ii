"""End-to-end tests of the extract dispatcher, plus golden-file checks.

Goldens regenerate when `UPDATE_GOLDEN=1` is set:
    UPDATE_GOLDEN=1 uv run pytest tests/extraction -k golden
The golden mechanism strips fields that vary per-run (`extracted_at`, the
absolute paths in `raw_markdown_path` / `raw_pdf_path`).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from monitorul_ii.extraction import extract
from monitorul_ii.extraction.pipeline import (
    EXTRACTOR_LABEL,
    SCHEMA_VERSION,
    _versions_current,
    extract as extract_fn,
)
from monitorul_ii.extraction.schema import validate

FIXTURE_NAMES = (
    "qr_2013-07-11_93.md",
    "qr_2019-09-03_91.md",
    "qr_2026-03-25_29.md",
)


def _normalise_for_golden(sidecar: dict[str, Any]) -> dict[str, Any]:
    """Strip per-run-volatile fields before golden compare."""
    sc = json.loads(json.dumps(sidecar))  # deep copy via json round-trip
    sc["extraction"].pop("extracted_at", None)
    # Paths vary by tmp_path; only assert they end with the expected basename
    for key in ("raw_markdown_path", "raw_pdf_path"):
        sc[key] = Path(sc[key]).name
    return sc


def _golden_path(fixtures_dir: Path, md_name: str) -> Path:
    return fixtures_dir / md_name.replace(".md", ".expected.json")


@pytest.mark.parametrize("md_name", FIXTURE_NAMES)
def test_golden_qr_fixture(
    md_name: str, fixtures_dir: Path, isolated_md, tmp_path: Path
) -> None:
    md_path = isolated_md(md_name)
    result = extract(md_path, write=False)
    assert result.status == "extract", result.reason
    assert result.sidecar is not None

    actual = _normalise_for_golden(result.sidecar)
    golden = _golden_path(fixtures_dir, md_name)

    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(
            json.dumps(actual, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return

    if not golden.exists():
        pytest.skip(
            f"golden file missing: {golden.name}; run "
            f"UPDATE_GOLDEN=1 uv run pytest to create it"
        )

    expected = json.loads(golden.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"golden mismatch for {md_name}; rerun with UPDATE_GOLDEN=1 if "
        "the change is intentional"
    )


@pytest.mark.parametrize("md_name", FIXTURE_NAMES)
def test_extracted_sidecar_validates(md_name: str, isolated_md) -> None:
    md_path = isolated_md(md_name)
    result = extract(md_path, write=False)
    assert result.status == "extract"
    validate(result.sidecar)  # raises on violation


@pytest.mark.parametrize("md_name", FIXTURE_NAMES)
def test_coverage_above_minimum_threshold(md_name: str, isolated_md) -> None:
    """Every fixture should hit at least 95% coverage. Tightening this
    over time is how we know the extractor is improving."""
    md_path = isolated_md(md_name)
    result = extract(md_path, write=False)
    assert result.coverage_pct is not None
    assert result.coverage_pct >= 0.95, (
        f"{md_name}: coverage {result.coverage_pct:.4f} below 0.95 threshold"
    )


@pytest.mark.parametrize("md_name", FIXTURE_NAMES)
def test_envelope_top_level_shape(md_name: str, isolated_md) -> None:
    md_path = isolated_md(md_name)
    sc = extract(md_path, write=False).sidecar
    assert sc["schema_version"] == SCHEMA_VERSION
    assert sc["document_type"] == "question_register"
    assert sc["extraction"]["extractor"] == "regex@1"
    versions = sc["extraction"]["extractor_versions"]
    assert "boilerplate" in versions
    assert "speakers" in versions
    assert "question_register" in versions


def test_dispatch_skips_unimplemented_types(isolated_md, monkeypatch) -> None:
    """The skip-with-reason contract for unimplemented types: when a doc
    classifies as a type without an extractor registered, it must skip with
    reason — not produce a stub `body=other` sidecar. v1.8.0 ships extractors
    for all 6 types; we monkeypatch EXTRACTORS to drop one and verify the
    contract still holds for any future type that ships before its
    extractor.
    """
    import monitorul_ii.extraction.extractors as extractors_pkg

    monkeypatch.delitem(extractors_pkg.EXTRACTORS, "report_facsimile")
    md_path = isolated_md("qr_2026-03-25_29.md")
    result = extract(md_path, override_type="report_facsimile", write=False)
    assert result.status == "skip"
    assert "no extractor" in (result.reason or "")
    assert result.sidecar is None


def test_force_re_extracts_even_when_versions_match(
    isolated_md, tmp_path: Path
) -> None:
    md_path = isolated_md("qr_2026-03-25_29.md")
    # First run writes the sidecar
    r1 = extract(md_path, force=False)
    assert r1.status == "extract"
    sidecar_path = r1.sidecar_path
    assert sidecar_path.exists()
    # Second run with force=False skips (versions match)
    r2 = extract(md_path, force=False, write=True)
    assert r2.status == "skip"
    assert r2.reason == "versions match"
    # force=True re-extracts
    r3 = extract(md_path, force=True, write=True)
    assert r3.status == "extract"


def test_versions_current_detects_schema_version_mismatch():
    cached = {
        "schema_version": "1.4.0",  # older
        "extraction": {"extractor_versions": {}},
    }
    assert not _versions_current(cached, "question_register")


def test_versions_current_detects_helper_version_mismatch():
    cached = {
        "schema_version": SCHEMA_VERSION,
        "extraction": {
            "extractor_versions": {
                "boilerplate": "9.9.9",  # nonsense version
                "coverage": "0.1.0",
                "references": "0.2.0",
                "speakers": "0.2.0",
                "topics": "0.1.0",
                "question_register": "0.1.0",
            }
        },
    }
    assert not _versions_current(cached, "question_register")


def test_extractor_label_format():
    # `regex@N` matches the schema's expected pattern (`extraction.extractor`
    # is just type=string, but the format is conventional).
    assert re.match(r"^regex@\d+$", EXTRACTOR_LABEL)


def test_missing_md_returns_error(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist.md"
    r = extract(nonexistent, write=False)
    assert r.status == "error"
    assert "missing" in (r.reason or "").lower()


def test_malformed_frontmatter_returns_error(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    p.write_text("---\nincomplete frontmatter without close\nbody\n", encoding="utf-8")
    r = extract(p, write=False)
    assert r.status == "error"


def test_atomic_write_no_part_files(isolated_md) -> None:
    md_path = isolated_md("qr_2026-03-25_29.md")
    extract(md_path, force=True, write=True)
    # `.part` rename is the atomicity contract; no leftover part file
    leftover = md_path.parent / f"{md_path.stem}.extraction.json.part"
    assert not leftover.exists()


def test_overall_extract_reexports_extract_fn() -> None:
    # `from monitorul_ii.extraction import extract` reaches the same callable
    assert extract is extract_fn
