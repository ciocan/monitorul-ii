"""Tests for the `extract` argparse subcommand wiring.

These exercise the CLI parser and dispatch — the parser is responsible for
validating flag combinations and routing to `cmd_extract`. The actual
extraction is exercised in `test_pipeline.py` and `test_question_register.py`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from monitorul_ii.cli import _build_parser, cmd_extract


def test_extract_subcommand_in_parser():
    p = _build_parser()
    args = p.parse_args(["extract", "some/path.md"])
    assert args.command == "extract"
    assert args.paths == [Path("some/path.md")]


def test_extract_force_flag():
    p = _build_parser()
    args = p.parse_args(["extract", "x.md", "--force"])
    assert args.force is True


def test_extract_type_flag_validates_choices():
    p = _build_parser()
    args = p.parse_args(["extract", "x.md", "--type", "question_register"])
    assert args.override_type == "question_register"


def test_extract_type_flag_rejects_unknown_type():
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["extract", "x.md", "--type", "bogus_type"])


def test_extract_coverage_below_flag():
    p = _build_parser()
    args = p.parse_args(["extract", "x.md", "--coverage-below", "0.95"])
    assert args.coverage_below == 0.95


def test_extract_default_no_coverage_below():
    p = _build_parser()
    args = p.parse_args(["extract", "x.md"])
    assert args.coverage_below is None


def test_extract_reverse_flag():
    p = _build_parser()
    args = p.parse_args(["extract", "x.md", "--reverse"])
    assert args.reverse is True


def test_extract_reuses_s3_args():
    p = _build_parser()
    args = p.parse_args(["extract", "x.md", "--no-upload"])
    assert args.no_upload is True


def test_cmd_extract_no_mds_returns_zero(tmp_path, capsys):
    """Empty input → exit 0 with a message, not a crash."""

    class A:
        pass

    args = A()
    args.paths = [tmp_path]  # empty dir
    args.reverse = False
    args.no_upload = True
    args.bucket = None
    args.force = False
    args.override_type = None
    args.coverage_below = None
    rc = cmd_extract(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "no MDs found" in err


def test_cmd_extract_writes_sidecar_for_qr_md(tmp_path, monkeypatch):
    """Smoke: copy a fixture MD into tmp_path, run cmd_extract, verify the
    sidecar lands next to it and validates."""
    src = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    if not src.exists():
        pytest.skip("fixture MD not present")
    dst = tmp_path / src.name
    shutil.copy(src, dst)

    class A:
        pass

    args = A()
    args.paths = [dst]
    args.reverse = False
    args.no_upload = True
    args.bucket = None
    args.force = False
    args.override_type = None
    args.coverage_below = None

    rc = cmd_extract(args)
    assert rc == 0

    sidecar = dst.parent / f"{dst.stem}.extraction.json"
    assert sidecar.exists()
    sc = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sc["document_type"] == "question_register"
    assert sc["schema_version"] == "1.11.0"
    assert sc["coverage"]["claimed_pct"] >= 0.95


def test_cmd_extract_coverage_below_emits_jsonl(tmp_path, capsys):
    """`--coverage-below 0.999999` should emit a JSONL row to stdout for
    every doc whose claimed_pct is below 0.999999 (i.e., basically all of
    them, which lets us assert the format without a coverage-low fixture)."""
    src = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    if not src.exists():
        pytest.skip("fixture MD not present")
    dst = tmp_path / src.name
    shutil.copy(src, dst)

    class A:
        pass

    args = A()
    args.paths = [dst]
    args.reverse = False
    args.no_upload = True
    args.bucket = None
    args.force = True
    args.override_type = None
    args.coverage_below = 0.999999

    cmd_extract(args)
    out = capsys.readouterr().out
    # Find any line that parses as JSON with the expected keys
    json_rows = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "claimed_pct" in row and "doc_type" in row:
            json_rows.append(row)
    assert json_rows, f"no coverage-outlier rows found in stdout:\n{out}"
    assert json_rows[0]["doc_type"] == "question_register"
