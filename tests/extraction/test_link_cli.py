"""Tests for the `link` argparse subcommand wiring.

Parser-level tests cover flag validation and routing; the actual linker
behaviour is exercised in `test_linker.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from monitorul_ii.cli import _build_parser, cmd_link


def test_link_subcommand_in_parser():
    p = _build_parser()
    args = p.parse_args(["link", "some/dir"])
    assert args.command == "link"
    assert args.paths == [Path("some/dir")]


def test_link_force_flag():
    p = _build_parser()
    args = p.parse_args(["link", "x", "--force"])
    assert args.force is True


def test_link_dry_run_flag():
    p = _build_parser()
    args = p.parse_args(["link", "x", "--dry-run"])
    assert args.dry_run is True


def test_link_reuses_s3_args():
    p = _build_parser()
    args = p.parse_args(["link", "x", "--no-upload"])
    assert args.no_upload is True


def test_link_report_only_flag():
    p = _build_parser()
    args = p.parse_args(["link", "x", "--report-only"])
    assert args.report_only is True
    assert args.vote_only is False
    assert args.xref_only is False


def test_link_vote_only_flag():
    p = _build_parser()
    args = p.parse_args(["link", "x", "--vote-only"])
    assert args.vote_only is True
    assert args.report_only is False
    assert args.xref_only is False


def test_link_xref_only_flag():
    p = _build_parser()
    args = p.parse_args(["link", "x", "--xref-only"])
    assert args.xref_only is True
    assert args.report_only is False
    assert args.vote_only is False


def test_link_pass_selectors_mutually_exclusive():
    p = _build_parser()
    import pytest

    with pytest.raises(SystemExit):
        p.parse_args(["link", "x", "--report-only", "--vote-only"])
    with pytest.raises(SystemExit):
        p.parse_args(["link", "x", "--report-only", "--xref-only"])
    with pytest.raises(SystemExit):
        p.parse_args(["link", "x", "--vote-only", "--xref-only"])


def test_cmd_link_no_sidecars_returns_zero(tmp_path, capsys):
    """Empty input dir → exit 0 with a message, not a crash."""

    class A:
        pass

    args = A()
    args.paths = [tmp_path]
    args.force = False
    args.dry_run = False
    args.report_only = False
    args.vote_only = False
    args.xref_only = False
    args.no_upload = True
    args.bucket = None
    rc = cmd_link(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "no .extraction.json" in err


def test_cmd_link_walks_directory_and_links(tmp_path, capsys):
    """End-to-end: drop a forged report sidecar + matching joint sidecar
    in tmp_path, run cmd_link, verify the report's received_in_document
    got populated."""

    def _envelope(doc_id, doc_type, session_date):
        return {
            "schema_version": "1.12.0",
            "document_id": doc_id,
            "content_sha": "0123456789ab",
            "document_type": doc_type,
            "metadata": {
                "issue": doc_id.split("/")[-1],
                "year": int(session_date[:4]),
                "part": "II",
                "published": session_date,
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

    joint = _envelope("mo://2013/II/95", "plenary_joint_session", "2013-12-04")
    joint["body"] = {
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
    report = _envelope("mo://2014/II/1R", "report_facsimile", "2014-01-20")
    report["metadata"]["chamber"] = "Camera Deputaților"
    report["body"] = {
        "report": {
            "title": "Raportul X în anul 2010",
            "issuing_body": "X",
            "issuing_body_normalized": None,
            "reporting_period": {"start": "2010-01-01", "end": "2010-12-31"},
            "received_at": {
                "session_kind": "joint",
                "session_date": "2013-12-04",
                "received_in_document": None,
            },
        },
        "headings": [],
        "raw_markdown_excerpt": "",
    }

    joint_p = tmp_path / "joint.extraction.json"
    joint_p.write_text(json.dumps(joint), encoding="utf-8")
    report_p = tmp_path / "report.extraction.json"
    report_p.write_text(json.dumps(report), encoding="utf-8")

    class A:
        pass

    args = A()
    args.paths = [tmp_path]
    args.force = False
    args.dry_run = False
    args.report_only = False
    args.vote_only = False
    args.xref_only = False
    args.no_upload = True
    args.bucket = None
    rc = cmd_link(args)
    assert rc == 0

    after = json.loads(report_p.read_text(encoding="utf-8"))
    assert (
        after["body"]["report"]["received_at"]["received_in_document"]
        == "mo://2013/II/95"
    )
    out = capsys.readouterr().out
    assert "linked=1" in out


def test_cmd_link_vote_only_pass_pairs_deferred_with_resolver(tmp_path, capsys):
    """End-to-end CLI: drop two stenogram sidecars (deferred + resolver) in
    tmp_path; run link --vote-only; verify the pair landed."""

    def _vote_act(outcome: str) -> dict:
        return {
            "type": "vote",
            "motion_text": "x",
            "motion_type": "final",
            "voting_method": "electronic",
            "timing": "deferred" if outcome == "deferred" else "live",
            "counts": {
                "for": None if outcome == "deferred" else 200,
                "against": None if outcome == "deferred" else 10,
                "abstain": None if outcome == "deferred" else 0,
                "not_voting": None,
                "total_voting": None if outcome == "deferred" else 210,
            },
            "outcome": outcome,
            "quorum_announced": None,
            "proposed_by": None,
            "nominal_breakdown": None,
            "defers_to": None,
            "resolves": [],
            "source_span": {
                "chars": [0, 100],
                "lines": [1, 2],
                "content_sha": "0123456789ab",
            },
            "extraction": {
                "extractor": "regex@1",
                "confidence": 0.9,
                "source_span": {
                    "chars": [0, 100],
                    "lines": [1, 2],
                    "content_sha": "0123456789ab",
                },
            },
        }

    def _bill() -> dict:
        return {
            "type": "bill",
            "raw": "PL-x 555/2025",
            "char_offsets": [0, 13],
            "prefix": "PL-x",
            "number": "555",
            "year": 2025,
            "secondary_year": None,
            "chamber_of_origin": "camera",
            "procedure": None,
            "subject": None,
        }

    def _agenda(vote: dict) -> dict:
        return {
            "ordinal": 1,
            "title": "x",
            "primary_references": [_bill()],
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

    def _stenogram(doc_id: str, session_date: str, outcome: str) -> dict:
        return {
            "schema_version": "1.12.0",
            "document_id": doc_id,
            "content_sha": "0123456789ab",
            "document_type": "plenary_stenogram",
            "metadata": {
                "issue": doc_id.split("/")[-1],
                "year": int(session_date[:4]),
                "part": "II",
                "published": session_date,
                "chamber": "Camera Deputaților",
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
                "agenda_items": [_agenda(_vote_act(outcome))],
                "interpellations": [],
            },
        }

    deferring = _stenogram("mo://2025/II/A", "2025-04-01", "deferred")
    resolving = _stenogram("mo://2025/II/B", "2025-04-15", "approved")
    pa = tmp_path / "a.extraction.json"
    pa.write_text(json.dumps(deferring), encoding="utf-8")
    pb = tmp_path / "b.extraction.json"
    pb.write_text(json.dumps(resolving), encoding="utf-8")

    class A:
        pass

    args = A()
    args.paths = [tmp_path]
    args.force = False
    args.dry_run = False
    args.report_only = False
    args.vote_only = True
    args.xref_only = False
    args.no_upload = True
    args.bucket = None
    rc = cmd_link(args)
    assert rc == 0

    after_a = json.loads(pa.read_text(encoding="utf-8"))
    after_b = json.loads(pb.read_text(encoding="utf-8"))
    assert (
        after_a["body"]["agenda_items"][0]["activities"][0]["defers_to"]
        == "mo://2025/II/B"
    )
    assert after_b["body"]["agenda_items"][0]["activities"][0]["resolves"] == [
        "mo://2025/II/A"
    ]
    out = capsys.readouterr().out
    assert "linked=2" in out


def test_cmd_link_xref_only_pass_resolves_art_n(tmp_path, capsys):
    """End-to-end CLI: drop a stenogram sidecar carrying a Legea anchor +
    an art-N unknown in the same agenda item's primary_references; run
    `link --xref-only`; verify the unknown's resolved_to landed."""

    body = "Conform Legea nr. 47/1992, art. 25 este aplicabil aici."
    md = tmp_path / "x.md"
    md.write_text(
        '---\nissue: "1"\nyear: 2025\npart: "II"\npublished: 2025-04-01\n---\n' + body,
        encoding="utf-8",
    )
    law_start = body.index("Legea")
    law_end = body.index(",")
    art_start = body.index("art. 25")
    art_end = art_start + len("art. 25")
    primary_refs = [
        {
            "type": "law",
            "raw": "Legea nr. 47/1992",
            "char_offsets": [law_start, law_end],
            "number": "47",
            "year": 1992,
            "subject": None,
        },
        {
            "type": "unknown",
            "raw": "art. 25",
            "char_offsets": [art_start, art_end],
            "hint": "law-ish",
        },
    ]
    agenda_item = {
        "ordinal": 1,
        "title": "Test agenda",
        "primary_references": primary_refs,
        "category": "bill_debate",
        "confidence_type": None,
        "requested_by_group": None,
        "outcome": None,
        "reexamination_reason": None,
        "pages_in_pdf": [],
        "topics": {"primary": [], "secondary": []},
        "activities": [],
        "source_span": {
            "chars": [0, len(body)],
            "lines": [1, 5],
            "content_sha": "0123456789ab",
        },
        "extraction": {
            "extractor": "regex@1",
            "confidence": 0.9,
            "source_span": {
                "chars": [0, len(body)],
                "lines": [1, 5],
                "content_sha": "0123456789ab",
            },
        },
    }
    sc = {
        "schema_version": "1.12.0",
        "document_id": "mo://2025/II/X",
        "content_sha": "0123456789ab",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": "X",
            "year": 2025,
            "part": "II",
            "published": "2025-04-01",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": "2025-04-01",
            "legislature": None,
        },
        "raw_markdown_path": str(md),
        "raw_pdf_path": "x.pdf",
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2026-05-04T12:00:00Z",
            "extractor_versions": {"boilerplate": "0.1.0"},
            "confidence": 0.9,
        },
        "coverage": {
            "body_chars": len(body),
            "claimed_chars": len(body),
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
            "agenda_items": [agenda_item],
            "interpellations": [],
        },
    }
    sp = tmp_path / "x.extraction.json"
    sp.write_text(json.dumps(sc), encoding="utf-8")

    class A:
        pass

    args = A()
    args.paths = [tmp_path]
    args.force = False
    args.dry_run = False
    args.report_only = False
    args.vote_only = False
    args.xref_only = True
    args.no_upload = True
    args.bucket = None
    rc = cmd_link(args)
    assert rc == 0

    after = json.loads(sp.read_text(encoding="utf-8"))
    unknown_ref = after["body"]["agenda_items"][0]["primary_references"][1]
    assert unknown_ref["resolved_to"] == {"char_offsets": [law_start, law_end]}
    out = capsys.readouterr().out
    assert "linked=1" in out
