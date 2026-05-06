from __future__ import annotations

import argparse
from datetime import date

import pytest

from monitorul_ii.cli import (
    _ConvertProgressReporter,
    _convert_summary_line,
    _fmt_bytes,
    _fmt_duration,
    _parse_date,
    _redact_proxy,
)


# --- _parse_date -----------------------------------------------------------


def test_parse_date_iso():
    assert _parse_date("2026-04-29") == date(2026, 4, 29)


@pytest.mark.parametrize("bad", ["29-04-2026", "2026/04/29", "2026-13-01", "today"])
def test_parse_date_rejects_bad_input(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_date(bad)


# --- _redact_proxy ---------------------------------------------------------


def test_redact_proxy_masks_password():
    redacted = _redact_proxy("http://user:secret@host.example:33335")
    assert "secret" not in redacted
    assert "user" in redacted
    assert "host.example" in redacted
    assert "33335" in redacted
    assert "***" in redacted


def test_redact_proxy_unchanged_when_no_password():
    url = "http://host.example:33335"
    assert _redact_proxy(url) == url


def test_redact_proxy_user_only_no_password():
    """Proxy URLs with just a username and no password should not be changed."""
    url = "http://user@host.example:8080"
    assert _redact_proxy(url) == url


# --- _fmt_bytes ------------------------------------------------------------


def test_fmt_bytes_under_one_gb_uses_mb():
    assert _fmt_bytes(0) == "0.0 MB"
    assert _fmt_bytes(1_500_000) == "1.5 MB"
    assert _fmt_bytes(999_999_999) == "1000.0 MB"


def test_fmt_bytes_one_gb_and_above_uses_gb():
    assert _fmt_bytes(1_000_000_000) == "1.00 GB"
    assert _fmt_bytes(2_500_000_000) == "2.50 GB"


# --- _fmt_duration ---------------------------------------------------------


def test_fmt_duration_zero():
    assert _fmt_duration(0) == "00:00:00"


def test_fmt_duration_seconds_only():
    assert _fmt_duration(45) == "00:00:45"


def test_fmt_duration_minutes():
    assert _fmt_duration(90) == "00:01:30"


def test_fmt_duration_hours():
    assert _fmt_duration(3600 + 120 + 5) == "01:02:05"


def test_fmt_duration_truncates_fractional_seconds():
    assert _fmt_duration(45.9) == "00:00:45"


def test_fmt_duration_handles_long_runs():
    """26-year backfills: hours field needs to keep widening past 99h."""
    assert _fmt_duration(100 * 3600 + 0 * 60 + 0) == "100:00:00"


# --- _convert_summary_line -------------------------------------------------


def _zero_counters() -> dict[str, int]:
    return {
        "converted": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
    }


def test_convert_summary_line_drops_s3_when_idle():
    c = _zero_counters() | {"converted": 5, "skipped": 2}
    line = _convert_summary_line(c)
    assert line == "converted=5 skipped=2 errors=0"
    assert "s3" not in line


def test_convert_summary_line_includes_s3_when_active():
    c = _zero_counters() | {
        "converted": 5,
        "uploaded": 3,
        "in_bucket": 2,
    }
    line = _convert_summary_line(c)
    assert "converted=5" in line
    assert "s3 uploaded=3 in-bucket=2 errors=0" in line


def test_convert_summary_line_prefix_marks_interrupt():
    c = _zero_counters() | {"converted": 7, "errors": 1}
    line = _convert_summary_line(c, prefix="interrupted: ")
    assert line.startswith("interrupted: ")
    assert "converted=7" in line


# --- _ConvertProgressReporter (non-tty / heartbeat path) -------------------
#
# `capsys` replaces sys.stderr with a capture object whose isatty() returns
# False, so the reporter naturally takes the non-tty heartbeat branch in tests
# below. No extra tty-faking is needed.


def test_convert_progress_reporter_advance_no_tty(capsys):
    counters = _zero_counters()
    with _ConvertProgressReporter(total=10, counters=counters) as r:
        for _ in range(3):
            r.advance()
        assert r.done == 3


def test_convert_progress_reporter_print_no_tty(capsys):
    counters = _zero_counters()
    with _ConvertProgressReporter(total=2, counters=counters) as r:
        r.print("hello stdout")
        r.print("hello stderr", err=True)
    out = capsys.readouterr()
    assert "hello stdout" in out.out
    assert "hello stderr" in out.err


def test_convert_progress_reporter_heartbeat_threshold(monkeypatch, capsys):
    """Heartbeat fires once per _CONVERT_HEARTBEAT_EVERY in non-tty mode."""
    from monitorul_ii import cli

    monkeypatch.setattr(cli, "_CONVERT_HEARTBEAT_EVERY", 5)
    counters = _zero_counters()
    with _ConvertProgressReporter(total=20, counters=counters) as r:
        for _ in range(11):
            r.advance()
            counters["converted"] = r.done
    err = capsys.readouterr().err
    # Heartbeats at done=5 and done=10 (not at done=20 — that's the terminal state)
    assert err.count("progress:") == 2


def test_convert_progress_reporter_zero_total_is_safe(capsys):
    """Empty PDF list: reporter must construct cleanly even with total=0."""
    with _ConvertProgressReporter(total=0, counters=_zero_counters()) as r:
        assert r.done == 0


# --- convert --reverse parser wiring ---------------------------------------


def test_convert_parser_reverse_flag_defaults_false():
    from monitorul_ii.cli import _build_parser

    args = _build_parser().parse_args(["convert", "pdfs/"])
    assert args.reverse is False


def test_convert_parser_reverse_flag_sets_true():
    from monitorul_ii.cli import _build_parser

    args = _build_parser().parse_args(["convert", "pdfs/", "--reverse"])
    assert args.reverse is True


# --- _BackfillProgressReporter (non-tty / heartbeat path) -----------------


def _zero_backfill_counters() -> dict[str, int]:
    """Match the schema cmd_backfill seeds in cli.py."""
    return {
        "filled": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
    }


def test_backfill_progress_reporter_advance_no_tty(capsys):
    from monitorul_ii.cli import _BackfillProgressReporter

    counters = _zero_backfill_counters()
    with _BackfillProgressReporter(
        total=10, counters=counters, pass_label="persons"
    ) as r:
        for _ in range(3):
            r.advance()
        assert r.done == 3


def test_backfill_progress_reporter_print_no_tty(capsys):
    from monitorul_ii.cli import _BackfillProgressReporter

    counters = _zero_backfill_counters()
    with _BackfillProgressReporter(
        total=2, counters=counters, pass_label="persons"
    ) as r:
        r.print("hello stdout")
        r.print("hello stderr", err=True)
    out = capsys.readouterr()
    assert "hello stdout" in out.out
    assert "hello stderr" in out.err


def test_backfill_progress_reporter_heartbeat_threshold(monkeypatch, capsys):
    """Heartbeat fires once per `_BACKFILL_HEARTBEAT_EVERY` in non-tty mode."""
    from monitorul_ii import cli

    monkeypatch.setattr(cli, "_BACKFILL_HEARTBEAT_EVERY", 5)
    counters = _zero_backfill_counters()
    with cli._BackfillProgressReporter(
        total=20, counters=counters, pass_label="ministry"
    ) as r:
        for _ in range(11):
            r.advance()
            counters["filled"] = r.done
    err = capsys.readouterr().err
    # Heartbeats at done=5 and done=10 (not at done=20 — terminal state).
    assert err.count("progress:") == 2


def test_backfill_progress_reporter_heartbeat_carries_pass_label(monkeypatch, capsys):
    """The heartbeat description embeds the pass label so a multi-pass
    run is debuggable from a tail of stderr."""
    from monitorul_ii import cli

    monkeypatch.setattr(cli, "_BACKFILL_HEARTBEAT_EVERY", 1)
    counters = _zero_backfill_counters()
    with cli._BackfillProgressReporter(
        total=2, counters=counters, pass_label="proposed_by"
    ) as r:
        r.advance()
    err = capsys.readouterr().err
    assert "[proposed_by]" in err


def test_backfill_progress_reporter_zero_total_is_safe(capsys):
    """Empty sidecar list: reporter must construct cleanly even with total=0."""
    from monitorul_ii.cli import _BackfillProgressReporter

    with _BackfillProgressReporter(
        total=0, counters=_zero_backfill_counters(), pass_label="issuing_body"
    ) as r:
        assert r.done == 0


def test_backfill_progress_reporter_desc_includes_s3_when_active():
    """The bar description shows the s3 trio only after at least one
    upload has happened — quiet runs without S3 don't carry idle zeros."""
    from monitorul_ii.cli import _BackfillProgressReporter

    counters = _zero_backfill_counters() | {"filled": 3}
    with _BackfillProgressReporter(
        total=10, counters=counters, pass_label="persons"
    ) as r:
        assert "s3" not in r._desc()
        counters["uploaded"] = 1
        assert "s3" in r._desc()
