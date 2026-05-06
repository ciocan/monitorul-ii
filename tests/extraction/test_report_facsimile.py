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


def test_extract_title_pentru_anul_form():
    """v0.2.0: ANCOM/ASF/SRTv SUMAR rows end in `pentru anul YYYY`."""
    body = (
        "Pagina Raportul de activitate al Autorității Naționale pentru "
        "Administrare și Reglementare în Comunicații pentru anul 2014 ..... 2–94\n"
    )
    title = rf._extract_title(body)
    assert title == (
        "Raportul de activitate al Autorității Naționale pentru "
        "Administrare și Reglementare în Comunicații pentru anul 2014"
    )


def test_extract_title_din_month_year_form():
    """v0.2.0: AEP election-day SUMAR rows end in `din DD month YYYY` /
    `din month YYYY`."""
    body = (
        "|Raportul Autorității Electorale Permanente privind referendumul "
        "național<br>din 29 iulie 2012 ..............|Pagina|\n"
    )
    title = rf._extract_title(body)
    assert title == (
        "Raportul Autorității Electorale Permanente privind referendumul "
        "național din 29 iulie 2012"
    )


def test_extract_title_din_month_year_form_no_day():
    """v0.2.0: month-only date form (`din iunie 2012`)."""
    body = (
        "Raportul Autorității Electorale Permanente asupra organizării și "
        "desfășurării alegerilor pentru autoritățile administrației publice "
        "locale din iunie 2012 ........... 2–204\n"
    )
    title = rf._extract_title(body)
    assert title is not None
    assert title.endswith("din iunie 2012")
    assert "Autorității Electorale Permanente" in title


def test_extract_title_strips_br_residue():
    """v0.2.0: `<br>` from MD-converted multi-line SUMAR cells gets stripped
    so the canonical title is reader-friendly and the issuing-body regex
    isn't broken by the HTML tag at the year-tail boundary."""
    body = (
        "|Raportul de activitate al Autorității Naționale de Reglementare în "
        "Domeniul Energiei<br>pe anul 2013...............|Pagina|\n"
    )
    title = rf._extract_title(body)
    assert title is not None
    assert "<br>" not in title


def test_extract_title_din_form_negative_no_year():
    """v0.2.0: `din MONTH` without a 4-digit year doesn't anchor the
    title-end. Avoids false positives on body prose like `din martie`."""
    body = "Raportul X din martie .....\n"
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


def test_extract_issuing_body_ancom_pentru_anul_form():
    """v0.2.0: pattern 4 with `pentru anul` extension covers ANCOM/ASF/SRTv."""
    title = (
        "Raportul de activitate al Autorității Naționale pentru "
        "Administrare și Reglementare în Comunicații pentru anul 2014"
    )
    body = rf._extract_issuing_body(title)
    assert body == (
        "Autorității Naționale pentru Administrare și Reglementare în Comunicații"
    )


def test_extract_issuing_body_asf_pentru_anul_form():
    title = "Raportul de activitate al Autorității de Supraveghere Financiară pentru anul 2013"
    assert rf._extract_issuing_body(title) == "Autorității de Supraveghere Financiară"


def test_extract_issuing_body_aep_privind_activitatea_form():
    """v0.2.0: AEP titles use `Raportul privind activitatea X în anul Y`
    *without* `desfășurată de` — pattern 2 (new) handles this."""
    title = (
        "Raportul privind activitatea Autorității Electorale Permanente în anul 2014"
    )
    assert rf._extract_issuing_body(title) == "Autorității Electorale Permanente"


def test_extract_issuing_body_aep_election_form():
    """v0.2.0: pattern 5 broadened from `Raportul X privind activitatea
    desfășurată` to `Raportul X (privind|asupra) <topic>`. Covers AEP
    election-day reports whose topic is `alegerile` / `referendumul`."""
    title = (
        "Raportul Autorității Electorale Permanente privind alegerile pentru "
        "Președintele României din anul 2014"
    )
    assert rf._extract_issuing_body(title) == "Autorității Electorale Permanente"


def test_extract_issuing_body_aep_referendumul_form():
    title = (
        "Raportul Autorității Electorale Permanente privind referendumul "
        "național din 29 iulie 2012"
    )
    assert rf._extract_issuing_body(title) == "Autorității Electorale Permanente"


def test_extract_issuing_body_aep_asupra_form():
    """v0.2.0: pattern 5 also handles `Raportul X asupra <topic>`."""
    title = (
        "Raportul Autorității Electorale Permanente asupra organizării și "
        "desfășurării alegerilor pentru autoritățile administrației publice "
        "locale din iunie 2012"
    )
    assert rf._extract_issuing_body(title) == "Autorității Electorale Permanente"


def test_extract_issuing_body_anre_long_form_with_br_residue():
    """v0.2.0: `<br>` between body and tail used to break pattern 4's
    `\\s+pe\\s+anul` lookahead — `_extract_issuing_body` now strips it."""
    title = (
        "Raportul de activitate al Autorității Naționale de Reglementare "
        "în Domeniul Energiei<br>pe anul 2013"
    )
    body = rf._extract_issuing_body(title)
    assert body == "Autorității Naționale de Reglementare în Domeniul Energiei"


def test_extract_issuing_body_anre_privind_determinarea_form():
    """v0.2.0: pattern 5 also covers ANRE topic-form titles (`Raportul X
    privind determinarea ...`)."""
    title = (
        "Raportul Autorității Naționale de Reglementare în Domeniul Energiei "
        "privind determinarea prețurilor și tarifelor reglementate pentru anul 2013"
    )
    body = rf._extract_issuing_body(title)
    assert body == "Autorității Naționale de Reglementare în Domeniul Energiei"


def test_extract_issuing_body_aep_privind_activitatea_does_not_collide_with_sri_pattern():
    """Pattern 2's negative lookahead (`(?!desfășurat)`) ensures it doesn't
    swallow titles that are properly handled by pattern 1 (SRI form)."""
    title = (
        "Raport privind activitatea desfășurată de Serviciul Român de "
        "Informații în anul 2007"
    )
    # Pattern 1 should still match — body is SRI, not the verb phrase.
    assert rf._extract_issuing_body(title) == "Serviciul Român de Informații"


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


def test_extract_reporting_period_pentru_anul_form():
    """v0.2.0: ANCOM/ASF/SRTv use `pentru anul YYYY`."""
    title = "Raportul de activitate al X pentru anul 2014"
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2014-01-01", "end": "2014-12-31"}


def test_extract_reporting_period_din_dd_month_year_fallback():
    """v0.2.1: AEP election-day form `din DD month YYYY` carries the
    reporting year inside the date. Title-scoped fallback after the
    annual-form regexes."""
    title = (
        "Raportul Autorității Electorale Permanente privind referendumul "
        "național din 29 iulie 2012"
    )
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2012-01-01", "end": "2012-12-31"}


def test_extract_reporting_period_din_month_year_fallback():
    """v0.2.1: month-only date form (`din iunie 2012`) — same election-day
    cohort, just no day component."""
    title = (
        "Raportul Autorității Electorale Permanente asupra organizării și "
        "desfășurării alegerilor pentru autoritățile administrației publice "
        "locale din iunie 2012"
    )
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2012-01-01", "end": "2012-12-31"}


def test_extract_reporting_period_annual_form_wins_over_date_fallback():
    """When both forms are present, the annual `(in|pe|pentru) anul YYYY`
    wins over the `din ... YYYY` fallback (annual is the canonical
    reporting period; date fallback exists only for AEP-style titles
    that lack the annual form)."""
    title = (
        "Raport de activitate al X pe anul 2013, pentru perioada din 1 ianuarie 2013"
    )
    p = rf._extract_reporting_period(title, "")
    assert p == {"start": "2013-01-01", "end": "2013-12-31"}


def test_extract_reporting_period_date_fallback_does_not_fire_on_body():
    """v0.2.1: the date fallback is title-scoped — body-internal dates
    must not trigger it (avoids false positives on unrelated body text)."""
    body = "Some body text with `din 5 mai 2020` mentioned in passing.\n"
    p = rf._extract_reporting_period(None, body)
    assert p == {"start": None, "end": None}


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
