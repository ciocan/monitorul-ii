from __future__ import annotations

from monitorul_ii.extraction.boilerplate import (
    BOILERPLATE_VERSION,
    claim_shared_boilerplate,
)


def test_boilerplate_version_format():
    parts = BOILERPLATE_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_claim_year_issue_banner():
    body = "Anul 194 (XXXVII) — Nr. 29\n\nbody\n"
    claims = claim_shared_boilerplate(body)
    reasons = [c.reason for c in claims]
    assert "shared_boilerplate.year_issue_banner" in reasons


def test_claim_weekday_date_line():
    body = "Joi, 11 iulie 2013\n\nbody\n"
    claims = claim_shared_boilerplate(body)
    reasons = [c.reason for c in claims]
    assert "shared_boilerplate.weekday_date_line" in reasons


def test_claim_partea_header_h1_and_h2():
    body_h1 = "# **PA R T E A  A  I I - A DEZBATERI PARLAMENTARE**\n"
    body_h2 = "## **PA R T E A  A  I I - A DEZBATERI PARLAMENTARE**\n"
    assert any(
        c.reason == "shared_boilerplate.partea_header"
        for c in claim_shared_boilerplate(body_h1)
    )
    assert any(
        c.reason == "shared_boilerplate.partea_header"
        for c in claim_shared_boilerplate(body_h2)
    )


def test_claim_chamber_heading_h1_and_h2():
    """2012-era docs render the chamber heading at H2; modern docs at H1."""
    h1 = "# **CAMERA DEPUTAȚILOR**\n"
    h2 = "## **SENATUL**\n"
    h2_old_glyph = "## **CAMERA DEPUTATILOR**\n"
    assert any(
        c.reason == "shared_boilerplate.chamber_heading"
        for c in claim_shared_boilerplate(h1)
    )
    assert any(
        c.reason == "shared_boilerplate.chamber_heading"
        for c in claim_shared_boilerplate(h2)
    )
    assert any(
        c.reason == "shared_boilerplate.chamber_heading"
        for c in claim_shared_boilerplate(h2_old_glyph)
    )


def test_claim_session_label():
    body = "SESIUNEA A II-A ORDINARĂ – SEPTEMBRIE–DECEMBRIE 2025\n"
    claims = claim_shared_boilerplate(body)
    assert any(c.reason == "shared_boilerplate.session_label" for c in claims)


def test_claim_legislature_paren():
    body = "(Legislatura a X-a)\n"
    claims = claim_shared_boilerplate(body)
    assert any(c.reason == "shared_boilerplate.legislature_paren" for c in claims)


def test_claim_does_not_match_arbitrary_text():
    body = "An ordinary paragraph that happens to mention SESIUNEA\nbut isn't a header line.\n"
    claims = claim_shared_boilerplate(body)
    # The session_label pattern is `^SESIUNEA[^\n]+$` line-anchored; a
    # paragraph beginning mid-line shouldn't false-match.
    reasons = [c.reason for c in claims]
    assert "shared_boilerplate.session_label" not in reasons


def test_claim_kind_is_boilerplate():
    body = "Anul 194 (XXXVII) — Nr. 29\n"
    for c in claim_shared_boilerplate(body):
        assert c.kind == "boilerplate"
        assert c.reason is not None
