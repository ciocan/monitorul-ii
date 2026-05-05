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


# v0.2.0: joint Camera+Senat permanent committees → `special_joint` --------


def test_classify_kind_special_joint_permanent_comuna():
    """`Comisia permanentă comună a Camerei Deputaților și Senatului ...`
    classifies as `special_joint` (joint variant of permanent — UNESCO,
    securitate națională, etc.)."""
    name = (
        "Comisia permanentă comună a Camerei Deputaților și Senatului pentru "
        "relația cu UNESCO"
    )
    assert cs._classify_kind(name) == "special_joint"


def test_classify_kind_special_joint_via_camera_senat_co_anchor():
    """`Comisia permanentă a Camerei Deputaților și Senatului privind
    Statutul deputaților ...` carries the joint anchor without the
    `comună` modifier; should still classify as `special_joint`."""
    name = (
        "Comisia permanentă a Camerei Deputaților și Senatului privind "
        "Statutul deputaților și al senatorilor"
    )
    assert cs._classify_kind(name) == "special_joint"


def test_classify_kind_special_joint_for_specially_marked_joint():
    """`Comisia specială comună ...` collapses to `special_joint`
    (special + joint markers both fire)."""
    name = (
        "Comisia specială comună a Camerei Deputaților și Senatului pentru "
        "combaterea traficului de persoane"
    )
    assert cs._classify_kind(name) == "special_joint"


def test_classify_kind_inquiry_joint_for_joint_inquiry():
    """`Comisia comună de anchetă a Camerei Deputaților și Senatului ...`
    fires `inquiry` + `joint` → `inquiry_joint`."""
    name = "Comisia comună de anchetă a Camerei Deputaților și Senatului"
    assert cs._classify_kind(name) == "inquiry_joint"


def test_classify_kind_keeps_plain_permanent_for_single_chamber():
    """`Comisia pentru muncă și protecție socială` carries no joint
    anchor and stays `permanent`."""
    assert cs._classify_kind("Comisia pentru muncă și protecție socială") == "permanent"


def test_classify_kind_keeps_special_for_single_chamber_special():
    """Single-chamber `Comisia specială ...` (no joint markers) stays
    `special`, not `special_joint`."""
    name = "Comisia specială a Camerei Deputaților pentru automatizare"
    # `Camerei Deputaților` alone (without `și Senatului`) is not the
    # joint anchor — single-chamber special commission.
    assert cs._classify_kind(name) == "special"


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


# -- roster format detection (v0.2.0) --------------------------------------


def test_detect_roster_format_tabular():
    block = (
        "## 1. **Comisia X**\n\n"
        "|Numele și prenumele||Prezența fizică|\n"
        "|---|---|---|\n"
        "|Popescu Ion|||Prezent fizic|\n"
        "|Ionescu Mara|||Prezent online|\n"
    )
    assert cs._detect_roster_format(block) == "tabular"


def test_detect_roster_format_per_day():
    block = (
        "## 1. **Comisia Y**\n\n"
        "- La lucrările comisiei din data de 28 ianuarie 2008\n"
        "- au fost prezenți:\n"
        "1. Mircea Ciopraga, Grupul parlamentar al P.N.L., vicepreședinte.\n"
        "2. Aurelia Vasile, Grupul parlamentar al P.S.D., secretar.\n"
    )
    assert cs._detect_roster_format(block) == "per_day"


def test_detect_roster_format_narrative():
    block = (
        "Comisia ... și-a desfășurat lucrările.\n\n"
        "La lucrări au fost prezenți următorii deputați: Ana Popescu, Ion Ionescu și "
        "Maria Stoica.\n"
    )
    assert cs._detect_roster_format(block) == "narrative"


def test_detect_roster_format_returns_none_when_no_signals():
    block = "Just some prose that has no roster information."
    assert cs._detect_roster_format(block) is None


# -- tabular roster parser -------------------------------------------------


def test_parse_roster_tabular_simple_two_column_table():
    block = (
        "|Numele și prenumele|Prezența fizică|\n"
        "|---|---|\n"
        "|Popescu Ion|Prezent fizic|\n"
        "|Ionescu Mara|Prezent online|\n"
        "|Stoica Vasile|Absent|\n"
    )
    roster = cs._parse_roster_tabular(block)
    assert len(roster) == 3
    by_name = {r["speaker"]["name"]: r for r in roster}
    assert by_name["Popescu Ion"]["mode"] == "physical"
    assert by_name["Ionescu Mara"]["mode"] == "online"
    assert by_name["Stoica Vasile"]["mode"] == "absent"


def test_parse_roster_tabular_two_pairs_per_row():
    """The 2025 corpus shape: `|NAME1|||STATUS1|NAME2||STATUS2|` — two
    parallel name/status pairs per row, separated by empty cells."""
    block = (
        "|Numele și prenumele|||Prezența|Numele și prenumele||Prezența|\n"
        "|---|---|---|---|---|---|---|\n"
        "|Popescu Ion|||Prezent fizic|Stoica Vasile||Prezent online|\n"
    )
    roster = cs._parse_roster_tabular(block)
    names = {r["speaker"]["name"] for r in roster}
    assert "Popescu Ion" in names
    assert "Stoica Vasile" in names
    by_name = {r["speaker"]["name"]: r["mode"] for r in roster}
    assert by_name["Popescu Ion"] == "physical"
    assert by_name["Stoica Vasile"] == "online"


def test_parse_roster_tabular_filters_party_group_labels():
    """Single-token cells like `Neafiliată` / `UDMR` must NOT pair with
    a status cell as if they were a person name."""
    block = (
        "|Numele și prenumele|Grupul|Prezența|\n"
        "|---|---|---|\n"
        "|Popescu Ion|UDMR|Prezent fizic|\n"
        "|Stoica Vasile|Neafiliată|Absent|\n"
    )
    roster = cs._parse_roster_tabular(block)
    names = {r["speaker"]["name"] for r in roster}
    assert "Popescu Ion" in names
    assert "Stoica Vasile" in names
    assert "UDMR" not in names
    assert "Neafiliată" not in names


def test_parse_roster_tabular_empty_input_returns_empty():
    assert cs._parse_roster_tabular("no table here") == []


# -- narrative roster parser ----------------------------------------------


def test_parse_roster_narrative_basic_present_list():
    block = (
        "La lucrările comisiei au fost prezenți următorii deputați: Ana Popescu, "
        "Ion Ionescu și Maria Stoica.\n"
    )
    roster = cs._parse_roster_narrative(block)
    names = [r["speaker"]["name"] for r in roster]
    assert names == ["Ana Popescu", "Ion Ionescu", "Maria Stoica"]
    assert all(r["mode"] == "physical" for r in roster)


def test_parse_roster_narrative_with_online_subset():
    """`Domnii deputați A, B au fost prezenți on-line` flips A and B from
    the default `physical` to `online`."""
    block = (
        "La lucrări au fost prezenți: Ana Popescu, Ion Ionescu, Maria Stoica și "
        "Vlad Munteanu.\n"
        "Domnii deputați Ion Ionescu și Vlad Munteanu au fost prezenți on-line.\n"
    )
    roster = cs._parse_roster_narrative(block)
    by_name = {r["speaker"]["name"]: r["mode"] for r in roster}
    assert by_name["Ana Popescu"] == "physical"
    assert by_name["Ion Ionescu"] == "online"
    assert by_name["Maria Stoica"] == "physical"
    assert by_name["Vlad Munteanu"] == "online"


def test_parse_roster_narrative_with_absent_clause():
    block = (
        "La lucrări au fost prezenți: Ana Popescu și Ion Ionescu.\n"
        "Au absentat motivat: Maria Stoica și Vlad Munteanu.\n"
    )
    roster = cs._parse_roster_narrative(block)
    by_name = {r["speaker"]["name"]: r["mode"] for r in roster}
    assert by_name["Ana Popescu"] == "physical"
    assert by_name["Maria Stoica"] == "absent"
    assert by_name["Vlad Munteanu"] == "absent"


def test_parse_roster_narrative_captures_role_suffix():
    """`Natalia Intotero – președinte` → role=`președinte` and the role
    suffix is stripped from the canonical name."""
    block = (
        "La lucrări au fost prezenți: Natalia Intotero – președinte, Aurel Nechita "
        "– vicepreședinte, Cristian Țepeluș – secretar.\n"
    )
    roster = cs._parse_roster_narrative(block)
    by_name = {r["speaker"]["name"]: r for r in roster}
    assert by_name["Natalia Intotero"]["intra_committee_role"] == "președinte"
    assert by_name["Aurel Nechita"]["intra_committee_role"] == "vicepreședinte"
    assert by_name["Cristian Țepeluș"]["intra_committee_role"] == "secretar"


def test_parse_roster_narrative_substitution_marks_subject_substituted():
    """`Cătălina Ciofu – înlocuită de domnul deputat Jaro Norbert Marșalic`
    flips `Cătălina Ciofu` to mode=`substituted` and attaches the
    substitute Speaker."""
    block = (
        "La lucrări au fost prezenți: Cătălina Ciofu – înlocuită de domnul deputat "
        "Jaro Norbert Marșalic, Brian Cristian și Romulus-Marius Damian.\n"
    )
    roster = cs._parse_roster_narrative(block)
    by_name = {r["speaker"]["name"]: r for r in roster}
    assert by_name["Cătălina Ciofu"]["mode"] == "substituted"
    assert (
        by_name["Cătălina Ciofu"]["substituted_by"]["name"] == "Jaro Norbert Marșalic"
    )


# -- per-day numbered roster parser ---------------------------------------


def test_parse_roster_per_day_2008_form_with_role_after_group():
    """2008-era: `N. NAME, Grupul parlamentar al X, ROLE.`"""
    block = (
        "1. Mircea Ciopraga, Grupul parlamentar al P.N.L., vicepreședinte.\n"
        "2. Aurelia Vasile, Grupul parlamentar al P.S.D., secretar.\n"
        "3. Ioan Bivolaru, Grupul parlamentar al P.S.D.\n"
    )
    roster = cs._parse_roster_per_day(block)
    assert len(roster) == 3
    by_name = {r["speaker"]["name"]: r for r in roster}
    assert by_name["Mircea Ciopraga"]["intra_committee_role"] == "vicepreședinte"
    assert by_name["Aurelia Vasile"]["intra_committee_role"] == "secretar"
    assert by_name["Ioan Bivolaru"]["intra_committee_role"] is None
    # Party group preserved in Speaker
    assert by_name["Mircea Ciopraga"]["speaker"]["party_group"] is not None


def test_parse_roster_per_day_2021_form_with_role_before_group():
    """2021-era: `N. NAME – ROLE, Grupul parlamentar al X – prezent[ă].`"""
    block = (
        "1. Bende Sándor – președinte, Grupul parlamentar al UDMR – prezent.\n"
        "2. Ioan Mang – vicepreședinte, Grupul parlamentar al PSD – prezent.\n"
    )
    roster = cs._parse_roster_per_day(block)
    by_name = {r["speaker"]["name"]: r for r in roster}
    assert by_name["Bende Sándor"]["intra_committee_role"] == "președinte"
    assert by_name["Ioan Mang"]["intra_committee_role"] == "vicepreședinte"


def test_parse_roster_per_day_skips_high_ordinals():
    """Schema caps ordinals at 200 — drop anything above that."""
    block = (
        "1. Ana Popescu, Grupul parlamentar al PNL.\n"
        "201. Spurious Match, Grupul parlamentar al PSD.\n"
    )
    roster = cs._parse_roster_per_day(block)
    names = {r["speaker"]["name"] for r in roster}
    assert "Ana Popescu" in names
    assert "Spurious Match" not in names


# -- joint_with parser ----------------------------------------------------


def test_parse_joint_with_single_committee():
    block = "Comisia A, în comun cu Comisia pentru industrii și servicii din Camera Deputaților, a desfășurat ședința."
    out = cs._parse_joint_with(block)
    assert len(out) == 1
    assert out[0]["name"].lower().startswith("comisia pentru industrii")
    assert out[0]["chamber"] == "camera"


def test_parse_joint_with_multiple_committees():
    """`în comun cu Comisia X, Comisia Y și Comisia Z din Senat` → three
    items, all chamber=senat."""
    block = (
        "Comisia X, în comun cu Comisia juridică, Comisia pentru sănătate "
        "și Comisia pentru afaceri europene din Senat, a desfășurat ședința."
    )
    out = cs._parse_joint_with(block)
    names = [c["name"] for c in out]
    assert any("juridic" in n.lower() for n in names)
    assert any("sănătate" in n.lower() for n in names)
    assert any("afaceri europene" in n.lower() for n in names)
    assert len(out) >= 3
    assert all(c["chamber"] == "senat" for c in out)


def test_parse_joint_with_disambiguates_subject_list():
    """`Comisia pentru muncă, sănătate și educație` is ONE committee with a
    3-item subject list — only chunks that literally start with `Comisia`
    count as items. So `în comun cu Comisia pentru X, Y și Z` collapses
    to a single committee."""
    block = (
        "În comun cu Comisia pentru muncă, sănătate și educație, comisia a deliberat."
    )
    out = cs._parse_joint_with(block)
    # Either we capture exactly the first chunk (`Comisia pentru muncă`)
    # OR we fail to capture trailing comma-chunks because they don't start
    # with `Comisia`. Either way, only ONE committee is emitted.
    assert len(out) == 1


def test_parse_joint_with_no_marker_returns_empty():
    block = "Comisia X a desfășurat ședința separat. Niciun comun cu altă comisie."
    out = cs._parse_joint_with(block)
    assert out == []


def test_parse_joint_with_no_chamber_when_unspecified():
    """`în comun cu Comisia X` (no chamber tail) → chamber=None."""
    block = "Comisia A, în comun cu Comisia juridică, a deliberat."
    out = cs._parse_joint_with(block)
    assert len(out) == 1
    assert out[0]["chamber"] is None


# -- tabular agenda parser ------------------------------------------------


def test_build_tabular_agenda_basic_3_row_table(monkeypatch):
    """Three rows with PL-x cite + Scopul + Rezoluție columns. Each row
    becomes one CommitteeAgendaItem with primary_references + outcome
    populated."""

    class _MockCtx:
        def make_source_span(self, span):
            return {"chars": list(span), "lines": [1, 1], "content_sha": "00" * 6}

    block = (
        "|Nr.|PL-x|Titlu|Scopul|Rezoluție|\n"
        "|---|---|---|---|---|\n"
        "|1.|PL-x 188/2022|Proiect de lege pentru aprobarea OUG nr. 20/2022|Raport|În urma examinării, deputații au hotărât adoptarea proiectului.|\n"
        "|2.|PL-x 434/2022|Proiect de lege pentru modificarea art. 3 din Legea 15/1994|Raport|În urma examinării, deputații au hotărât respingerea proiectului.|\n"
        "|3.|PL-x 263/2024|Proiect de lege privind Codul fiscal|Raport|În urma examinării, deputații au hotărât adoptarea cu unanimitate de voturi.|\n"
    )
    items = cs._build_tabular_agenda(block, 0, _MockCtx())
    assert len(items) == 3
    assert items[0]["ordinal"] == 1
    assert items[2]["ordinal"] == 3
    # Each row has at least one bill primary_reference
    for it in items:
        bill_refs = [r for r in it["primary_references"] if r["type"] == "bill"]
        assert bill_refs, f"item {it['ordinal']} missing bill ref"
    # Outcome text recovered for each row
    assert items[0]["outcome_text"] is not None
    assert "În urma" in items[0]["outcome_text"]


def test_build_tabular_agenda_mixed_rows_some_without_plx():
    """Rows without a PL-x cite still emit an item — title-only fallback."""

    class _MockCtx:
        def make_source_span(self, span):
            return {"chars": list(span), "lines": [1, 1], "content_sha": "00" * 6}

    block = (
        "|Nr.|PL-x|Titlu|Rezoluție|\n"
        "|---|---|---|---|\n"
        "|1.|PL-x 100/2024|Proiect de lege A|Raport|\n"
        "|2.|Diverse|||\n"
    )
    items = cs._build_tabular_agenda(block, 0, _MockCtx())
    # First row populates fully; second row has no PL-x but ordinal+title get
    # captured.
    assert items
    assert items[0]["ordinal"] == 1
    bill_refs = [r for r in items[0]["primary_references"] if r["type"] == "bill"]
    assert bill_refs


def test_build_tabular_agenda_returns_empty_when_no_table():
    class _MockCtx:
        def make_source_span(self, span):
            return {"chars": list(span), "lines": [1, 1], "content_sha": "00" * 6}

    block = "Just narrative text with no agenda table."
    assert cs._build_tabular_agenda(block, 0, _MockCtx()) == []
