"""Unit tests for the committee_synthesis extractor's parser branches.

Each new regex / parser branch gets one positive sample, one negative
sample, and one quirk sample where the extractor encodes a non-obvious
behavior (mojibake-tolerance, multi-bold date splits, etc.). End-to-end
fixture tests live in `test_committee_synthesis_fixtures.py`.
"""

from __future__ import annotations

import re

from monitorul_ii.extraction.extractors import committee_synthesis as cs


# -- period header parser --------------------------------------------------


def test_parse_period_full_explicit_months():
    body = "## **Perioada: 17.11–20.11.2003**"
    p = cs._parse_period(body)
    assert p == {"start": "2003-11-17", "end": "2003-11-20"}


def test_parse_period_single_month_abbreviated():
    body = "**Perioada: 23–25.11.2021 SUMAR**"
    p = cs._parse_period(body)
    assert p == {"start": "2021-11-23", "end": "2021-11-25"}


def test_parse_period_with_full_stop_spacing():
    body = "**Perioada: 22.03. – 25.03.2004**"
    p = cs._parse_period(body)
    assert p == {"start": "2004-03-22", "end": "2004-03-25"}


def test_parse_period_single_day():
    body = "**Perioada: 12.10.2024**"
    p = cs._parse_period(body)
    assert p == {"start": "2024-10-12", "end": "2024-10-12"}


def test_parse_period_returns_nulls_when_absent():
    body = "no Perioada line here"
    p = cs._parse_period(body)
    assert p == {"start": None, "end": None}


def test_parse_period_outermost_range_for_disjoint_windows():
    """Multi-window `2.08 – 5.08; 16.08 – 19.08.2004` collapses to the
    first range — small lossy compromise per design notes."""
    body = "**Perioada:  2.08 – 5.08; 16.08 – 19.08.2004**"
    p = cs._parse_period(body)
    assert p["start"] == "2004-08-02"
    assert p["end"] == "2004-08-05"


# -- committee header partitioner -----------------------------------------


def test_committee_header_modern_form():
    line = "## 1. **Comisia pentru buget, finanțe și bănci**"
    m = cs._COMMITTEE_HEADER_RE.search(line)
    assert m
    assert m.group("ord_outside") == "1"
    assert "buget" in m.group("name1")


def test_committee_header_number_inside_bold():
    line = "## **15. Comisia specială pentru elaborarea propunerilor legislative**"
    m = cs._COMMITTEE_HEADER_RE.search(line)
    assert m
    assert m.group("ord_inside") == "15"
    assert "specială" in m.group("name2")


def test_committee_header_no_number():
    line = "## **Comisia pentru cultură**"
    m = cs._COMMITTEE_HEADER_RE.search(line)
    assert m
    assert m.group("name_only").startswith("Comisia")


def test_committee_header_rejects_signature_line():
    """Signature lines like `## **Bogdan-Iulian Huțucă**` must NOT match —
    the inner content has to start with "Comisia"."""
    line = "## **Bogdan-Iulian Huțucă**"
    m = cs._COMMITTEE_HEADER_RE.search(line)
    assert m is None


def test_committee_header_rejects_partea_banner():
    """`## **PA R T E A A I I - A SINTEZA LUCRĂRILOR ...**` must NOT
    match — only Comisia-prefixed bolds are committees."""
    line = "## **PA R T E A  A  I I - A SINTEZA LUCRĂRILOR COMISIILOR CAMEREI DEPUTAȚILOR**"
    m = cs._COMMITTEE_HEADER_RE.search(line)
    # The trailing `**COMISIILOR CAMEREI DEPUTAȚILOR**` is one bold span
    # but the whole line has multiple bolds, so the line-anchored regex
    # doesn't match in any of the three forms.
    assert m is None


# -- date harvesting -------------------------------------------------------


def test_parse_dates_modern_multi_bold():
    """Modern form: `în zilele de **DD, DD, DD** și **DD month YYYY**`
    splits the day list into two bold spans. Strip-emphasis trick handles
    both.
    """
    block = (
        "Comisia pentru buget, finanțe și bănci și-a desfășurat lucrările "
        "în zilele de **12, 13, 14** și **15 ianuarie 2015** ."
    )
    dates = cs._parse_dates(block)
    assert dates == ["2015-01-12", "2015-01-13", "2015-01-14", "2015-01-15"]


def test_parse_dates_single_bold_with_day_range():
    block = "Comisia ... și-a desfășurat lucrările în perioada **2-5 iunie 2025** ."
    dates = cs._parse_dates(block)
    # `2-5 iunie 2025` — only the explicit day numbers (2 and 5) extracted
    assert "2025-06-02" in dates
    assert "2025-06-05" in dates


def test_parse_dates_single_day():
    block = "Comisia ... și-a desfășurat lucrările în ziua de **3 iunie 2025** ."
    dates = cs._parse_dates(block)
    assert dates == ["2025-06-03"]


def test_parse_dates_returns_empty_when_trigger_absent():
    block = "Random prose without the trigger phrase."
    assert cs._parse_dates(block) == []


def test_parse_dates_handles_cedilla_and_diacritic_variants():
    """Older docs use cedilla: `şi-a desfăşurat lucrările`."""
    block = (
        "Comisia X şi-a desfăşurat lucrările "
        "în zilele de **20** şi **21 martie 2008** ."
    )
    dates = cs._parse_dates(block)
    assert dates == ["2008-03-20", "2008-03-21"]


# -- time windows ----------------------------------------------------------


def test_parse_time_windows_modern_dot_separator():
    block = "în intervalele orare 8.30–12.00, respectiv 13.00–18.00"
    tw = cs._parse_time_windows(block)
    assert tw == [
        {"start": "08:30", "end": "12:00"},
        {"start": "13:00", "end": "18:00"},
    ]


def test_parse_time_windows_colon_separator():
    """Modern HH:MM (colon) form — converter sometimes outputs colon
    instead of the canonical dot."""
    block = "în intervalul orar 13:00–17:30"
    tw = cs._parse_time_windows(block)
    assert tw == [{"start": "13:00", "end": "17:30"}]


def test_parse_time_windows_filters_unrealistic_hours():
    """Bill numbers like `art. 99.99` shouldn't be picked up as times."""
    block = "art. 99.99–99.99 not a time"
    tw = cs._parse_time_windows(block)
    assert tw == []


# -- format detector ------------------------------------------------------


def test_parse_format_mixed():
    block = "cu prezență fizică și online"
    assert cs._parse_format(block) == "mixed"


def test_parse_format_online():
    block = "lucrările s-au desfășurat exclusiv online"
    assert cs._parse_format(block) == "online"


def test_parse_format_returns_none_when_no_marker():
    """No positive marker — leave format null. Caller decides on a date-
    based default if it wants one."""
    block = "Comisia s-a întrunit la sediul Camerei Deputaților."
    assert cs._parse_format(block) is None


# -- purpose detector -----------------------------------------------------


def test_parse_purpose_documentare():
    block = "PL-x 509/2021 – documentare și consultare."
    assert cs._parse_purpose(block) == "documentare_consultare"


def test_parse_purpose_audiere_candidati():
    block = "Audierea domnului X, candidat pentru ocuparea funcției de ministru"
    assert cs._parse_purpose(block) == "audiere_candidați"


def test_parse_purpose_returns_none_for_normal_dezbatere():
    block = "Proiect de lege privind aprobarea OUG nr. 99/2006"
    assert cs._parse_purpose(block) is None


# -- signature extraction --------------------------------------------------


def test_parse_signatures_modern_form():
    block = "PREȘEDINTE,\n\n## **Bogdan-Iulian Huțucă**"
    chair, secretary = cs._parse_signatures(block)
    assert chair is not None
    assert chair["name"] == "Bogdan-Iulian Huțucă"
    assert secretary is None


def test_parse_signatures_inline_form():
    block = "PREȘEDINTE, **Costel Neculai Dunava**\n\nSECRETAR, **Bogdan-Alin Stoica**"
    chair, secretary = cs._parse_signatures(block)
    assert chair["name"] == "Costel Neculai Dunava"
    assert secretary["name"] == "Bogdan-Alin Stoica"


def test_parse_signatures_handles_mojibake():
    """2008-era mojibake: `PRE�EDINTE,` with U+FFFD replacing the diacritic."""
    block = "PRE�EDINTE,\n\n**Cătălin Micula**"
    chair, _ = cs._parse_signatures(block)
    assert chair is not None
    assert chair["name"] == "Cătălin Micula"


def test_parse_signatures_handles_cedilla_form():
    """Older docs: `PREŞEDINTE,` with cedilla `Ş`."""
    block = "PREŞEDINTE,\n\n## **Mircea Ciopraga**"
    chair, _ = cs._parse_signatures(block)
    assert chair is not None
    assert chair["name"] == "Mircea Ciopraga"


def test_parse_signatures_secretar_in_heading_position():
    """`## SECRETAR, **Name**` — the H2-position oddity from PDF→MD."""
    block = "## SECRETAR, **Aurelia Vasile**"
    _, secretary = cs._parse_signatures(block)
    assert secretary is not None
    assert secretary["name"] == "Aurelia Vasile"


def test_parse_signatures_both_absent():
    chair, secretary = cs._parse_signatures("just some text without signatures")
    assert chair is None
    assert secretary is None


# -- committee kind classifier ---------------------------------------------


def test_classify_kind_permanent_default():
    assert cs._classify_kind("Comisia pentru buget, finanțe și bănci") == "permanent"


def test_classify_kind_special():
    name = "Comisia specială pentru elaborarea propunerilor legislative privind …"
    assert cs._classify_kind(name) == "special"


def test_classify_kind_inquiry():
    name = "Comisia de anchetă cu privire la acuzațiile de concurență neloială"
    assert cs._classify_kind(name) == "inquiry"


def test_classify_kind_handles_mojibake_special():
    """`Comisia specialã` (cedilla `ã`) and `Comisia special„` (mojibake)
    both classify as `special`."""
    assert cs._classify_kind("Comisia specialã pentru …") == "special"
    assert cs._classify_kind("Comisia special„ pentru …") == "special"


# -- committee name normalisation ------------------------------------------


def test_clean_committee_name_strips_trailing_page_dots():
    raw = "Comisia pentru sănătate și familie..............8–10"
    assert cs._clean_committee_name(raw) == "Comisia pentru sănătate și familie"


def test_clean_committee_name_keeps_parenthetical():
    raw = "Comisia juridică (Perioada: 20–23 august 2001)"
    assert (
        cs._clean_committee_name(raw)
        == "Comisia juridică (Perioada: 20–23 august 2001)"
    )


# -- agenda item splitter -------------------------------------------------


def test_split_agenda_picks_up_numbered_items():
    block = (
        "1. Proiect de lege A.\n\n"
        "2. Proiect de lege B.\n\n"
        "3. Comunicare COM(2025) 79.\n"
    )
    items = cs._split_agenda(block)
    assert [i[0] for i in items] == [1, 2, 3]
    assert items[0][2].startswith("Proiect de lege A")


def test_split_agenda_rejects_high_ordinals():
    """Article references like `art. 1.234` shouldn't be treated as agenda
    items. Schema caps ordinals at 200; we reject anything above that."""
    block = "art. 1.234 din Codul fiscal\n\n234. Some title not actually agenda\n"
    items = cs._split_agenda(block)
    # No ordinal sequence detected (1234 caps out, 234 also above 200)
    assert items == []


def test_split_agenda_handles_restart_sequences():
    """A committee block can have a restart from `1.` after the first
    sub-day's agenda; we accept that."""
    block = (
        "1. Item A.\n\n2. Item B.\n\n"
        "Continuarea lucrărilor:\n\n"
        "1. Item C.\n\n2. Item D.\n"
    )
    items = cs._split_agenda(block)
    assert len(items) == 4
    assert items[0][2].startswith("Item A")
    assert items[2][2].startswith("Item C")


# -- committee_role + output_type detectors --------------------------------


def test_detect_committee_role_fond_comun():
    title = "Proiect de lege … fond comun cu Comisia X"
    assert cs._detect_committee_role(title) == "fond_comun"


def test_detect_committee_role_aviz():
    title = "Proiect de lege … (PL-x 395/2017; aviz)"
    assert cs._detect_committee_role(title) == "aviz"


def test_detect_committee_role_aviz_pentru():
    """`aviz pentru Comisia X` is the same `aviz` role; the `pentru` clause
    just names the receiving committee."""
    title = "Proiect de lege … aviz pentru Comisia juridică"
    assert cs._detect_committee_role(title) == "aviz"


def test_detect_output_type_raport_preliminar_beats_raport():
    """Order matters — the longer specific match wins. `raport preliminar`
    must NOT collapse to `raport`."""
    title = "Proiect de lege; raport preliminar pentru Comisia juridică"
    assert cs._detect_output_type(title) == "raport_preliminar"


def test_detect_output_type_raport_comun_suplimentar_beats_raport_comun():
    title = "Proiect; raport comun suplimentar"
    assert cs._detect_output_type(title) == "raport_comun_suplimentar"


def test_detect_output_type_studiu():
    title = "Proiect de lege; studiu"
    assert cs._detect_output_type(title) == "studiu"


def test_detect_output_type_amanare():
    title = "Proiect de lege amânat pentru o săptămână"
    assert cs._detect_output_type(title) == "amânare"


# -- vote summary parser --------------------------------------------------


def test_parse_vote_summary_unanimous_approval():
    text = "În urma dezbaterilor, comisia a hotărât, cu unanimitate de voturi, adoptarea proiectului de lege."
    s = cs._parse_vote_summary(text)
    assert s is not None
    assert s["outcome"] == "approved"
    assert s["majority"] == "unanimous"


def test_parse_vote_summary_majority_with_counts():
    text = "Aprobat cu majoritate de voturi (6 voturi împotrivă și două abțineri)."
    s = cs._parse_vote_summary(text)
    assert s is not None
    assert s["outcome"] == "approved"
    assert s["majority"] == "majority"
    assert s["against"] == 6
    assert s["abstain"] == 2


def test_parse_vote_summary_rejected():
    text = "Respins cu majoritate de voturi."
    s = cs._parse_vote_summary(text)
    assert s is not None
    assert s["outcome"] == "rejected"


def test_parse_vote_summary_deferred():
    text = "Proiectul de lege a fost amânat pentru o săptămână."
    s = cs._parse_vote_summary(text)
    assert s is not None
    assert s["outcome"] == "deferred"


def test_parse_vote_summary_returns_none_when_no_outcome():
    text = "La dezbateri au luat cuvântul deputații X, Y și Z."
    assert cs._parse_vote_summary(text) is None


# -- boilerplate claims ----------------------------------------------------


def test_claim_committee_boilerplate_partea_sinteza_banner():
    body = "## **PA R T E A  A  I I - A SINTEZA LUCRĂRILOR COMISIILOR CAMEREI DEPUTAȚILOR**\n"
    claims = cs._claim_committee_boilerplate(body, cs._line_offsets(body))
    reasons = {c.reason for c in claims}
    assert "committee_synthesis.partea_sinteza_banner" in reasons


def test_claim_committee_boilerplate_trailing_footer():
    body = (
        "Some content.\n\n"
        "**EDITOR: PARLAMENTUL ROMÂNIEI  — CAMERA DEPUTAȚILOR**\n"
        "footer paragraph 1\n"
        "footer paragraph 2\n"
    )
    claims = cs._claim_committee_boilerplate(body, cs._line_offsets(body))
    footer_claims = [
        c for c in claims if c.reason == "committee_synthesis.trailing_footer"
    ]
    assert len(footer_claims) == 1
    # Footer claim spans from the EDITOR line to EOF
    fc = footer_claims[0]
    assert body[fc.chars[0] : fc.chars[1]].startswith("**EDITOR")


def test_claim_committee_boilerplate_no_match_returns_empty():
    body = "Just some prose with no boilerplate."
    claims = cs._claim_committee_boilerplate(body, cs._line_offsets(body))
    assert claims == []


# -- versioning -----------------------------------------------------------


def test_extractor_version_format():
    parts = cs.EXTRACTOR_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_extractor_label_format():
    assert re.match(r"^regex@committee_synthesis@\d+\.\d+\.\d+$", cs.EXTRACTOR_LABEL)
