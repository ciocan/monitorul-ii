"""Unit tests for plenary-specific boilerplate patterns."""

from __future__ import annotations

from monitorul_ii.extraction.extractors.plenary.boilerplate import (
    claim_plenary_boilerplate,
)


def test_claim_partea_bold_only():
    body = "**PA R T E A  A  I I - A DEZBATERI PARLAMENTARE**\n"
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.partea_header_bold_only" in reasons


def test_claim_joint_session_header():
    body = (
        "Some preamble\n"
        "**ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI** "
        "SESIUNEA A II-A ORDINARĂ – SEPTEMBRIE 2025\n"
    )
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.joint_session_header" in reasons


def test_claim_sumar_keyword():
    body = "header\nSUMAR\n|table|\n"
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.sumar_keyword" in reasons


def test_claim_chair_address_opener():
    body = "## Doamnelor și domnilor deputați,\nText.\n"
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.chair_address_opener" in reasons


def test_no_claims_for_unrelated_body():
    body = "Just some plain text content with no boilerplate patterns."
    claims = claim_plenary_boilerplate(body)
    assert claims == []


def test_each_claim_has_valid_span():
    body = "**PA R T E A  A  I I - A DEZBATERI PARLAMENTARE**\nSUMAR\n"
    claims = claim_plenary_boilerplate(body)
    for c in claims:
        assert c.chars[0] < c.chars[1]
        assert c.lines[0] <= c.lines[1]
        assert c.kind == "boilerplate"
        assert c.reason is not None
