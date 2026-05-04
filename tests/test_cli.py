from __future__ import annotations

import argparse
from datetime import date

import pytest

from monitorul_ii.cli import (
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
