"""Tests for the cross-document linker (v0.2.0 — both passes).

Synthetic sidecars (built in-test, no fixture MDs needed) cover:
  - Index construction (joint vs single-chamber priority, date sources)
  - Single-sidecar linking happy path
  - Skip paths (already-linked, no session_date, no match, wrong type)
  - Force re-link
  - Schema validation guard
  - Vote-pair pass: pair detection, multi-deferral chain, no-resolver,
    force re-link, idempotent skip, window-out-of-range rejection,
    multi-resolver back-link merge
  - End-to-end on real fixtures (extract a report_facsimile + a joint
    plenary, then link)
"""

from __future__ import annotations

import json
from pathlib import Path

from monitorul_ii.extraction.linker import (
    DEFERRAL_WINDOW_DAYS,
    LINKER_VERSION,
    build_session_index,
    build_vote_index,
    link_all,
    link_all_votes,
    link_report,
    link_vote,
)
from monitorul_ii.extraction.linker import (
    _build_pairs as build_pairs,
)


# -- helpers --------------------------------------------------------------


def _minimal_envelope(
    *, doc_id: str, doc_type: str, published: str, session_date: str | None
) -> dict:
    """Build a minimal sidecar envelope shared across types."""
    return {
        "schema_version": "1.12.0",
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


# -- vote-pair pass (v0.2.0) ----------------------------------------------


def _vote_activity(
    *, outcome: str, motion_type: str = "final", chars: tuple[int, int] = (0, 100)
) -> dict:
    return {
        "type": "vote",
        "motion_text": "Supun votului final.",
        "motion_type": motion_type,
        "voting_method": "electronic",
        "timing": "deferred" if outcome == "deferred" else "live",
        "counts": {
            "for": None if outcome == "deferred" else 200,
            "against": None if outcome == "deferred" else 10,
            "abstain": None if outcome == "deferred" else 5,
            "not_voting": None,
            "total_voting": None if outcome == "deferred" else 215,
        },
        "outcome": outcome,
        "quorum_announced": None,
        "proposed_by": None,
        "nominal_breakdown": None,
        "defers_to": None,
        "resolves": [],
        "source_span": {
            "chars": list(chars),
            "lines": [1, 2],
            "content_sha": "0123456789ab",
        },
        "extraction": {
            "extractor": "regex@1",
            "confidence": 0.9,
            "source_span": {
                "chars": list(chars),
                "lines": [1, 2],
                "content_sha": "0123456789ab",
            },
        },
    }


def _agenda_with_vote(
    *,
    ordinal: int,
    title: str,
    bill_ref: dict | None = None,
    vote: dict,
) -> dict:
    refs = [bill_ref] if bill_ref else []
    return {
        "ordinal": ordinal,
        "title": title,
        "primary_references": refs,
        "category": "bill_debate",
        "confidence_type": None,
        "requested_by_group": None,
        "outcome": None,
        "reexamination_reason": None,
        "pages_in_pdf": [],
        "topics": {"primary": [], "secondary": []},
        "activities": [vote],
        "source_span": {
            "chars": [0, 200],
            "lines": [1, 5],
            "content_sha": "0123456789ab",
        },
        "extraction": {
            "extractor": "regex@1",
            "confidence": 0.9,
            "source_span": {
                "chars": [0, 200],
                "lines": [1, 5],
                "content_sha": "0123456789ab",
            },
        },
    }


def _bill_ref(prefix: str = "PL-x", number: str = "100", year: int = 2025) -> dict:
    return {
        "type": "bill",
        "raw": f"{prefix} {number}/{year}",
        "char_offsets": [0, len(f"{prefix} {number}/{year}")],
        "prefix": prefix,
        "number": number,
        "year": year,
        "secondary_year": None,
        "chamber_of_origin": "camera",
        "procedure": None,
        "subject": None,
    }


def _stenogram_with_votes(
    *,
    doc_id: str,
    session_date: str,
    agenda_items: list[dict],
) -> dict:
    sc = _stenogram_sidecar(
        doc_id=doc_id, published=session_date, session_date=session_date
    )
    sc["body"]["agenda_items"] = agenda_items
    return sc


# -- match key ---------------------------------------------------------


def test_build_vote_index_keys_by_bill_cite(tmp_path: Path):
    bill = _bill_ref(number="100", year=2025)
    vote = _vote_activity(outcome="deferred")
    sc = _stenogram_with_votes(
        doc_id="mo://2025/II/1",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1, title="Adoptare PL-x 100/2025", bill_ref=bill, vote=vote
            )
        ],
    )
    p = _write(tmp_path, "sten1.extraction.json", sc)
    idx = build_vote_index([p])
    # Key includes the bill prefix so PL-x and L can't cross-collide.
    assert "bill:PL-x:100/2025" in idx
    assert len(idx["bill:PL-x:100/2025"]) == 1
    assert idx["bill:PL-x:100/2025"][0].outcome == "deferred"


def test_build_vote_index_separates_camera_from_senate_bills(tmp_path: Path):
    """PL-x N/Y and L N/Y are different bills; their keys must differ."""
    pl = _bill_ref(prefix="PL-x", number="500", year=2025)
    l_ref = _bill_ref(prefix="L", number="500", year=2025)
    sc_pl = _stenogram_with_votes(
        doc_id="mo://2025/II/PL",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=pl,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    sc_l = _stenogram_with_votes(
        doc_id="mo://2025/II/L",
        session_date="2025-04-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="y",
                bill_ref=l_ref,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    p_pl = _write(tmp_path, "pl.extraction.json", sc_pl)
    p_l = _write(tmp_path, "l.extraction.json", sc_l)
    idx = build_vote_index([p_pl, p_l])
    assert set(idx.keys()) == {"bill:PL-x:500/2025", "bill:L:500/2025"}


def test_build_vote_index_skips_when_no_stable_cite(tmp_path: Path):
    """No primary_references → vote is unmatchable, dropped from index.

    Generic procedural agenda titles (`Diverse`, `Aprobarea ordinii de
    zi`, `Ședința`) repeat every session, so the title-hash fallback
    would produce thousands of false-positive cross-doc pairs. v0.2.0
    drops the fallback and accepts the coverage gap.
    """
    vote = _vote_activity(outcome="approved")
    sc = _stenogram_with_votes(
        doc_id="mo://2025/II/2",
        session_date="2025-04-02",
        agenda_items=[
            _agenda_with_vote(ordinal=1, title="Diverse", bill_ref=None, vote=vote)
        ],
    )
    p = _write(tmp_path, "sten2.extraction.json", sc)
    idx = build_vote_index([p])
    assert idx == {}


def test_build_vote_index_skips_non_plenary(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
        ),
    )
    idx = build_vote_index([rp])
    assert idx == {}


def test_build_vote_index_sorts_by_session_date(tmp_path: Path):
    bill = _bill_ref(number="200", year=2025)
    a = _stenogram_with_votes(
        doc_id="mo://2025/II/A",
        session_date="2025-05-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    b = _stenogram_with_votes(
        doc_id="mo://2025/II/B",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    pa = _write(tmp_path, "sten_a.extraction.json", a)
    pb = _write(tmp_path, "sten_b.extraction.json", b)
    idx = build_vote_index([pa, pb])
    entries = idx["bill:PL-x:200/2025"]
    assert [e.document_id for e in entries] == ["mo://2025/II/B", "mo://2025/II/A"]


# -- pair detection ---------------------------------------------------------


def test_pair_detection_simple_two_doc_pair(tmp_path: Path):
    """Doc N defers, doc M (within 60 days) resolves."""
    bill = _bill_ref(number="300", year=2025)
    deferring = _stenogram_with_votes(
        doc_id="mo://2025/II/N",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    resolving = _stenogram_with_votes(
        doc_id="mo://2025/II/M",
        session_date="2025-04-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pn = _write(tmp_path, "n.extraction.json", deferring)
    pm = _write(tmp_path, "m.extraction.json", resolving)
    idx = build_vote_index([pn, pm])
    forward, back = build_pairs(idx)
    assert forward[("mo://2025/II/N", 0, 0)] == "mo://2025/II/M"
    assert back[("mo://2025/II/M", 0, 0)] == ["mo://2025/II/N"]


def test_pair_detection_outside_window_rejects(tmp_path: Path):
    """Resolver is > 60 days out — no pair."""
    bill = _bill_ref(number="400", year=2025)
    deferring = _stenogram_with_votes(
        doc_id="mo://2025/II/N",
        session_date="2025-01-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    far = _stenogram_with_votes(
        doc_id="mo://2025/II/Z",
        session_date="2025-06-01",  # 151 days after — beyond 60-day window
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pn = _write(tmp_path, "n.extraction.json", deferring)
    pz = _write(tmp_path, "z.extraction.json", far)
    idx = build_vote_index([pn, pz])
    forward, back = build_pairs(idx)
    assert forward == {}
    assert back == {}


def test_pair_detection_no_resolver_at_all(tmp_path: Path):
    """Only deferred votes — no forward link possible."""
    bill = _bill_ref(number="500", year=2025)
    a = _stenogram_with_votes(
        doc_id="mo://2025/II/A",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    pa = _write(tmp_path, "a.extraction.json", a)
    idx = build_vote_index([pa])
    forward, back = build_pairs(idx)
    assert forward == {}
    assert back == {}


def test_multi_deferral_chain(tmp_path: Path):
    """A→B→C: A defers to B, B defers to C, C resolves. C.resolves = [A, B]."""
    bill = _bill_ref(number="600", year=2025)
    a = _stenogram_with_votes(
        doc_id="mo://2025/II/A",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    b = _stenogram_with_votes(
        doc_id="mo://2025/II/B",
        session_date="2025-04-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    c = _stenogram_with_votes(
        doc_id="mo://2025/II/C",
        session_date="2025-05-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pa = _write(tmp_path, "a.extraction.json", a)
    pb = _write(tmp_path, "b.extraction.json", b)
    pc = _write(tmp_path, "c.extraction.json", c)
    idx = build_vote_index([pa, pb, pc])
    forward, back = build_pairs(idx)
    assert forward[("mo://2025/II/A", 0, 0)] == "mo://2025/II/B"
    assert forward[("mo://2025/II/B", 0, 0)] == "mo://2025/II/C"
    # C resolves both A and B (chain reversal)
    assert back[("mo://2025/II/C", 0, 0)] == ["mo://2025/II/A", "mo://2025/II/B"]


def test_multi_resolvers_into_single_back_link(tmp_path: Path):
    """Two prior deferrals on the same key both resolve in the same later doc.

    Both sources back-link to the same resolver via the resolves[] list.
    """
    bill = _bill_ref(number="700", year=2025)
    a = _stenogram_with_votes(
        doc_id="mo://2025/II/A",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    b = _stenogram_with_votes(
        doc_id="mo://2025/II/B",
        session_date="2025-04-08",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    c = _stenogram_with_votes(
        doc_id="mo://2025/II/C",
        session_date="2025-04-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pa = _write(tmp_path, "a.extraction.json", a)
    pb = _write(tmp_path, "b.extraction.json", b)
    pc = _write(tmp_path, "c.extraction.json", c)
    idx = build_vote_index([pa, pb, pc])
    forward, back = build_pairs(idx)
    # A→B (immediate next), B→C (immediate next)
    assert forward[("mo://2025/II/A", 0, 0)] == "mo://2025/II/B"
    assert forward[("mo://2025/II/B", 0, 0)] == "mo://2025/II/C"
    # C resolves both A and B
    assert back[("mo://2025/II/C", 0, 0)] == ["mo://2025/II/A", "mo://2025/II/B"]


# -- link_vote write semantics ---------------------------------------------


def test_link_vote_writes_forward_link(tmp_path: Path):
    bill = _bill_ref(number="800", year=2025)
    a = _stenogram_with_votes(
        doc_id="mo://2025/II/A",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    pa = _write(tmp_path, "a.extraction.json", a)
    forward = {("mo://2025/II/A", 0, 0): "mo://2025/II/B"}
    back: dict = {}
    result = link_vote(pa, forward_links=forward, back_links=back)
    assert result.status == "linked"
    assert result.pairs_written == 1
    after = json.loads(pa.read_text(encoding="utf-8"))
    assert (
        after["body"]["agenda_items"][0]["activities"][0]["defers_to"]
        == "mo://2025/II/B"
    )


def test_link_vote_writes_back_link_on_resolver(tmp_path: Path):
    bill = _bill_ref(number="900", year=2025)
    c = _stenogram_with_votes(
        doc_id="mo://2025/II/C",
        session_date="2025-04-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pc = _write(tmp_path, "c.extraction.json", c)
    forward: dict = {}
    back = {("mo://2025/II/C", 0, 0): ["mo://2025/II/A", "mo://2025/II/B"]}
    result = link_vote(pc, forward_links=forward, back_links=back)
    assert result.status == "linked"
    assert result.backlinks_written == 1
    after = json.loads(pc.read_text(encoding="utf-8"))
    assert after["body"]["agenda_items"][0]["activities"][0]["resolves"] == [
        "mo://2025/II/A",
        "mo://2025/II/B",
    ]


def test_link_vote_idempotent_skips_when_already_linked(tmp_path: Path):
    """Run link twice — second pass should skip with no further updates."""
    bill = _bill_ref(number="1000", year=2025)
    deferring = _stenogram_with_votes(
        doc_id="mo://2025/II/N",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    resolving = _stenogram_with_votes(
        doc_id="mo://2025/II/M",
        session_date="2025-04-15",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pn = _write(tmp_path, "n.extraction.json", deferring)
    pm = _write(tmp_path, "m.extraction.json", resolving)
    # First pass: writes the pair
    results1 = list(link_all_votes([pn, pm]))
    assert all(r.status == "linked" for r in results1)
    # Second pass: every vote is already linked → all skip
    results2 = list(link_all_votes([pn, pm]))
    assert all(r.status == "skip" for r in results2)


def test_link_vote_force_relinks_populated_entries(tmp_path: Path):
    """force=True overwrites existing defers_to + resolves[]."""
    bill = _bill_ref(number="1100", year=2025)
    deferring = _stenogram_with_votes(
        doc_id="mo://2025/II/N",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    # Stale forward link
    deferring["body"]["agenda_items"][0]["activities"][0]["defers_to"] = (
        "mo://2025/II/STALE"
    )
    pn = _write(tmp_path, "n.extraction.json", deferring)
    forward = {("mo://2025/II/N", 0, 0): "mo://2025/II/M"}
    back: dict = {}
    # Without force: stale stays
    r1 = link_vote(pn, forward_links=forward, back_links=back, force=False)
    assert r1.status == "skip"
    after_no_force = json.loads(pn.read_text(encoding="utf-8"))
    assert (
        after_no_force["body"]["agenda_items"][0]["activities"][0]["defers_to"]
        == "mo://2025/II/STALE"
    )
    # With force: rewrites
    r2 = link_vote(pn, forward_links=forward, back_links=back, force=True)
    assert r2.status == "linked"
    after_force = json.loads(pn.read_text(encoding="utf-8"))
    assert (
        after_force["body"]["agenda_items"][0]["activities"][0]["defers_to"]
        == "mo://2025/II/M"
    )


def test_link_vote_dry_run_preserves_disk(tmp_path: Path):
    bill = _bill_ref(number="1200", year=2025)
    deferring = _stenogram_with_votes(
        doc_id="mo://2025/II/N",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    pn = _write(tmp_path, "n.extraction.json", deferring)
    before = pn.read_text(encoding="utf-8")
    forward = {("mo://2025/II/N", 0, 0): "mo://2025/II/M"}
    result = link_vote(pn, forward_links=forward, back_links={}, write=False)
    assert result.status == "linked"
    assert result.pairs_written == 1
    assert pn.read_text(encoding="utf-8") == before


def test_link_vote_skips_non_plenary(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            published="2014-01-20",
            session_date="2013-12-04",
        ),
    )
    result = link_vote(rp, forward_links={}, back_links={})
    assert result.status == "skip"
    assert "not a plenary sidecar" in (result.reason or "")


def test_window_constant_is_60_days():
    """Sanity: the window constant matches the documented 60-day rule."""
    assert DEFERRAL_WINDOW_DAYS == 60


def test_link_all_votes_end_to_end_writes_pairs(tmp_path: Path):
    """End-to-end across the iterator entry point."""
    bill = _bill_ref(number="1300", year=2025)
    deferring = _stenogram_with_votes(
        doc_id="mo://2025/II/D",
        session_date="2025-04-01",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="deferred"),
            )
        ],
    )
    resolving = _stenogram_with_votes(
        doc_id="mo://2025/II/R",
        session_date="2025-04-20",
        agenda_items=[
            _agenda_with_vote(
                ordinal=1,
                title="x",
                bill_ref=bill,
                vote=_vote_activity(outcome="approved"),
            )
        ],
    )
    pd = _write(tmp_path, "d.extraction.json", deferring)
    pr = _write(tmp_path, "r.extraction.json", resolving)
    results = list(link_all_votes([pd, pr]))
    assert len(results) == 2
    assert all(r.status == "linked" for r in results)
    after_d = json.loads(pd.read_text(encoding="utf-8"))
    after_r = json.loads(pr.read_text(encoding="utf-8"))
    assert (
        after_d["body"]["agenda_items"][0]["activities"][0]["defers_to"]
        == "mo://2025/II/R"
    )
    assert after_r["body"]["agenda_items"][0]["activities"][0]["resolves"] == [
        "mo://2025/II/D"
    ]


def test_linker_version_bumped_to_0_2_0():
    assert LINKER_VERSION == "0.2.0"
