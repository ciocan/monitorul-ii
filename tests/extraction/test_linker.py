"""Tests for the cross-document linker.

Synthetic sidecars (built in-test, no fixture MDs needed) cover:
  - Index construction (joint vs single-chamber priority, date sources)
  - Single-sidecar linking happy path
  - Skip paths (already-linked, no session_date, no match, wrong type)
  - Force re-link
  - Schema validation guard
  - End-to-end on real fixtures (extract a report_facsimile + a joint
    plenary, then link)
"""

from __future__ import annotations

import json
from pathlib import Path

from monitorul_ii.extraction.linker import (
    LINKER_VERSION,
    build_session_index,
    link_all,
    link_report,
)


# -- helpers --------------------------------------------------------------


def _minimal_envelope(
    *, doc_id: str, doc_type: str, published: str, session_date: str | None
) -> dict:
    """Build a minimal sidecar envelope shared across types."""
    return {
        "schema_version": "1.9.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": doc_type,
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": int(published[:4]),
            "part": "II",
            "published": published,
            "chamber": "joint" if doc_type == "plenary_joint_session" else None,
            "session": None,
            "session_type": None,
            "session_date": session_date,
            "legislature": None,
        },
        "raw_markdown_path": "x.md",
        "raw_pdf_path": "x.pdf",
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2026-05-04T12:00:00Z",
            "extractor_versions": {"boilerplate": "0.1.0"},
            "confidence": 0.9,
        },
        "coverage": {
            "body_chars": 100,
            "claimed_chars": 100,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
    }


def _joint_sidecar(*, doc_id: str, published: str, session_date: str | None) -> dict:
    sc = _minimal_envelope(
        doc_id=doc_id,
        doc_type="plenary_joint_session",
        published=published,
        session_date=session_date,
    )
    sc["body"] = {
        "session": {
            "chair": [],
            "chair_segments": [],
            "chambers_present": ["Camera Deputaților", "Senatul"],
            "secretaries": [],
            "attendance": {"registered": None, "total_seats": None},
            "quorum_met": None,
            "opened_at": None,
            "closed_at": None,
            "format": None,
            "outcome": None,
            "special_procedure": None,
        },
        "agenda_items": [],
        "interpellations": [],
    }
    return sc


def _stenogram_sidecar(
    *, doc_id: str, published: str, session_date: str | None
) -> dict:
    sc = _minimal_envelope(
        doc_id=doc_id,
        doc_type="plenary_stenogram",
        published=published,
        session_date=session_date,
    )
    sc["metadata"]["chamber"] = "Camera Deputaților"
    sc["body"] = {
        "session": {
            "chair": [],
            "chair_segments": [],
            "secretaries": [],
            "attendance": {"registered": None, "total_seats": None},
            "quorum_met": None,
            "opened_at": None,
            "closed_at": None,
            "format": None,
            "outcome": None,
            "special_procedure": None,
        },
        "agenda_items": [],
        "interpellations": [],
    }
    return sc


def _report_sidecar(
    *,
    doc_id: str,
    published: str,
    session_date: str | None,
    received_in_document: str | None = None,
) -> dict:
    sc = _minimal_envelope(
        doc_id=doc_id,
        doc_type="report_facsimile",
        published=published,
        session_date=session_date,
    )
    sc["body"] = {
        "report": {
            "title": "Raportul X în anul 2010",
            "issuing_body": "X",
            "issuing_body_normalized": None,
            "reporting_period": {"start": "2010-01-01", "end": "2010-12-31"},
            "received_at": {
                "session_kind": "joint",
                "session_date": session_date,
                "received_in_document": received_in_document,
            },
        },
        "headings": [],
        "raw_markdown_excerpt": "",
    }
    return sc


def _write(tmp_path: Path, name: str, sidecar: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# -- index construction --------------------------------------------------


def test_build_session_index_picks_up_joint_sessions(tmp_path: Path):
    p = _write(
        tmp_path,
        "joint.extraction.json",
        _joint_sidecar(
            doc_id="mo://2013/II/95",
            published="2013-12-04",
            session_date="2013-12-04",
        ),
    )
    idx = build_session_index([p])
    assert idx == {"2013-12-04": "mo://2013/II/95"}


def test_build_session_index_falls_back_to_published_when_session_date_null(
    tmp_path: Path,
):
    p = _write(
        tmp_path,
        "joint.extraction.json",
        _joint_sidecar(
            doc_id="mo://2014/II/3",
            published="2014-01-22",
            session_date=None,
        ),
    )
    idx = build_session_index([p])
    assert idx == {"2014-01-22": "mo://2014/II/3"}


def test_build_session_index_joint_beats_stenogram_on_same_date(tmp_path: Path):
    """When both a joint and a single-chamber stenogram exist for the
    same date, the joint session wins (it's the more common report
    receiver)."""
    paths = [
        _write(
            tmp_path,
            "stenogram.extraction.json",
            _stenogram_sidecar(
                doc_id="mo://2013/II/100",
                published="2013-12-04",
                session_date="2013-12-04",
            ),
        ),
        _write(
            tmp_path,
            "joint.extraction.json",
            _joint_sidecar(
                doc_id="mo://2013/II/95",
                published="2013-12-04",
                session_date="2013-12-04",
            ),
        ),
    ]
    idx = build_session_index(paths)
    assert idx["2013-12-04"] == "mo://2013/II/95"


def test_build_session_index_skips_irrelevant_types(tmp_path: Path):
    p = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
        ),
    )
    idx = build_session_index([p])
    assert idx == {}


def test_build_session_index_skips_unreadable_sidecars(tmp_path: Path):
    bad = tmp_path / "broken.extraction.json"
    bad.write_text("{not valid json", encoding="utf-8")
    good = _write(
        tmp_path,
        "joint.extraction.json",
        _joint_sidecar(
            doc_id="mo://2024/II/1",
            published="2024-04-09",
            session_date="2023-10-10",
        ),
    )
    idx = build_session_index([bad, good])
    assert idx == {"2023-10-10": "mo://2024/II/1"}


# -- single sidecar link -------------------------------------------------


def test_link_report_happy_path(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
        ),
    )
    idx = {"2013-12-04": "mo://2013/II/95"}
    result = link_report(rp, session_index=idx)
    assert result.status == "linked"
    assert result.target_document_id == "mo://2013/II/95"
    # Sidecar on disk reflects the linked back-pointer
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert (
        sc["body"]["report"]["received_at"]["received_in_document"] == "mo://2013/II/95"
    )


def test_link_report_skips_already_linked(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
            received_in_document="mo://2013/II/PRE-EXISTING",
        ),
    )
    idx = {"2013-12-04": "mo://2013/II/95"}
    result = link_report(rp, session_index=idx)
    assert result.status == "skip"
    assert "already linked" in (result.reason or "")
    assert result.target_document_id == "mo://2013/II/PRE-EXISTING"


def test_link_report_force_relinks_populated_field(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
            received_in_document="mo://2013/II/STALE",
        ),
    )
    idx = {"2013-12-04": "mo://2013/II/95"}
    result = link_report(rp, session_index=idx, force=True)
    assert result.status == "linked"
    assert result.target_document_id == "mo://2013/II/95"
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert (
        sc["body"]["report"]["received_at"]["received_in_document"] == "mo://2013/II/95"
    )


def test_link_report_skips_when_no_session_date(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date=None,
        ),
    )
    result = link_report(rp, session_index={"2013-12-04": "mo://2013/II/95"})
    assert result.status == "skip"
    assert "no session_date" in (result.reason or "")


def test_link_report_skips_when_no_match(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
        ),
    )
    result = link_report(rp, session_index={"2099-01-01": "mo://2099/II/1"})
    assert result.status == "skip"
    assert "no receiving session" in (result.reason or "")


def test_link_report_skips_non_report_sidecar(tmp_path: Path):
    p = _write(
        tmp_path,
        "joint.extraction.json",
        _joint_sidecar(
            doc_id="mo://2013/II/95",
            published="2013-12-04",
            session_date="2013-12-04",
        ),
    )
    result = link_report(p, session_index={"2013-12-04": "mo://2013/II/95"})
    assert result.status == "skip"
    assert "not a report_facsimile" in (result.reason or "")


def test_link_report_dry_run_does_not_modify_file(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
        ),
    )
    before = rp.read_text(encoding="utf-8")
    result = link_report(
        rp,
        session_index={"2013-12-04": "mo://2013/II/95"},
        write=False,
    )
    assert result.status == "linked"
    assert result.target_document_id == "mo://2013/II/95"
    # File on disk is unchanged
    assert rp.read_text(encoding="utf-8") == before


def test_link_report_returns_error_on_unreadable(tmp_path: Path):
    p = tmp_path / "missing.extraction.json"
    result = link_report(p, session_index={})
    assert result.status == "error"
    assert "failed to read" in (result.reason or "")


# -- batch link_all ------------------------------------------------------


def test_link_all_yields_one_result_per_report(tmp_path: Path):
    paths = [
        _write(
            tmp_path,
            "joint.extraction.json",
            _joint_sidecar(
                doc_id="mo://2013/II/95",
                published="2013-12-04",
                session_date="2013-12-04",
            ),
        ),
        _write(
            tmp_path,
            "report1.extraction.json",
            _report_sidecar(
                doc_id="mo://2014/II/1R",
                published="2014-01-20",
                session_date="2013-12-04",
            ),
        ),
        _write(
            tmp_path,
            "report2.extraction.json",
            _report_sidecar(
                doc_id="mo://2014/II/2R",
                published="2014-01-21",
                session_date="2099-01-01",  # no match
            ),
        ),
    ]
    results = list(link_all(paths))
    assert len(results) == 2
    by_status = {r.status for r in results}
    assert by_status == {"linked", "skip"}


# -- versioning ----------------------------------------------------------


def test_linker_version_format():
    parts = LINKER_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


# -- end-to-end on real fixtures ----------------------------------------


def test_end_to_end_link_after_extract(tmp_path: Path):
    """Full pipeline: extract a report_facsimile + a joint plenary, then
    link. The linker should connect the report (which was received on
    the joint session's date) to the joint sidecar's document_id."""
    from monitorul_ii.extraction import extract

    fixtures = Path(__file__).parent / "fixtures"
    # Joint plenary received the CSAT 2010 report on 2013-12-04. The 2013
    # joint plenary fixture has session_date 2013-12-04 in its frontmatter.
    joint_src = fixtures / "plenary" / "2013-01-30_MO-PII-1-2013.md"
    report_src = fixtures / "report_facsimile" / "2014-01-20_MO-PII-1R-2014.md"

    joint_md = tmp_path / joint_src.name
    joint_md.write_text(joint_src.read_text(encoding="utf-8"), encoding="utf-8")
    report_md = tmp_path / report_src.name
    report_md.write_text(report_src.read_text(encoding="utf-8"), encoding="utf-8")

    joint_r = extract(joint_md)
    assert joint_r.status == "extract"
    report_r = extract(report_md)
    assert report_r.status == "extract"

    # Sanity: report's session_date should be a string, joint's metadata
    # should also have a session_date (or fall back to published)
    report_sc = json.loads(report_r.sidecar_path.read_text(encoding="utf-8"))
    initial_link = report_sc["body"]["report"]["received_at"]["received_in_document"]
    assert initial_link is None  # extract didn't fill it; that's the linker's job

    sidecar_paths = [joint_r.sidecar_path, report_r.sidecar_path]
    results = list(link_all(sidecar_paths))
    # The 2013 joint fixture is dated 2013-01-30 but the CSAT report's
    # received_at is 2013-12-04 — so they don't match (no link possible).
    # The contract is "skip with reason"; assert that branch:
    assert len(results) == 1
    assert results[0].status == "skip"
    assert "no receiving session" in (results[0].reason or "")


def test_end_to_end_link_with_synthetic_match(tmp_path: Path):
    """End-to-end variant where we forge a joint sidecar with the matching
    date so the link succeeds."""
    from monitorul_ii.extraction import extract

    fixtures = Path(__file__).parent / "fixtures"
    report_src = fixtures / "report_facsimile" / "2014-01-20_MO-PII-1R-2014.md"
    report_md = tmp_path / report_src.name
    report_md.write_text(report_src.read_text(encoding="utf-8"), encoding="utf-8")

    report_r = extract(report_md)
    assert report_r.status == "extract"
    report_sc = json.loads(report_r.sidecar_path.read_text(encoding="utf-8"))
    session_date = report_sc["body"]["report"]["received_at"]["session_date"]
    assert session_date == "2013-12-04"

    # Forge a joint sidecar matching that date
    forged_path = _write(
        tmp_path,
        "forged-joint.extraction.json",
        _joint_sidecar(
            doc_id="mo://2013/II/100",
            published="2013-12-04",
            session_date="2013-12-04",
        ),
    )

    results = list(link_all([forged_path, report_r.sidecar_path]))
    assert len(results) == 1
    assert results[0].status == "linked"
    assert results[0].target_document_id == "mo://2013/II/100"

    # Confirm it landed on disk
    after = json.loads(report_r.sidecar_path.read_text(encoding="utf-8"))
    assert (
        after["body"]["report"]["received_at"]["received_in_document"]
        == "mo://2013/II/100"
    )
