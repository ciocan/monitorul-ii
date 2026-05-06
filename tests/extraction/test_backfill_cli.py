"""Tests for the `backfill` argparse subcommand wiring.

Parser-level tests cover flag validation and routing; the actual
backfill behaviour is exercised in `test_backfills.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from monitorul_ii.cli import _build_parser, cmd_backfill
from monitorul_ii.extraction.identity import assign_identity


def _report_sidecar(doc_id: str, issuing_body: str | None) -> dict:
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
    sc["extraction"]["identity"] = assign_identity(
        sc["body"],
        doc_type=sc["document_type"],
        doc_id=sc["document_id"],
        year=sc["metadata"]["year"],
        issue=sc["metadata"]["issue"],
    )
    return sc


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
        p.parse_args(["backfill", "x", "--kind=nope"])


def test_backfill_kind_persons_accepted():
    """The persons pass is wired into the --kind enum."""
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--kind=persons"])
    assert args.kind == "persons"


def test_backfill_workers_flag_defaults_to_cpu_count():
    """`-j` / `--workers` defaults to os.cpu_count() so the default
    invocation parallelizes without the user opting in."""
    import os as _os

    p = _build_parser()
    args = p.parse_args(["backfill", "x"])
    assert args.workers == (_os.cpu_count() or 1)


def test_backfill_workers_flag_explicit():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "-j", "4"])
    assert args.workers == 4


def test_backfill_workers_flag_long_form():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--workers", "2"])
    assert args.workers == 2


def test_backfill_workers_one_means_serial():
    """Explicit `-j 1` is preserved (not auto-bumped) — important for
    deterministic output ordering during debugging."""
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "-j", "1"])
    assert args.workers == 1


def test_backfill_reupload_on_skip_flag_defaults_false():
    p = _build_parser()
    args = p.parse_args(["backfill", "x"])
    assert args.reupload_on_skip is False


def test_backfill_reupload_on_skip_flag_sets_true():
    p = _build_parser()
    args = p.parse_args(["backfill", "x", "--reupload-on-skip"])
    assert args.reupload_on_skip is True


def test_should_upload_after_backfill_filled_always_uploads():
    """Default `reupload_on_skip=False`: status="filled" still uploads."""
    from types import SimpleNamespace

    from monitorul_ii.cli import _should_upload_after_backfill

    r = SimpleNamespace(status="filled", reason=None)
    assert _should_upload_after_backfill(r, reupload_on_skip=False) is True
    assert _should_upload_after_backfill(r, reupload_on_skip=True) is True


def test_should_upload_after_backfill_skip_default_does_not_upload():
    """Default: status="skip" never uploads, even on 'already filled'."""
    from types import SimpleNamespace

    from monitorul_ii.cli import _should_upload_after_backfill

    r = SimpleNamespace(status="skip", reason="already filled (5 speakers)")
    assert _should_upload_after_backfill(r, reupload_on_skip=False) is False


def test_should_upload_after_backfill_skip_with_flag_uploads_already_filled():
    """`reupload_on_skip=True`: 'already filled' skip path triggers an
    upload — closes the historical bucket-staleness gap."""
    from types import SimpleNamespace

    from monitorul_ii.cli import _should_upload_after_backfill

    # All four pass-specific reason formats start with "already filled".
    for reason in (
        "already filled with same canonical id",  # issuing_body
        "already filled (3 records)",  # ministry
        "already filled (12 eligible votes)",  # proposed_by
        "already filled (107 speakers)",  # persons
    ):
        r = SimpleNamespace(status="skip", reason=reason)
        assert _should_upload_after_backfill(r, reupload_on_skip=True) is True, (
            f"reason {reason!r} should trigger upload"
        )


def test_should_upload_after_backfill_skip_with_flag_does_not_upload_other_reasons():
    """`reupload_on_skip=True` is precision-targeted: other skip reasons
    mean the local file simply has no data this pass would emit, so the
    bucket can't be 'stale' relative to one. We don't upload on those."""
    from types import SimpleNamespace

    from monitorul_ii.cli import _should_upload_after_backfill

    for reason in (
        "no raw value (12 records)",
        "no speakers in body",
        "no registry match (5 records)",
        "no government-proposed agendas",
        "mismatch (force off) (3 records)",
    ):
        r = SimpleNamespace(status="skip", reason=reason)
        assert _should_upload_after_backfill(r, reupload_on_skip=True) is False, (
            f"reason {reason!r} should NOT trigger upload"
        )


def test_should_upload_after_backfill_error_never_uploads():
    """Workers that errored never upload — the local file may be in any
    state and we don't want to push half-baked bytes."""
    from types import SimpleNamespace

    from monitorul_ii.cli import _should_upload_after_backfill

    r = SimpleNamespace(status="error", reason="schema validation failed")
    assert _should_upload_after_backfill(r, reupload_on_skip=False) is False
    assert _should_upload_after_backfill(r, reupload_on_skip=True) is False


def test_cmd_backfill_keyboard_interrupt_installs_hard_exit_handler(
    tmp_path: Path, monkeypatch
):
    """First Ctrl+C: print summary + return 130 + install a hard-exit
    SIGINT handler for the next signal. The handler must be a callable
    distinct from the default — that's what gives the user an escape
    hatch when the multiprocessing atexit join takes too long.
    """
    import signal as _signal
    from types import SimpleNamespace

    from monitorul_ii import cli

    # Stub the persons backfill helper to raise KbdInt straight away —
    # simulates the user hitting Ctrl+C at any point during the run.
    def _raise_kbd(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_persons_backfill", _raise_kbd)

    # Drop a single trivial sidecar so _collect_sidecars returns
    # something — the actual content doesn't matter, the helper is
    # stubbed.
    p = tmp_path / "x.extraction.json"
    p.write_text("{}", encoding="utf-8")

    # Snapshot the current SIGINT handler so we can assert the cmd
    # changed it AND restore for pytest's own signal handling.
    prev_sigint = _signal.getsignal(_signal.SIGINT)
    try:
        args = SimpleNamespace(
            paths=[tmp_path],
            kind="persons",
            force=False,
            dry_run=False,
            no_upload=True,
            bucket=None,
            workers=1,
            reupload_on_skip=False,
        )
        rc = cli.cmd_backfill(args)
        assert rc == 130

        new_handler = _signal.getsignal(_signal.SIGINT)
        # Handler must be installed and must NOT be SIG_IGN — that was
        # the previous (over-aggressive) fix that locked users out
        # entirely. Hard-exit handler is callable.
        assert new_handler != prev_sigint, "SIGINT handler should change"
        assert new_handler != _signal.SIG_IGN, (
            "SIGINT must not be ignored — leaves no escape hatch"
        )
        assert callable(new_handler), "expected a callable hard-exit handler"
    finally:
        _signal.signal(_signal.SIGINT, prev_sigint)


def _plenary_sidecar_with_speaker(doc_id: str, year: int) -> dict:
    """Tiny plenary sidecar with one chair Speaker — for the persons CLI test."""
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
            "body_chars": 100,
            "claimed_chars": 100,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
        "body": {
            "session": {
                "chair": [
                    {
                        "raw": "Domnul Florin Iordache",
                        "name": "Florin Iordache",
                        "title": None,
                        "role": None,
                        "party_group": None,
                        "person_id": None,
                    }
                ],
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
    sc["extraction"]["identity"] = assign_identity(
        sc["body"],
        doc_type=sc["document_type"],
        doc_id=sc["document_id"],
        year=sc["metadata"]["year"],
        issue=sc["metadata"]["issue"],
    )
    return sc


def test_cmd_backfill_persons_kind_fills_chair_speaker(tmp_path: Path, capsys):
    """End-to-end: --kind=persons over a plenary sidecar fills the chair
    Speaker's person_id from persons.json."""
    sc = _plenary_sidecar_with_speaker("mo://2018/II/100", 2018)
    p = tmp_path / "plen.extraction.json"
    p.write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8")

    args = SimpleNamespace(
        paths=[tmp_path],
        kind="persons",
        force=False,
        dry_run=False,
        no_upload=True,
        bucket=None,
    )
    rc = cmd_backfill(args)
    assert rc == 0

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["body"]["session"]["chair"][0]["person_id"] == "iordache-florin"

    out = capsys.readouterr().out
    assert "filled=1" in out


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
