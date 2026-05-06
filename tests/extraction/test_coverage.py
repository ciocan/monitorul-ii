from __future__ import annotations

from monitorul_ii.extraction.coverage import (
    COVERAGE_VERSION,
    Claim,
    compute_coverage,
    line_for_offset,
    line_offsets,
    lines_for_range,
    make_boilerplate_claim,
    make_record_claim,
)


def test_coverage_version_format():
    parts = COVERAGE_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_line_for_offset_basic():
    text = "line1\nline2\nline3\n"
    offsets = line_offsets(text)
    assert line_for_offset(0, offsets) == 1
    assert line_for_offset(5, offsets) == 1  # the \n at end of line 1
    assert line_for_offset(6, offsets) == 2  # first char of line 2
    assert line_for_offset(11, offsets) == 2  # newline ending line 2


def test_lines_for_range_inclusive():
    text = "a\nb\nc\nd\n"
    offsets = line_offsets(text)
    # span covering "a\nb" → lines 1..2
    assert lines_for_range((0, 3), offsets) == (1, 2)
    # whole doc
    assert lines_for_range((0, 8), offsets) == (1, 4)


def test_compute_coverage_full_claim():
    body = "Hello world.\n"
    claim = make_record_claim((0, len(body)), line_offsets(body))
    cov = compute_coverage(body, [claim])
    assert cov["body_chars"] == len(body)
    assert cov["claimed_chars"] == len(body)
    assert cov["claimed_pct"] == 1.0
    assert cov["gaps"] == []


def test_compute_coverage_partial_with_gap():
    body = "AAAAA\n" + "B" * 100 + "\nCCCCC\n"  # 5 + 1 + 100 + 1 + 5 + 1 = 113
    offsets = line_offsets(body)
    # Claim only the first 5 chars + the last 6 chars
    claims = [
        make_record_claim((0, 5), offsets),
        make_record_claim((len(body) - 6, len(body)), offsets),
    ]
    cov = compute_coverage(body, claims)
    assert cov["body_chars"] == len(body)
    assert cov["claimed_chars"] == 5 + 6
    assert 0.05 < cov["claimed_pct"] < 0.15
    assert len(cov["gaps"]) == 1
    assert cov["gaps"][0]["preview"].startswith("B")


def test_compute_coverage_short_gap_dropped():
    """Gaps below 20 chars are dropped (whitespace / page-residue heuristic)."""
    body = "X" * 100 + "ggggg" + "Y" * 100  # 5-char gap between two claims
    offsets = line_offsets(body)
    claims = [
        make_record_claim((0, 100), offsets),
        make_record_claim((105, 205), offsets),
    ]
    cov = compute_coverage(body, claims)
    # The 5-char gap is below the 20-char threshold; gaps list is empty.
    assert cov["gaps"] == []


def test_compute_coverage_whitespace_only_gap_dropped():
    body = "X" * 100 + " " * 50 + "Y" * 100  # 50 whitespace chars
    offsets = line_offsets(body)
    claims = [
        make_record_claim((0, 100), offsets),
        make_record_claim((150, 250), offsets),
    ]
    cov = compute_coverage(body, claims)
    assert cov["gaps"] == []


def test_compute_coverage_overlapping_claims_merged():
    body = "X" * 100
    offsets = line_offsets(body)
    claims = [
        make_record_claim((0, 60), offsets),
        make_record_claim((40, 80), offsets),  # overlaps with above
    ]
    cov = compute_coverage(body, claims)
    assert cov["claimed_chars"] == 80  # union, not sum (which would be 100)


def test_claimed_by_policy_separated_from_records():
    body = "X" * 100
    offsets = line_offsets(body)
    claims = [
        make_record_claim((0, 50), offsets),
        make_boilerplate_claim((50, 100), "shared.test", offsets),
    ]
    cov = compute_coverage(body, claims)
    assert cov["claimed_pct"] == 1.0
    assert len(cov["claimed_by_policy"]) == 1
    assert cov["claimed_by_policy"][0]["reason"] == "shared.test"


def test_claim_kind_required():
    """Claim is frozen + dataclass with kind/reason; smoke check."""
    c = Claim(chars=(0, 10), lines=(1, 1), kind="record")
    assert c.kind == "record"
    assert c.reason is None


def test_compute_coverage_empty_body():
    cov = compute_coverage("", [])
    assert cov["body_chars"] == 0
    assert cov["claimed_chars"] == 0
    assert cov["claimed_pct"] == 1.0
    assert cov["gaps"] == []
