"""Tests for the registry-driven backfill passes (Tier 4).

Synthetic sidecars built in-test, no fixture MDs needed. Mirrors the
linker's test pattern — happy path + skip paths + force + schema guard.
"""

from __future__ import annotations

import json
from pathlib import Path

from monitorul_ii.extraction.backfills import (
    ISSUING_BODY_BACKFILL_VERSION,
    MINISTRY_BACKFILL_VERSION,
    PERSONS_BACKFILL_VERSION,
    PROPOSED_BY_BACKFILL_VERSION,
    backfill_all_issuing_bodies,
    backfill_all_ministries,
    backfill_all_persons,
    backfill_all_proposed_by,
    backfill_issuing_body,
    backfill_ministries,
    backfill_persons,
    backfill_proposed_by,
)
from monitorul_ii.extraction.identity import assign_identity


# -- sidecar builder --------------------------------------------------------


def _stamp_identity(sc: dict) -> dict:
    """Backfill the schema 1.13.0 identity layer onto a freshly-built test
    sidecar. Mutates `sc` in place AND attaches the envelope identity
    block to `extraction.identity`."""
    body = sc.get("body") or {}
    meta = sc.get("metadata") or {}
    identity = assign_identity(
        body,
        doc_type=sc["document_type"],
        doc_id=sc["document_id"],
        year=int(meta.get("year", 2024)),
        issue=str(meta.get("issue", "1")),
    )
    sc.setdefault("extraction", {})["identity"] = identity
    return sc


def _report_sidecar(
    *,
    doc_id: str,
    issuing_body: str | None,
    issuing_body_normalized: str | None = None,
) -> dict:
    sc = {
        "schema_version": "1.13.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": "report_facsimile",
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": 2014,
            "part": "II",
            "published": "2014-01-20",
            "chamber": "joint",
            "session": None,
            "session_type": None,
            "session_date": "2013-12-04",
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
        "body": {
            "report": {
                "title": "Raportul X",
                "issuing_body": issuing_body,
                "issuing_body_normalized": issuing_body_normalized,
                "reporting_period": {"start": "2010-01-01", "end": "2010-12-31"},
                "received_at": {
                    "session_kind": "joint",
                    "session_date": "2013-12-04",
                    "received_in_document": None,
                },
            },
            "headings": [],
            "raw_markdown_excerpt": "",
        },
    }
    return _stamp_identity(sc)


def _qr_sidecar(
    *, doc_id: str, ministries: list[tuple[str | None, str | None]]
) -> dict:
    """A question_register sidecar with one Question per (ministry, normalized).

    `ministries` is a list of `(raw, normalized)` pairs — `normalized=None`
    means "leave the slot null on the input sidecar", so the backfill
    needs to fill it in.
    """
    questions = []
    for i, (raw, norm) in enumerate(ministries, start=1):
        questions.append(
            {
                "ordinal": i,
                "addressee": {
                    "ministry": raw,
                    "ministry_normalized": norm,
                    "name": None,
                    "role": None,
                },
                "questioner": {
                    "raw": "Domnul X",
                    "name": None,
                    "title": None,
                    "role": None,
                    "party_group": None,
                    "person_id": None,
                },
                "registration_number": None,
                "registration_date": None,
                "topic": None,
                "question_text": None,
                "source_span": {
                    "chars": [0, 1],
                    "lines": [1, 1],
                    "content_sha": "0123456789ab",
                },
                "extraction": {
                    "extractor": "qr@1",
                    "confidence": 0.8,
                    "source_span": {
                        "chars": [0, 1],
                        "lines": [1, 1],
                        "content_sha": "0123456789ab",
                    },
                },
            }
        )
    sc = {
        "schema_version": "1.13.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": "question_register",
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": 2024,
            "part": "II",
            "published": "2024-04-09",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": "2024-04-09",
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
        "body": {
            "session_label": None,
            "chamber": "Camera Deputaților",
            "questions": questions,
        },
    }
    return _stamp_identity(sc)


def _plenary_with_interpellations_sidecar(
    *, doc_id: str, addressed_to: list[tuple[str | None, str | None]]
) -> dict:
    interps = []
    for i, (raw, norm) in enumerate(addressed_to, start=1):
        interps.append(
            {
                "genre": "interpelare",
                "questioner": {
                    "raw": "Domnul X",
                    "name": None,
                    "title": None,
                    "role": None,
                    "party_group": None,
                    "person_id": None,
                },
                "addressed_to": raw,
                "addressed_to_normalized": norm,
                "interpellation_number": None,
                "topic": None,
                "question_text": None,
                "response": None,
                "response_deferred": False,
                "source_span": {
                    "chars": [0, 1],
                    "lines": [1, 1],
                    "content_sha": "0123456789ab",
                },
                "extraction": {
                    "extractor": "interp@1",
                    "confidence": 0.8,
                    "source_span": {
                        "chars": [0, 1],
                        "lines": [1, 1],
                        "content_sha": "0123456789ab",
                    },
                },
            }
        )
    sc = {
        "schema_version": "1.13.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": 2024,
            "part": "II",
            "published": "2024-04-09",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": "2024-04-09",
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
        "body": {
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
            "interpellations": interps,
        },
    }
    return _stamp_identity(sc)


def _vote_activity(
    *,
    span_start: int = 0,
    span_end: int = 1,
    proposed_by: dict | None = None,
) -> dict:
    return {
        "type": "vote",
        "motion_text": "Supun votului…",
        "motion_type": "final",
        "voting_method": "electronic",
        "timing": "live",
        "counts": {
            "for": 100,
            "against": 0,
            "abstain": 0,
            "not_voting": 0,
            "total_voting": 100,
        },
        "outcome": "approved",
        "quorum_announced": None,
        "proposed_by": proposed_by,
        "nominal_breakdown": None,
        "defers_to": None,
        "resolves": [],
        "source_span": {
            "chars": [span_start, span_end],
            "lines": [1, 1],
            "content_sha": "0123456789ab",
        },
        "extraction": {
            "extractor": "votes@1",
            "confidence": 0.9,
            "source_span": {
                "chars": [span_start, span_end],
                "lines": [1, 1],
                "content_sha": "0123456789ab",
            },
        },
    }


def _agenda_with_vote(
    *,
    title: str,
    primary_references: list[dict] | None = None,
    proposed_by: dict | None = None,
    span_start: int = 0,
    span_end: int = 100,
) -> dict:
    return {
        "ordinal": 1,
        "title": title,
        "category": "bill_debate",
        "primary_references": primary_references or [],
        "outcome": None,
        "confidence_type": None,
        "requested_by_group": None,
        "reexamination_reason": None,
        "pages_in_pdf": [],
        "topics": {"primary": [], "secondary": []},
        "activities": [_vote_activity(proposed_by=proposed_by)],
        "source_span": {
            "chars": [span_start, span_end],
            "lines": [1, 5],
            "content_sha": "0123456789ab",
        },
        "extraction": {
            "extractor": "agenda@1",
            "confidence": 0.9,
            "source_span": {
                "chars": [span_start, span_end],
                "lines": [1, 5],
                "content_sha": "0123456789ab",
            },
        },
    }


def _plenary_with_agenda_sidecar(
    *,
    doc_id: str,
    agenda_items: list[dict],
) -> dict:
    sc = {
        "schema_version": "1.13.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": 2024,
            "part": "II",
            "published": "2024-04-09",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": "2024-04-09",
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
            "body_chars": 1000,
            "claimed_chars": 1000,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
        "body": {
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
            "agenda_items": agenda_items,
            "interpellations": [],
        },
    }
    return _stamp_identity(sc)


def _stenogram_sidecar(*, doc_id: str) -> dict:
    """A non-report sidecar — backfill should ignore these."""
    sc = {
        "schema_version": "1.13.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": 2024,
            "part": "II",
            "published": "2024-04-09",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": "2024-04-09",
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
        "body": {
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
        },
    }
    return _stamp_identity(sc)


def _write(tmp_path: Path, name: str, sidecar: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# -- module surface ---------------------------------------------------------


def test_backfill_version_constant_present():
    assert isinstance(ISSUING_BODY_BACKFILL_VERSION, str)
    assert ISSUING_BODY_BACKFILL_VERSION.count(".") == 2


# -- happy path -------------------------------------------------------------


def test_backfill_issuing_body_happy_path(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/1R",
            issuing_body="Consiliul Legislativ",
        ),
    )
    result = backfill_issuing_body(rp)
    assert result.status == "filled"
    assert result.canonical_id == "consiliul_legislativ"
    assert result.matched_via == "exact"
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert sc["body"]["report"]["issuing_body_normalized"] == "consiliul_legislativ"


def test_backfill_resolves_genitive_form(tmp_path: Path):
    """Common in extracted titles — genitive declensions resolve."""
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/2R",
            issuing_body="Consiliului Suprem de Apărare a Țării",
        ),
    )
    result = backfill_issuing_body(rp)
    assert result.status == "filled"
    assert result.canonical_id == "csat"


# -- skip paths -------------------------------------------------------------


def test_backfill_skip_when_no_raw_value(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(doc_id="mo://2014/II/3R", issuing_body=None),
    )
    result = backfill_issuing_body(rp)
    assert result.status == "skip"
    assert "no issuing_body raw value" in (result.reason or "")
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert sc["body"]["report"]["issuing_body_normalized"] is None


def test_backfill_skip_when_unknown_body(tmp_path: Path):
    """Unknown raw → skip with explicit `no registry match` reason; the
    normalized slot must remain null so the registry gap stays visible
    in the corpus smoke."""
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/4R",
            issuing_body="Asociaţiei Pro Democraţia",
        ),
    )
    result = backfill_issuing_body(rp)
    assert result.status == "skip"
    assert "no registry match" in (result.reason or "")
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert sc["body"]["report"]["issuing_body_normalized"] is None


def test_backfill_skip_when_already_filled_with_same_id(tmp_path: Path):
    """Idempotent re-runs: same canonical id → skip."""
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/5R",
            issuing_body="Consiliul Legislativ",
            issuing_body_normalized="consiliul_legislativ",
        ),
    )
    result = backfill_issuing_body(rp)
    assert result.status == "skip"
    assert "already filled" in (result.reason or "")


def test_backfill_skip_when_already_filled_different_id_without_force(
    tmp_path: Path,
):
    """Pre-existing mismatched value is left intact unless force=True."""
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/6R",
            issuing_body="Consiliul Legislativ",
            issuing_body_normalized="csat",
        ),
    )
    result = backfill_issuing_body(rp)
    assert result.status == "skip"
    assert "differs from registry id" in (result.reason or "")
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert sc["body"]["report"]["issuing_body_normalized"] == "csat"


def test_backfill_force_overwrites_mismatch(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/7R",
            issuing_body="Consiliul Legislativ",
            issuing_body_normalized="csat",
        ),
    )
    result = backfill_issuing_body(rp, force=True)
    assert result.status == "filled"
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert sc["body"]["report"]["issuing_body_normalized"] == "consiliul_legislativ"


def test_backfill_skip_for_non_report_sidecars(tmp_path: Path):
    sp = _write(
        tmp_path,
        "stenogram.extraction.json",
        _stenogram_sidecar(doc_id="mo://2024/II/100"),
    )
    result = backfill_issuing_body(sp)
    assert result.status == "skip"
    assert "not a report_facsimile" in (result.reason or "")


def test_backfill_dry_run_does_not_write_to_disk(tmp_path: Path):
    rp = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(
            doc_id="mo://2014/II/8R",
            issuing_body="SRI",
        ),
    )
    result = backfill_issuing_body(rp, write=False)
    assert result.status == "filled"
    sc = json.loads(rp.read_text(encoding="utf-8"))
    assert sc["body"]["report"]["issuing_body_normalized"] is None


# -- batch iterator ---------------------------------------------------------


def test_backfill_all_filters_to_reports(tmp_path: Path):
    paths = [
        _write(
            tmp_path,
            "report1.extraction.json",
            _report_sidecar(
                doc_id="mo://2014/II/9R",
                issuing_body="Consiliul Legislativ",
            ),
        ),
        _write(
            tmp_path,
            "report2.extraction.json",
            _report_sidecar(
                doc_id="mo://2014/II/10R",
                issuing_body="Banca Națională a României",
            ),
        ),
        _write(
            tmp_path,
            "stenogram.extraction.json",
            _stenogram_sidecar(doc_id="mo://2024/II/200"),
        ),
    ]
    results = list(backfill_all_issuing_bodies(paths))
    # The stenogram is filtered out before yielding — only 2 results.
    assert len(results) == 2
    assert all(r.status == "filled" for r in results)
    ids = {r.canonical_id for r in results}
    assert ids == {"consiliul_legislativ", "bnr"}


# -- ministry pass (4.2) ----------------------------------------------------


def test_ministry_backfill_version_constant_present():
    assert isinstance(MINISTRY_BACKFILL_VERSION, str)
    assert MINISTRY_BACKFILL_VERSION.count(".") == 2


def test_ministry_backfill_qr_happy_path(tmp_path: Path):
    sc = _qr_sidecar(
        doc_id="mo://2024/II/100",
        ministries=[
            ("Ministerul Sănătății", None),
            ("Ministerul Educației Naționale", None),
        ],
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    result = backfill_ministries(p)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    qs = on_disk["body"]["questions"]
    assert qs[0]["addressee"]["ministry_normalized"] == "health"
    assert qs[1]["addressee"]["ministry_normalized"] == "education"


def test_ministry_backfill_plenary_happy_path(tmp_path: Path):
    sc = _plenary_with_interpellations_sidecar(
        doc_id="mo://2024/II/200",
        addressed_to=[
            ("Ministerul Mediului", None),
            ("Ministerul Apărării", None),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_ministries(p)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    interps = on_disk["body"]["interpellations"]
    assert interps[0]["addressed_to_normalized"] == "environment"
    assert interps[1]["addressed_to_normalized"] == "defense"


def test_ministry_backfill_skips_already_filled_with_same_id(tmp_path: Path):
    sc = _qr_sidecar(
        doc_id="mo://2024/II/300",
        ministries=[
            ("Ministerul Sănătății", "health"),
            ("Ministerul Educației", "education"),
        ],
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    result = backfill_ministries(p)
    assert result.status == "skip"
    assert "already filled" in (result.reason or "")
    # File should NOT be re-written; idempotency guarantees no fills.
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    qs = on_disk["body"]["questions"]
    assert qs[0]["addressee"]["ministry_normalized"] == "health"


def test_ministry_backfill_handles_mixed_records(tmp_path: Path):
    """Some records are pre-filled, some are null, some have null raws,
    some are unknown ministries — all four cases coexist in one
    sidecar; the writer fills only the records where it adds value."""
    sc = _qr_sidecar(
        doc_id="mo://2024/II/400",
        ministries=[
            ("Ministerul Sănătății", None),  # fills
            ("Ministerul Educației", "education"),  # already-correct → skip
            (None, None),  # no raw → skip
            ("Ministerul Inventat", None),  # registry miss → skip
        ],
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    result = backfill_ministries(p)
    assert result.status == "filled"
    assert "1 records filled" in (result.reason or "")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    qs = on_disk["body"]["questions"]
    assert qs[0]["addressee"]["ministry_normalized"] == "health"
    assert qs[1]["addressee"]["ministry_normalized"] == "education"
    assert qs[2]["addressee"]["ministry_normalized"] is None
    assert qs[3]["addressee"]["ministry_normalized"] is None


def test_ministry_backfill_force_overwrites_mismatch(tmp_path: Path):
    sc = _qr_sidecar(
        doc_id="mo://2024/II/500",
        ministries=[("Ministerul Sănătății", "education")],  # bogus pre-fill
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    # Without force: skip.
    r1 = backfill_ministries(p)
    assert r1.status == "skip"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["questions"][0]["addressee"]["ministry_normalized"] == (
        "education"
    )
    # With force: overwrite.
    r2 = backfill_ministries(p, force=True)
    assert r2.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["questions"][0]["addressee"]["ministry_normalized"] == (
        "health"
    )


def test_ministry_backfill_skip_for_unrelated_doctype(tmp_path: Path):
    sp = _write(
        tmp_path,
        "stenogram.extraction.json",
        _stenogram_sidecar(doc_id="mo://2024/II/600"),
    )
    # The minimal stenogram has no interpellations — backfill_ministries
    # finds zero records and skips.
    result = backfill_ministries(sp)
    assert result.status == "skip"


def test_ministry_backfill_fallback_to_institutional(tmp_path: Path):
    """Curtea de Conturi isn't a ministry — the institutional fallback
    must catch it."""
    sc = _qr_sidecar(
        doc_id="mo://2024/II/700",
        ministries=[("Curtea de Conturi a României", None)],
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    result = backfill_ministries(p)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["questions"][0]["addressee"]["ministry_normalized"] == (
        "curtea_de_conturi"
    )


def test_ministry_backfill_dry_run_does_not_write(tmp_path: Path):
    sc = _qr_sidecar(
        doc_id="mo://2024/II/800",
        ministries=[("Ministerul Sănătății", None)],
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    result = backfill_ministries(p, write=False)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["questions"][0]["addressee"]["ministry_normalized"] is None


def test_backfill_all_ministries_filters_to_relevant_types(tmp_path: Path):
    paths = [
        _write(
            tmp_path,
            "qr.extraction.json",
            _qr_sidecar(
                doc_id="mo://2024/II/900",
                ministries=[("Ministerul Sănătății", None)],
            ),
        ),
        _write(
            tmp_path,
            "report.extraction.json",
            _report_sidecar(
                doc_id="mo://2014/II/12R",
                issuing_body="SRI",
            ),
        ),
        _write(
            tmp_path,
            "plen.extraction.json",
            _plenary_with_interpellations_sidecar(
                doc_id="mo://2024/II/901",
                addressed_to=[("Ministerul Mediului", None)],
            ),
        ),
    ]
    results = list(backfill_all_ministries(paths))
    # Report sidecar is filtered out.
    assert len(results) == 2
    assert all(r.status == "filled" for r in results)


# -- proposed_by pass (4.4) -------------------------------------------------


def test_proposed_by_backfill_version_constant_present():
    assert isinstance(PROPOSED_BY_BACKFILL_VERSION, str)
    assert PROPOSED_BY_BACKFILL_VERSION.count(".") == 2


def _oug_ref(number: str = "50", year: int = 2024) -> dict:
    return {
        "type": "oug",
        "raw": f"OUG nr. {number}/{year}",
        "char_offsets": [0, 18],
        "number": number,
        "year": year,
    }


def test_proposed_by_fills_oug_ref(tmp_path: Path):
    """Agenda with `oug` ref → vote gets Guvern proposer."""
    sc = _plenary_with_agenda_sidecar(
        doc_id="mo://2024/II/1000",
        agenda_items=[
            _agenda_with_vote(
                title="Aprobarea OUG nr. 50/2024 privind X",
                primary_references=[_oug_ref()],
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_proposed_by(p)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    vote = on_disk["body"]["agenda_items"][0]["activities"][0]
    assert vote["proposed_by"] is not None
    assert vote["proposed_by"]["role"] == "Guvern"
    assert vote["proposed_by"]["name"] == "Guvernul"


def test_proposed_by_fills_from_title_pattern(tmp_path: Path):
    """Title contains `Ordonanței Guvernului` → Government-proposed."""
    sc = _plenary_with_agenda_sidecar(
        doc_id="mo://2024/II/1100",
        agenda_items=[
            _agenda_with_vote(
                title="Adoptarea proiectului de Lege pentru aprobarea Ordonanței Guvernului nr. 12/2023 privind Y",
                primary_references=[],
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_proposed_by(p)
    assert result.status == "filled"


def test_proposed_by_skips_when_no_government_signal(tmp_path: Path):
    """Agenda with bill ref but no OUG/OG cite or title pattern → null."""
    sc = _plenary_with_agenda_sidecar(
        doc_id="mo://2024/II/1200",
        agenda_items=[
            _agenda_with_vote(
                title="Adoptarea proiectului de Lege privind X",
                primary_references=[
                    {
                        "type": "bill",
                        "raw": "PL-x nr. 100/2024",
                        "char_offsets": [0, 18],
                        "prefix": "PL-x",
                        "number": "100",
                        "year": 2024,
                        "subject": None,
                    }
                ],
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_proposed_by(p)
    assert result.status == "skip"
    assert "no government-proposed agendas" in (result.reason or "")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["agenda_items"][0]["activities"][0]["proposed_by"] is None


def test_proposed_by_idempotent_skip_when_already_filled(tmp_path: Path):
    """Pre-filled with the same Guvern speaker → no rewrite."""
    gov = {
        "raw": "Guvernul României",
        "name": "Guvernul",
        "title": None,
        "role": "Guvern",
        "party_group": None,
        "person_id": None,
    }
    sc = _plenary_with_agenda_sidecar(
        doc_id="mo://2024/II/1300",
        agenda_items=[
            _agenda_with_vote(
                title="Aprobarea OUG nr. 1/2024",
                primary_references=[_oug_ref(number="1")],
                proposed_by=gov,
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_proposed_by(p)
    assert result.status == "skip"
    assert "already filled" in (result.reason or "")


def test_proposed_by_force_overwrites_mismatch(tmp_path: Path):
    """Pre-filled with a non-Guvern speaker; force replaces."""
    other = {
        "raw": "Domnul X",
        "name": "X",
        "title": None,
        "role": None,
        "party_group": None,
        "person_id": None,
    }
    sc = _plenary_with_agenda_sidecar(
        doc_id="mo://2024/II/1400",
        agenda_items=[
            _agenda_with_vote(
                title="Aprobarea OUG nr. 2/2024",
                primary_references=[_oug_ref(number="2")],
                proposed_by=other,
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    # Without force: skip.
    r1 = backfill_proposed_by(p)
    assert r1.status == "skip"
    assert "mismatch" in (r1.reason or "")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert (
        on_disk["body"]["agenda_items"][0]["activities"][0]["proposed_by"]["name"]
        == "X"
    )
    # With force: overwrite.
    r2 = backfill_proposed_by(p, force=True)
    assert r2.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert (
        on_disk["body"]["agenda_items"][0]["activities"][0]["proposed_by"]["role"]
        == "Guvern"
    )


def test_proposed_by_skip_for_unrelated_doctype(tmp_path: Path):
    p = _write(
        tmp_path,
        "report.extraction.json",
        _report_sidecar(doc_id="mo://2014/II/15R", issuing_body="SRI"),
    )
    result = backfill_proposed_by(p)
    assert result.status == "skip"
    assert "not a plenary sidecar" in (result.reason or "")


def test_proposed_by_dry_run_does_not_write(tmp_path: Path):
    sc = _plenary_with_agenda_sidecar(
        doc_id="mo://2024/II/1500",
        agenda_items=[
            _agenda_with_vote(
                title="Aprobarea OUG nr. 3/2024",
                primary_references=[_oug_ref(number="3")],
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_proposed_by(p, write=False)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["agenda_items"][0]["activities"][0]["proposed_by"] is None


def test_backfill_all_proposed_by_filters_to_plenary(tmp_path: Path):
    paths = [
        _write(
            tmp_path,
            "plen.extraction.json",
            _plenary_with_agenda_sidecar(
                doc_id="mo://2024/II/1600",
                agenda_items=[
                    _agenda_with_vote(
                        title="Aprobarea OUG nr. 4/2024",
                        primary_references=[_oug_ref(number="4")],
                    )
                ],
            ),
        ),
        _write(
            tmp_path,
            "report.extraction.json",
            _report_sidecar(doc_id="mo://2014/II/16R", issuing_body="SRI"),
        ),
    ]
    results = list(backfill_all_proposed_by(paths))
    # Report filtered out.
    assert len(results) == 1
    assert results[0].status == "filled"


def test_backfill_all_skips_corrupt_sidecar(tmp_path: Path):
    bad = tmp_path / "broken.extraction.json"
    bad.write_text("{ not valid }", encoding="utf-8")
    good = _write(
        tmp_path,
        "good.extraction.json",
        _report_sidecar(doc_id="mo://2014/II/11R", issuing_body="SRI"),
    )
    results = list(backfill_all_issuing_bodies([bad, good]))
    assert len(results) == 1
    assert results[0].status == "filled"
    assert results[0].sidecar_path == good


# -- persons pass (4.3) ----------------------------------------------------


def _make_speaker(
    *, raw: str, name: str | None = None, person_id: str | None = None
) -> dict:
    """Build a Speaker dict — six keys, all required, all non-failing."""
    return {
        "raw": raw,
        "name": name,
        "title": None,
        "role": None,
        "party_group": None,
        "person_id": person_id,
    }


def _plenary_with_speakers_sidecar(
    *,
    doc_id: str,
    year: int,
    speakers_in_chair: list[dict],
    activity_speakers: list[dict] | None = None,
) -> dict:
    """Build a plenary sidecar carrying real Speaker dicts in chair +
    optionally one agenda activity. Used by the persons backfill tests
    to exercise the recursive Speaker walk.
    """
    activity_speakers = activity_speakers or []
    activities = []
    for i, sp in enumerate(activity_speakers, start=1):
        activities.append(
            {
                "type": "speech",
                "speaker": sp,
                "text": "Mulțumesc, domnule președinte.",
                "delivery_mode": None,
                "references_mentioned": [],
                "source_span": {
                    "chars": [i * 100, i * 100 + 50],
                    "lines": [i, i],
                    "content_sha": "0123456789ab",
                },
                "extraction": {
                    "extractor": "act@1",
                    "confidence": 0.9,
                    "source_span": {
                        "chars": [i * 100, i * 100 + 50],
                        "lines": [i, i],
                        "content_sha": "0123456789ab",
                    },
                },
            }
        )
    sc = {
        "schema_version": "1.13.0",
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": year,
            "part": "II",
            "published": f"{year}-04-09",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": f"{year}-04-09",
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
            "body_chars": 1000,
            "claimed_chars": 1000,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
        "body": {
            "session": {
                "chair": speakers_in_chair,
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
            "agenda_items": [
                {
                    "ordinal": 1,
                    "title": "Test agenda",
                    "category": "bill_debate",
                    "primary_references": [],
                    "outcome": None,
                    "confidence_type": None,
                    "requested_by_group": None,
                    "reexamination_reason": None,
                    "pages_in_pdf": [],
                    "topics": {"primary": [], "secondary": []},
                    "activities": activities,
                    "source_span": {
                        "chars": [0, 1000],
                        "lines": [1, 50],
                        "content_sha": "0123456789ab",
                    },
                    "extraction": {
                        "extractor": "agenda@1",
                        "confidence": 0.9,
                        "source_span": {
                            "chars": [0, 1000],
                            "lines": [1, 50],
                            "content_sha": "0123456789ab",
                        },
                    },
                }
            ]
            if activities
            else [],
            "interpellations": [],
        },
    }
    return _stamp_identity(sc)


def test_persons_backfill_version_constant_present():
    assert isinstance(PERSONS_BACKFILL_VERSION, str)
    assert PERSONS_BACKFILL_VERSION.count(".") == 2


def test_persons_backfill_happy_path(tmp_path: Path):
    """Three speakers: two resolvable (one common, one with mojibake),
    one unknown. Backfill fills two, leaves one null, records matched_via."""
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/100",
        year=2018,
        speakers_in_chair=[
            _make_speaker(raw="Domnul Florin Iordache", name="Florin Iordache"),
        ],
        activity_speakers=[
            _make_speaker(
                raw="Domnul Nicolae V„c„roiu", name="Nicolae V„c„roiu"
            ),  # mojibake → diacritic-tier hit (already an alias → exact)
            _make_speaker(
                raw="Doamna Inexistentă Persoană",
                name="Inexistentă Persoană",
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_persons(p)
    assert result.status == "filled"
    assert "2 speakers filled" in (result.reason or "")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    chair = on_disk["body"]["session"]["chair"][0]
    assert chair["person_id"] == "iordache-florin"
    a0 = on_disk["body"]["agenda_items"][0]["activities"][0]
    a1 = on_disk["body"]["agenda_items"][0]["activities"][1]
    assert a0["speaker"]["person_id"] == "vacaroiu-nicolae"
    assert a1["speaker"]["person_id"] is None  # unresolved stays null


def test_persons_backfill_idempotent_skip(tmp_path: Path):
    """Second run on a fully-filled sidecar yields zero updates."""
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/101",
        year=2018,
        speakers_in_chair=[
            _make_speaker(
                raw="Domnul Florin Iordache",
                name="Florin Iordache",
                person_id="iordache-florin",
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_persons(p)
    assert result.status == "skip"
    assert "already filled" in (result.reason or "")


def test_persons_backfill_force_overwrites_mismatch(tmp_path: Path):
    """Without force the bogus pre-fill stays; with force it's replaced."""
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/102",
        year=2018,
        speakers_in_chair=[
            _make_speaker(
                raw="Domnul Florin Iordache",
                name="Florin Iordache",
                person_id="not-the-real-id",
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    r1 = backfill_persons(p)
    assert r1.status == "skip"
    assert "mismatch" in (r1.reason or "")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["session"]["chair"][0]["person_id"] == "not-the-real-id"

    r2 = backfill_persons(p, force=True)
    assert r2.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["session"]["chair"][0]["person_id"] == "iordache-florin"


def test_persons_backfill_dry_run_does_not_write(tmp_path: Path):
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/103",
        year=2018,
        speakers_in_chair=[
            _make_speaker(raw="Domnul Florin Iordache", name="Florin Iordache"),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_persons(p, write=False)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["session"]["chair"][0]["person_id"] is None


def test_persons_backfill_force_clears_stale_when_matcher_returns_none(
    tmp_path: Path,
):
    """Recovery path for matcher precision improvements: a sidecar carries
    a person_id that an earlier matcher version wrote (e.g. via the buggy
    joined-Lev≤2 fuzzy tier), but the current matcher returns None for
    the same raw. --force clears the stale fill so the data reflects the
    current matcher's truth. Without --force the stale value is preserved.

    Concrete case: `Domnul Gheorghe Vela` was incorrectly resolved to
    `gheorghe-vlad` by the prior fuzzy tier; the per-token fuzzy tier
    rejects the match (vela vs vlad is distance 2 over a 4-char token,
    above the per-token cap of 1), so the matcher now returns None.
    """
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2025/II/200",
        year=2025,
        speakers_in_chair=[
            _make_speaker(
                raw="Domnul Gheorghe Vela",
                name="Gheorghe Vela",
                person_id="gheorghe-vlad",  # stale fill from buggy matcher
            ),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)

    # Without force: the matcher returns None and we leave the stale fill
    # in place (defensive — operators may have set ids manually).
    r1 = backfill_persons(p)
    assert r1.status == "skip"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["session"]["chair"][0]["person_id"] == "gheorghe-vlad"

    # With force: the stale fill is cleared to None so ES no longer
    # attributes Vela's speeches to the wrong person.
    r2 = backfill_persons(p, force=True)
    assert r2.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["session"]["chair"][0]["person_id"] is None


def test_persons_backfill_qr_questioners(tmp_path: Path):
    """The Speaker walker must reach into qr.questions[*].questioner."""
    sc = _qr_sidecar(
        doc_id="mo://2024/II/901", ministries=[("Ministerul Sănătății", None)]
    )
    # Mutate the questioner Speaker to point at a known person.
    sc["body"]["questions"][0]["questioner"] = _make_speaker(
        raw="Domnul Florin Iordache", name="Florin Iordache"
    )
    p = _write(tmp_path, "qr.extraction.json", sc)
    result = backfill_persons(p)
    assert result.status == "filled"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["questions"][0]["questioner"]["person_id"] == (
        "iordache-florin"
    )


def test_persons_backfill_handles_empty_body(tmp_path: Path):
    """A plenary with no chair / activities / interpellations: skip."""
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/104",
        year=2018,
        speakers_in_chair=[],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_persons(p)
    assert result.status == "skip"


def test_persons_backfill_non_canonical_labels_stay_null(tmp_path: Path):
    """`Din sală` / `Guvernul` are not people — backfill leaves person_id=null."""
    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/105",
        year=2018,
        speakers_in_chair=[
            _make_speaker(raw="Din sală", name="Din sală"),
            _make_speaker(raw="Guvernul", name="Guvern"),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    result = backfill_persons(p)
    assert result.status == "skip"
    assert "no registry match" in (result.reason or "")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    for sp in on_disk["body"]["session"]["chair"]:
        assert sp["person_id"] is None


def test_backfill_all_persons_walks_every_doc_type(tmp_path: Path):
    """Iterator hits plenary, qr — both contain Speakers."""
    plenary = _write(
        tmp_path,
        "plen.extraction.json",
        _plenary_with_speakers_sidecar(
            doc_id="mo://2018/II/200",
            year=2018,
            speakers_in_chair=[
                _make_speaker(raw="Domnul Florin Iordache", name="Florin Iordache"),
            ],
        ),
    )
    qr = _qr_sidecar(
        doc_id="mo://2024/II/201", ministries=[("Ministerul Sănătății", None)]
    )
    qr["body"]["questions"][0]["questioner"] = _make_speaker(
        raw="Domnul Klaus Iohannis", name="Klaus Iohannis"
    )
    qr_path = _write(tmp_path, "qr.extraction.json", qr)
    results = list(backfill_all_persons([plenary, qr_path]))
    assert len(results) == 2
    assert all(r.status == "filled" for r in results)
    canonical_ids = {r.canonical_id for r in results}
    assert "iordache-florin" in canonical_ids
    assert "iohannis-klaus" in canonical_ids


# -- parallel iterators ----------------------------------------------------


def test_persons_backfill_parallel_workers_one_short_circuits_to_serial(
    tmp_path: Path,
):
    """`workers=1` (or 0) routes through the serial generator without
    spawning a process pool — important so debugging / determinism cases
    don't pay the spawn-pool overhead."""
    from monitorul_ii.extraction.backfills import backfill_all_persons_parallel

    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/300",
        year=2018,
        speakers_in_chair=[
            _make_speaker(raw="Domnul Florin Iordache", name="Florin Iordache"),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)
    results = list(backfill_all_persons_parallel([p], workers=1))
    assert len(results) == 1
    assert results[0].status == "filled"
    assert results[0].canonical_id == "iordache-florin"


def test_persons_backfill_parallel_yields_same_set_as_serial(tmp_path: Path):
    """The parallel iterator yields the same set of BackfillResults as
    the serial iterator — same canonical_ids, same statuses — just
    potentially in a different order (`as_completed` returns in
    completion order, not input order)."""
    from monitorul_ii.extraction.backfills import backfill_all_persons_parallel

    paths = []
    for i, (raw, name, expected_id) in enumerate(
        [
            ("Domnul Florin Iordache", "Florin Iordache", "iordache-florin"),
            ("Domnul Klaus Iohannis", "Klaus Iohannis", "iohannis-klaus"),
            ("Doamna Raluca Turcan", "Raluca Turcan", "turcan-raluca"),
        ]
    ):
        sc = _plenary_with_speakers_sidecar(
            doc_id=f"mo://2018/II/{400 + i}",
            year=2018,
            speakers_in_chair=[_make_speaker(raw=raw, name=name)],
        )
        paths.append(_write(tmp_path, f"plen{i}.extraction.json", sc))

    serial = sorted(
        (r.sidecar_path.name, r.canonical_id, r.status)
        for r in backfill_all_persons(paths)
    )
    # Reset person_id so the parallel pass actually has work to do.
    for p in paths:
        sc = json.loads(p.read_text(encoding="utf-8"))
        sc["body"]["session"]["chair"][0]["person_id"] = None
        p.write_text(json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")

    parallel = sorted(
        (r.sidecar_path.name, r.canonical_id, r.status)
        for r in backfill_all_persons_parallel(paths, workers=2)
    )
    assert serial == parallel


def test_issuing_body_backfill_parallel_routes_to_serial_with_one_path(
    tmp_path: Path,
):
    """A single-path workload short-circuits to serial regardless of
    workers — spawning a pool for one task burns ~2s of startup for no
    gain."""
    from monitorul_ii.extraction.backfills import (
        backfill_all_issuing_bodies_parallel,
    )

    sc = _report_sidecar(doc_id="mo://2014/II/1R", issuing_body="Consiliul Legislativ")
    p = _write(tmp_path, "report.extraction.json", sc)
    results = list(backfill_all_issuing_bodies_parallel([p], workers=8))
    assert len(results) == 1
    assert results[0].status == "filled"
    assert results[0].canonical_id == "consiliul_legislativ"


def test_worker_ignore_sigint_handler_installs_sig_ign():
    """The worker initializer must register SIG_IGN for SIGINT so a
    Ctrl+C in the parent doesn't propagate to workers mid-task. Verified
    by snapshotting the previous handler, calling the initializer, and
    confirming the new handler is `SIG_IGN`."""
    import signal

    from monitorul_ii.extraction.backfills import _worker_ignore_sigint

    # Snapshot the current handler so we restore it; pytest itself
    # may have its own SIGINT handler installed.
    prev = signal.getsignal(signal.SIGINT)
    try:
        _worker_ignore_sigint()
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, prev)


def test_run_in_parallel_generator_close_does_not_block_on_shutdown(
    tmp_path: Path, monkeypatch
):
    """When the caller stops iterating mid-stream (e.g. Ctrl+C in the
    parent), the parallel generator's finally block must call
    `executor.shutdown(wait=False, cancel_futures=True)` — NOT
    `wait=True`. wait=True would block the GeneratorExit cleanup on a
    join, which a second Ctrl+C would interrupt and dump a traceback."""
    import concurrent.futures

    from monitorul_ii.extraction.backfills import (
        backfill_all_persons_parallel,
    )

    # Spy on every shutdown call's kwargs.
    calls: list[dict] = []
    original_shutdown = concurrent.futures.ProcessPoolExecutor.shutdown

    def spy_shutdown(self, *args, **kwargs):
        calls.append(kwargs)
        return original_shutdown(self, *args, **kwargs)

    monkeypatch.setattr(
        concurrent.futures.ProcessPoolExecutor, "shutdown", spy_shutdown
    )

    # Two synthetic sidecars — enough to trigger the parallel branch.
    paths = []
    for i in range(2):
        sc = _plenary_with_speakers_sidecar(
            doc_id=f"mo://2018/II/{900 + i}",
            year=2018,
            speakers_in_chair=[
                _make_speaker(raw="Domnul Florin Iordache", name="Florin Iordache"),
            ],
        )
        paths.append(_write(tmp_path, f"plen{i}.extraction.json", sc))

    gen = backfill_all_persons_parallel(paths, workers=2)
    next(gen)  # consume one result so the generator is suspended mid-stream
    gen.close()  # simulate the GeneratorExit path on caller-side KbdInt

    assert calls, "shutdown() should have been called via the finally block"
    # The finally explicitly passes wait=False + cancel_futures=True so a
    # second Ctrl+C during cleanup can't interrupt a blocking join.
    last = calls[-1]
    assert last.get("wait") is False, (
        f"expected wait=False to avoid second-Ctrl+C race; got {last!r}"
    )
    assert last.get("cancel_futures") is True


def test_run_in_parallel_serial_path_does_not_create_pool(tmp_path: Path):
    """`workers=1` short-circuits to the serial generator without
    constructing a `ProcessPoolExecutor` — important so debug runs and
    single-task workloads don't pay the spawn-pool overhead AND don't
    spin up the SIGINT-ignoring child workers."""
    import concurrent.futures

    from monitorul_ii.extraction.backfills import (
        backfill_all_persons_parallel,
    )

    constructed = []
    original_init = concurrent.futures.ProcessPoolExecutor.__init__

    def spy_init(self, *args, **kwargs):
        constructed.append(kwargs)
        return original_init(self, *args, **kwargs)

    sc = _plenary_with_speakers_sidecar(
        doc_id="mo://2018/II/1000",
        year=2018,
        speakers_in_chair=[
            _make_speaker(raw="Domnul Florin Iordache", name="Florin Iordache"),
        ],
    )
    p = _write(tmp_path, "plen.extraction.json", sc)

    # Patch only for the duration of the call.
    try:
        concurrent.futures.ProcessPoolExecutor.__init__ = spy_init
        list(backfill_all_persons_parallel([p], workers=1))
    finally:
        concurrent.futures.ProcessPoolExecutor.__init__ = original_init

    assert constructed == [], (
        "workers=1 should short-circuit to serial without constructing a pool"
    )
