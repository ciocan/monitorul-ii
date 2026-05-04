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


# -- editor footer (added v0.1.x) ------------------------------------------


def test_claim_editor_footer_modern():
    """Trailing `**EDITOR: GUVERNUL ROMÂNIEI**` block (post-2010 docs)."""
    body = (
        "## **Domnul X:**\nText.\n_(Aplauze.)_\n\n"
        '**EDITOR: GUVERNUL ROMÂNIEI** „Monitorul Oficial" R.A., '
        "Str. Parcului nr. 65, sectorul 1, București."
    )
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.editor_footer" in reasons


def test_claim_editor_footer_subscription_block():
    """Some docs lead the trailing block with the subscription rate-card
    `**A B O N A M E N T E ...**` instead of `**EDITOR:**`."""
    body = (
        "## **Domnul X:**\nText.\n\n"
        "**A B O N A M E N T E   L A   P U B L I C A Ț I I L E "
        "O F I C I A L E** — Prețuri pentru anul 2015 — content..."
    )
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.editor_footer" in reasons


def test_claim_editor_footer_quirk_no_match_inline():
    """An inline mention of `EDITOR:` mid-body (not bold-prefixed) MUST
    NOT match the footer regex — the `**` prefix is required."""
    body = "regular text mentioning EDITOR: not bolded should not match."
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.editor_footer" not in reasons


# -- mojibake variants -----------------------------------------------------


def test_claim_joint_session_header_mojibake():
    """Pre-2008 PDF→MD conversions emit `ŞEDINŢE COMUNE ALE CAMEREI
    DEPUTAŢILOR ŞI SENATULUI` (cedilla Ş/Ţ instead of Ș/Ț)."""
    body = (
        "**ŞEDINŢE COMUNE ALE CAMEREI DEPUTAŢILOR ŞI SENATULUI** "
        "SESIUNEA I ORDINARĂ – FEBRUARIE 2008\n"
    )
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.joint_session_header" in reasons


def test_claim_sumar_keyword_with_markdown_prefix():
    """`## SUMAR` (PDF→MD heading-promoted form) must also be claimed."""
    body = "header\n## SUMAR\n|table|\n"
    claims = claim_plenary_boilerplate(body)
    reasons = {c.reason for c in claims}
    assert "plenary_stenogram.sumar_keyword" in reasons
