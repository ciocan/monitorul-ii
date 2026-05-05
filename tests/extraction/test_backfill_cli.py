"""Tests for the `backfill` argparse subcommand wiring.

Parser-level tests cover flag validation and routing; the actual
backfill behaviour is exercised in `test_backfills.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from monitorul_ii.cli import _build_parser, cmd_backfill


def _report_sidecar(doc_id: str, issuing_body: str | None) -> dict:
    return {
        "schema_version": "1.12.0",
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
        },
    }


# -- parser-level -----------------------------------------------------------


def test_backfill_subcommand_in_parser():
    p = _build_parser()
    args = p.parse_args(["backfill", "some/dir"])
    assert args.command == "backfill"
    assert args.paths == [Path("some/dir")]
    assert args.kind == "all"  # default


def test_backfill_kind_choice():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--kind=issuing_body"])
    assert args.kind == "issuing_body"


def test_backfill_dry_run_flag():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--dry-run"])
    assert args.dry_run is True


def test_backfill_force_flag():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--force"])
    assert args.force is True


def test_backfill_reuses_s3_args():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--no-upload"])
    assert args.no_upload is True


def test_backfill_kind_rejects_unknown_choice():
    p = _build_parser()
    import pytest

    with pytest.raises(SystemExit):
        p.parse_args(["backfill", "x", "--kind=person"])


# -- end-to-end on tmp directory --------------------------------------------


def test_cmd_backfill_no_sidecars_returns_zero(tmp_path: Path, capsys):
    args = SimpleNamespace(
        paths=[tmp_path],
        kind="all",
        force=False,
        dry_run=False,
        no_upload=True,
        bucket=None,
    )
    rc = cmd_backfill(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "no .extraction.json" in err


def test_cmd_backfill_walks_directory_and_fills(tmp_path: Path, capsys):
    sc = _report_sidecar("mo://2014/II/1R", "Consiliul Legislativ")
    p = tmp_path / "report.extraction.json"
    p.write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8")

    args = SimpleNamespace(
        paths=[tmp_path],
        kind="all",
        force=False,
        dry_run=False,
        no_upload=True,
        bucket=None,
    )
    rc = cmd_backfill(args)
    assert rc == 0

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert (
        on_disk["body"]["report"]["issuing_body_normalized"] == "consiliul_legislativ"
    )

    out = capsys.readouterr().out
    assert "filled=1" in out


def test_cmd_backfill_dry_run_does_not_write(tmp_path: Path, capsys):
    sc = _report_sidecar("mo://2014/II/2R", "Consiliul Legislativ")
    p = tmp_path / "report.extraction.json"
    p.write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8")

    args = SimpleNamespace(
        paths=[tmp_path],
        kind="all",
        force=False,
        dry_run=True,
        no_upload=True,
        bucket=None,
    )
    rc = cmd_backfill(args)
    assert rc == 0
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["report"]["issuing_body_normalized"] is None


def test_cmd_backfill_force_re_runs_on_populated(tmp_path: Path, capsys):
    sc = _report_sidecar("mo://2014/II/3R", "Consiliul Legislativ")
    sc["body"]["report"]["issuing_body_normalized"] = "csat"  # wrong on purpose
    p = tmp_path / "report.extraction.json"
    p.write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8")

    # Without --force: skip with mismatch reason, file unchanged.
    args = SimpleNamespace(
        paths=[tmp_path],
        kind="all",
        force=False,
        dry_run=False,
        no_upload=True,
        bucket=None,
    )
    rc = cmd_backfill(args)
    assert rc == 0
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["report"]["issuing_body_normalized"] == "csat"

    # With --force: overwrite to consiliul_legislativ.
    args.force = True
    rc = cmd_backfill(args)
    assert rc == 0
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert (
        on_disk["body"]["report"]["issuing_body_normalized"] == "consiliul_legislativ"
    )
