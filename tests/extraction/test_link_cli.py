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


def test_cmd_link_no_sidecars_returns_zero(tmp_path, capsys):
    """Empty input dir → exit 0 with a message, not a crash."""

    class A:
        pass

    args = A()
    args.paths = [tmp_path]
    args.force = False
    args.dry_run = False
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
            "schema_version": "1.10.0",
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
