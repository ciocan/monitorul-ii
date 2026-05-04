"""Unit tests for the report_facsimile extractor's parser branches.

Each new regex / parser branch gets one positive sample, one negative
sample, and one quirk sample. End-to-end fixture tests live in
`test_report_facsimile_fixtures.py`.
"""

from __future__ import annotations

import re

from monitorul_ii.extraction.extractors import report_facsimile as rf


# -- title detection ------------------------------------------------------


def test_extract_title_from_sumar_csat_form():
    body = (
        "(RAPOARTE DE ACTIVITATE)\n\n"
        "SUMAR\n\n"
        "Pagina\n\n"
        "Raportul Consiliului Suprem de Apărare a Țării privind "
        "activitatea desfășurată în anul 2010 ........................ 2–41\n"
    )
    assert rf._extract_title(body) == (
        "Raportul Consiliului Suprem de Apărare a Țării privind "
        "activitatea desfășurată în anul 2010"
    )


def test_extract_title_from_sumar_sri_form():
    body = "Pagina Raport privind activitatea desfășurată de Serviciul Român de Informații în anul 2007 ..... 2–32\n"
    assert "Serviciul Român de Informații" in rf._extract_title(body)


def test_extract_title_from_sumar_srtv_form():
    body = (
        "|Raportul de activitate al Societății Române de Televiziune "
        "pe anul 2013...........|Pagina|\n"
    )
    assert "Societății Române de Televiziune" in rf._extract_title(body)


def test_extract_title_from_body_heading_when_sumar_absent():
    body = (
        "## **RAPORT privind activitatea desfășurată de Serviciul Român "
        "de Informații în anul 2007**\n"
    )
    assert "Serviciul Român de Informații" in rf._extract_title(body)


def test_extract_title_returns_none_on_unrelated_prose():
    body = "Just some prose without any Raport opener.\n"
    assert rf._extract_title(body) is None


# -- issuing body --------------------------------------------------------


def test_extract_issuing_body_csat_form():
    title = (
        "Raportul Consiliului Suprem de Apărare a Țării privind "
        "activitatea desfășurată în anul 2010"
    )
    assert rf._extract_issuing_body(title) == ("Consiliului Suprem de Apărare a Țării")


def test_extract_issuing_body_sri_form():
    title = (
        "Raport privind activitatea desfășurată de Serviciul Român de "
        "Informații în anul 2007"
    )
    assert rf._extract_issuing_body(title) == "Serviciul Român de Informații"


def test_extract_issuing_body_consiliul_legislativ_form():
    title = "Raport asupra activității desfășurate de Consiliul Legislativ în anul 2010"
    assert rf._extract_issuing_body(title) == "Consiliul Legislativ"


def test_extract_issuing_body_srtv_form():
    title = "Raportul de activitate al Societății Române de Televiziune pe anul 2013"
    assert rf._extract_issuing_body(title) == "Societății Române de Televiziune"


def test_extract_issuing_body_handles_anre_long_form():
    """ANRE = Autoritatea Națională de Reglementare în Domeniul Energiei.
    Long noun-phrase shouldn't trip the trailing-`în anul` boundary."""
    title = (
        "Raportul de activitate al Autorității Naționale de Reglementare "
        "în Domeniul Energiei pe anul 2013"
    )
    body = rf._extract_issuing_body(title)
    assert body is not None
    assert "Autorității Naționale de Reglementare" in body
    assert "Domeniul Energiei" in body


def test_extract_issuing_body_returns_none_when_title_is_none():
    assert rf._extract_issuing_body(None) is None


def test_extract_issuing_body_returns_none_on_unrecognized_form():
    assert rf._extract_issuing_body("Random title that doesn't match any form") is None


# -- reporting period -----------------------------------------------------


def test_extract_reporting_period_in_anul_form():
    title = "Raportul X privind activitatea desfășurată în anul 2010"
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2010-01-01", "end": "2010-12-31"}


def test_extract_reporting_period_pe_anul_form():
    title = "Raportul de activitate al X pe anul 2013"
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2013-01-01", "end": "2013-12-31"}


def test_extract_reporting_period_multi_year_form():
    title = "Raport privind activitatea desfășurată de X în perioada 2018-2020"
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2018-01-01", "end": "2020-12-31"}


def test_extract_reporting_period_falls_back_to_body():
    """When title has no year, the body's first year occurrence wins."""
    body = "Raportul X în anul 2014 ..."
    p = rf._extract_reporting_period(None, body)
    assert p == {"start": "2014-01-01", "end": "2014-12-31"}


def test_extract_reporting_period_returns_nulls_when_absent():
    p = rf._extract_reporting_period(None, "no year here")
    assert p == {"start": None, "end": None}


# -- received_at ----------------------------------------------------------


def test_extract_received_at_joint_with_body_date():
    body = (
        "**ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI** SESIUNEA …\n\n"
        "## **Ședința din ziua de 4 decembrie 2013**\n\n"
        "(RAPOARTE DE ACTIVITATE)\n"
    )
    r = rf._extract_received_at(body, {})
    assert r["session_kind"] == "joint"
    assert r["session_date"] == "2013-12-04"
    assert r["received_in_document"] is None


def test_extract_received_at_falls_back_to_frontmatter():
    """Body has no Ședința line — fall back to frontmatter session_date.
    Joint header detection needs the full `ALE CAMEREI ... ȘI SENATULUI`
    phrase; absent both anchors, frontmatter chamber drives session_kind."""
    body = "**ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI**"
    r = rf._extract_received_at(body, {"session_date": "2024-04-15"})
    assert r["session_kind"] == "joint"
    assert r["session_date"] == "2024-04-15"


def test_extract_received_at_camera_chamber():
    """No joint header but Camera frontmatter — session_kind=camera."""
    body = "no joint header here"
    r = rf._extract_received_at(body, {"chamber": "Camera Deputaților"})
    assert r["session_kind"] == "camera"


def test_extract_received_at_session_kind_null_when_neither_anchor():
    body = "no joint header"
    r = rf._extract_received_at(body, {})
    assert r["session_kind"] is None
    assert r["session_date"] is None


def test_extract_received_at_handles_cedilla_sedinta():
    """Older docs render `Ședința` with cedilla `Şedinţa`."""
    body = "## **Şedinţa din ziua de 11 aprilie 2016**"
    r = rf._extract_received_at(body, {})
    assert r["session_date"] == "2016-04-11"


# -- headings outline ----------------------------------------------------


def test_extract_headings_picks_up_h1_h2_with_bold():
    from monitorul_ii.extraction.coverage import line_offsets

    body = "## **CAPITOLUL I**\n\n## **OBIECTIVELE PRIORITARE**\n\n# **OVERVIEW**\n"
    h = rf._extract_headings(body, line_offsets(body))
    texts = [x["text"] for x in h]
    assert "CAPITOLUL I" in texts
    assert "OBIECTIVELE PRIORITARE" in texts
    assert "OVERVIEW" in texts


def test_extract_headings_skips_banner_and_footer():
    body = (
        "## **DEZBATERI PARLAMENTARE**\n\n"
        "## **Ședința din ziua de 4 decembrie 2013**\n\n"
        "## **CAPITOLUL I**\n\n"
        "## **EDITOR: GUVERNUL ROMÂNIEI**\n"
    )
    from monitorul_ii.extraction.coverage import line_offsets

    h = rf._extract_headings(body, line_offsets(body))
    texts = [x["text"] for x in h]
    assert texts == ["CAPITOLUL I"]


def test_extract_headings_skips_sumar_and_nota():
    body = "## SUMAR\n\n## N O T Ă:\n\n## **Cuvânt înainte**\n"
    from monitorul_ii.extraction.coverage import line_offsets

    h = rf._extract_headings(body, line_offsets(body))
    assert [x["text"] for x in h] == ["Cuvânt înainte"]


# -- boilerplate claims --------------------------------------------------


def test_claim_rf_boilerplate_genre_marker():
    body = "(RAPOARTE DE ACTIVITATE)\n"
    from monitorul_ii.extraction.coverage import line_offsets

    claims = rf._claim_rf_boilerplate(body, line_offsets(body))
    reasons = {c.reason for c in claims}
    assert "report_facsimile.rapoarte_genre_marker" in reasons


def test_claim_rf_boilerplate_facsimil_note_inline_form():
    body = "N O T Ă: Raportul X este reprodus în facsimil.\n"
    from monitorul_ii.extraction.coverage import line_offsets

    claims = rf._claim_rf_boilerplate(body, line_offsets(body))
    assert any(c.reason == "report_facsimile.facsimil_note" for c in claims)


def test_claim_rf_boilerplate_page_running_header():
    body = "20 MONITORUL  OFICIAL  AL  ROMÂNIEI,  PARTEA a II-a,  Nr.  1/R/20.I.2014\n"
    from monitorul_ii.extraction.coverage import line_offsets

    claims = rf._claim_rf_boilerplate(body, line_offsets(body))
    assert any(c.reason == "report_facsimile.page_running_header" for c in claims)


def test_claim_rf_boilerplate_trailing_footer_guvernul_variant():
    """Older R-suffix docs use `EDITOR: GUVERNUL ROMÂNIEI` (not PARLAMENTUL)."""
    body = (
        "Some content\n\n"
        "## **EDITOR: GUVERNUL ROMÂNIEI**\n\n"
        "Footer text continues to EOF.\n"
    )
    from monitorul_ii.extraction.coverage import line_offsets

    claims = rf._claim_rf_boilerplate(body, line_offsets(body))
    footer = [c for c in claims if c.reason == "report_facsimile.trailing_footer"]
    assert len(footer) == 1
    fc = footer[0]
    assert "GUVERNUL ROMÂNIEI" in body[fc.chars[0] : fc.chars[1]]


def test_claim_rf_boilerplate_no_match_returns_empty():
    from monitorul_ii.extraction.coverage import line_offsets

    body = "Just prose with no rf boilerplate."
    assert rf._claim_rf_boilerplate(body, line_offsets(body)) == []


# -- report content span -------------------------------------------------


def test_report_content_span_genre_marker_to_footer():
    body = (
        "Header content\n\n"
        "(RAPOARTE DE ACTIVITATE)\n\n"
        "report content here\n\n"
        "**EDITOR: GUVERNUL ROMÂNIEI**\n"
        "footer\n"
    )
    span = rf._report_content_span(body)
    assert span is not None
    start, end = span
    # The regex anchors to `^\s*` so the span may include leading whitespace
    # — use lstrip to compare on content.
    assert body[start:].lstrip().startswith("(RAPOARTE DE ACTIVITATE)")
    assert "EDITOR" not in body[start:end]


def test_report_content_span_to_eof_when_no_footer():
    body = "(RAPOARTE DE ACTIVITATE)\nreport text\n"
    span = rf._report_content_span(body)
    assert span is not None
    assert span[1] == len(body)


def test_report_content_span_returns_none_without_marker():
    body = "no genre marker present"
    assert rf._report_content_span(body) is None


# -- versioning -----------------------------------------------------------


def test_extractor_version_format():
    parts = rf.EXTRACTOR_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_extractor_label_format():
    assert re.match(r"^regex@report_facsimile@\d+\.\d+\.\d+$", rf.EXTRACTOR_LABEL)
