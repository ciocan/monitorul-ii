"""Unit tests for the plenary agenda extractor (categories + sub-fields +
SUMAR parser + outcome detection)."""

from __future__ import annotations

from datetime import date

from monitorul_ii.extraction.envelope import EnvelopeMeta
from monitorul_ii.extraction.extractors.plenary.agenda import (
    _BODY_AGENDA_ITEM_RE,
    _clean_sumar_title,
    _clip_contamination_tail,
    detect_category,
    detect_confidence_type,
    detect_outcome_from_body,
    detect_reexamination_reason,
    detect_requested_by_group,
    extract_agenda,
    parse_sumar,
)
from monitorul_ii.extraction.extractors.plenary.session import find_sumar_span
from monitorul_ii.extraction.pipeline import ExtractContext


def _ctx(body: str, year: int = 2025) -> ExtractContext:
    from monitorul_ii.extraction.coverage import line_offsets

    meta = EnvelopeMeta(issue="1", year=year, part="II", published=date(year, 1, 1))
    return ExtractContext(
        body_text=body,
        line_offsets=line_offsets(body),
        content_sha="0123456789ab",
        meta=meta,
        frontmatter={
            "issue": "1",
            "year": year,
            "part": "II",
            "published": meta.published,
        },
    )


# -- detect_category --------------------------------------------------------


def test_category_oath_taking():
    cat, conf, _ = detect_category("Depunerea jurământului de către domnul deputat X")
    assert cat == "oath_taking"
    assert conf >= 0.9


def test_category_government_hour():
    cat, _, _ = detect_category("Ora Guvernului – Dezbateri politice")
    assert cat == "government_hour"


def test_category_government_confidence_cenzura():
    cat, _, _ = detect_category(
        "Prezentarea, dezbaterea și respingerea moțiunii de cenzură"
    )
    assert cat == "government_confidence"


def test_category_bill_debate_generic_baseline():
    cat, conf, _ = detect_category(
        "Dezbaterea Proiectului de lege privind salarizarea unitară"
    )
    assert cat == "bill_debate"
    # Generic baseline → lower confidence than specific patterns
    assert conf < 0.95


def test_category_committee_membership_beats_bill_debate():
    """Specific category (committee_membership, weight 1.0) wins over generic
    bill_debate (weight 0.5). Runners-up only include rules within 0.2 of
    the winner, so bill_debate is *not* listed as a runner — confirming the
    weight gap correctly demotes the generic baseline.
    """
    cat, _, _runners = detect_category(
        "Dezbaterea Proiectului de hotărâre privind constituirea Comisiei "
        "speciale comune a Camerei Deputaților și Senatului"
    )
    assert cat == "committee_membership"


def test_category_no_match_returns_other():
    cat, conf, _ = detect_category("random title without keywords")
    assert cat == "other"
    assert conf < 0.5


def test_category_final_vote_batch():
    cat, _, _ = detect_category("Supunerea la votul final a:")
    assert cat == "final_vote_batch"


def test_category_eu_consultation():
    cat, _, _ = detect_category(
        "Adoptarea opiniei referitoare la Comunicarea Comisiei Europene"
    )
    assert cat == "eu_consultation"


def test_category_questions_interpellations():
    cat, _, _ = detect_category(
        "Întrebări orale adresate Guvernului — Răspunsuri scrise"
    )
    assert cat == "questions_interpellations"


# -- sub-field detectors ---------------------------------------------------


def test_confidence_type_cenzura():
    assert detect_confidence_type("Prezentarea moțiunii de cenzură") == "cenzură"


def test_confidence_type_demitere():
    assert detect_confidence_type("Demitere a Guvernului") == "demitere"


def test_confidence_type_no_match():
    assert detect_confidence_type("regular plenary debate") is None


def test_requested_by_group_extracts_party():
    g = detect_requested_by_group(
        "Ora Guvernului la solicitarea Grupului parlamentar al PNL cu privire la X"
    )
    assert g == "PNL"


def test_requested_by_group_no_match():
    assert detect_requested_by_group("regular Ora Guvernului") is None


def test_reexamination_presidential():
    assert (
        detect_reexamination_reason("Reexaminare la solicitarea Președintelui României")
        == "presidential_request"
    )


def test_reexamination_constitutional_court():
    assert (
        detect_reexamination_reason(
            "Reexaminare ca urmare a Deciziei Curții Constituționale nr. 5/2020"
        )
        == "constitutional_court"
    )


def test_reexamination_default_to_parliamentary():
    assert (
        detect_reexamination_reason("Cerere de reexaminare a Legii X")
        == "parliamentary_majority"
    )


def test_reexamination_returns_none_when_not_present():
    assert detect_reexamination_reason("regular plenary debate") is None


# -- outcome from body ----------------------------------------------------


def test_outcome_adoptat():
    assert detect_outcome_from_body("a fost adoptat în ședință", "x") == "adoptat"


def test_outcome_respins():
    assert detect_outcome_from_body("a fost respins cu majoritate", "x") == "respins"


def test_outcome_votul_final_deferred():
    assert (
        detect_outcome_from_body("Aceasta rămâne pentru votul final.", "x")
        == "votul_final_deferred"
    )


def test_outcome_tacit_adoption():
    assert (
        detect_outcome_from_body("a fost adoptat tacit prin împlinirea termenului", "x")
        == "adoptat_tacit"
    )


def test_outcome_none_for_neutral_body():
    assert detect_outcome_from_body("plain text no outcome marker", "x") is None


# -- parse_sumar ----------------------------------------------------------


def test_parse_sumar_br_separated_table():
    """Modern SUMAR layout: items separated by `<br>` inside one table cell."""
    body = (
        "SUMAR\n"
        "|Nr.<br>1.<br>First item title<br>2.<br>Second item title<br>"
        "3.<br>Third item title|Pagina|\n"
        "|---|---|\n"
        "## **Domnul X:**\n"
    )
    span = find_sumar_span(body)
    entries = parse_sumar(body, span)
    assert len(entries) >= 3
    assert entries[0].ordinal == 1
    assert "First item" in entries[0].title


def test_parse_sumar_table_row_layout():
    """Multi-table SUMAR: each item in its own `|N.|...|page|` row."""
    body = (
        "SUMAR\n"
        "|14.|Title fourteen with extra content|18|\n"
        "|15.|Title fifteen here|18-19|\n"
        "## **Domnul X:**\n"
    )
    span = find_sumar_span(body)
    entries = parse_sumar(body, span)
    ords = {e.ordinal for e in entries}
    assert 14 in ords
    assert 15 in ords


def test_parse_sumar_returns_sorted_by_ordinal():
    body = (
        "SUMAR\n"
        "|14.|Title fourteen|18|\n"
        "|Nr.<br>1.<br>First<br>2.<br>Second|Pagina|\n"
        "## **Domnul X:**\n"
    )
    span = find_sumar_span(body)
    entries = parse_sumar(body, span)
    # Output sorted regardless of pass order
    ordinals = [e.ordinal for e in entries]
    assert ordinals == sorted(ordinals)


def test_parse_sumar_skips_invalid_ordinals():
    body = (
        "SUMAR\n"
        "|999.|Should skip — out of range|99|\n"
        "|1.|Valid item|1|\n"
        "## **Domnul X:**\n"
    )
    span = find_sumar_span(body)
    entries = parse_sumar(body, span)
    ords = {e.ordinal for e in entries}
    assert 1 in ords
    # 999 is out of the 1-200 valid range
    assert 999 not in ords


# -- _BODY_AGENDA_ITEM_RE relaxation ---------------------------------------
#
# Older docs (pre-2010) sometimes render agenda headers as `## N. Title`
# without the bold wrapping. Relaxed regex accepts both.


def test_body_agenda_item_re_bold_wrapped():
    body = "## **3. Adoptarea proiectului de lege**\n"
    matches = list(_BODY_AGENDA_ITEM_RE.finditer(body))
    assert len(matches) == 1
    assert matches[0].group("ord") == "3"


def test_body_agenda_item_re_unwrapped():
    """Older docs render `## 3. Title` (no `**` bold)."""
    body = "## 3. Adoptarea proiectului de lege\n"
    matches = list(_BODY_AGENDA_ITEM_RE.finditer(body))
    assert len(matches) == 1
    assert matches[0].group("ord") == "3"


def test_body_agenda_item_re_skips_inline_numbers():
    """Inline numbered list inside a speech (`vă rog: 1. care e...`) MUST
    NOT match — only line-anchored `## ` prefixed headers count."""
    body = "vă rog să-mi comunicați: 1. care este stadiul rezolvării\n"
    matches = list(_BODY_AGENDA_ITEM_RE.finditer(body))
    assert matches == []


# -- implicit single-item fallback -----------------------------------------


def test_extract_agenda_implicit_single_item_when_no_sumar_no_body_marks():
    """A short modern session with body speech turns but NO SUMAR and NO
    `## **N.**` agenda markers gets wrapped into one implicit item so its
    speeches are claimed."""
    body = (
        "## **Domnul Florin Iordache:**\n"
        "Bună ziua! Declar deschisă ședința consacrată declarațiilor "
        "politice de astăzi.\n\n"
        "## **Doamna Maria Test:**\n"
        "Mulțumesc, domnule președinte. Doresc să ridic o problemă...\n"
    )
    items, claims = extract_agenda(body, len(body), _ctx(body))
    assert len(items) == 1
    assert items[0]["ordinal"] == 1
    assert items[0]["category"] == "other"
    # Activities should include both speech turns
    assert len(items[0]["activities"]) >= 2


def test_extract_agenda_implicit_single_item_skipped_when_sumar_provides_entries():
    """Negative control: when SUMAR is parsed and produces entries, the
    SUMAR-driven path runs and the implicit fallback stays dormant."""
    body = (
        "SUMAR\n\n"
        "|Nr.<br>1.<br>First item title<br>2.<br>Second item title|Pagina|\n"
        "## **Domnul X:**\nText.\n"
    )
    items, _ = extract_agenda(body, len(body), _ctx(body))
    assert len(items) >= 2
    # Item 1 still keeps its real title from SUMAR (not "Ședința")
    assert items[0]["title"] != "Ședința"


def test_extract_agenda_implicit_fallback_skipped_for_empty_body():
    """If body has no speech turns post-SUMAR, the implicit fallback emits
    nothing rather than wrapping a blank span."""
    body = "header text only\nno speeches at all\n"
    items, claims = extract_agenda(body, len(body), _ctx(body))
    assert items == []
    assert claims == []


# -- _clip_contamination_tail (v0.2.5) --------------------------------------
#
# Clips a cleaned SUMAR `rest` at the first detected next-item boundary —
# guards against the SUMAR over-capture that produced ~40% linker FPs.


def test_clip_contamination_tier1_page_plus_next_ordinal():
    """Tier 1: page-list followed by next ordinal + capital. The most common
    contamination shape — `... 15 4. Aprobarea ...` clips at `4.`."""
    s = "Aprobarea ordinii de zi și a programului de lucru 15 4. Aprobarea unei modificări"
    out = _clip_contamination_tail(s)
    assert out == "Aprobarea ordinii de zi și a programului de lucru 15"


def test_clip_contamination_tier1_page_range():
    """Page-range form `30–31 3. Numirea ...` is also clipped (en-dash)."""
    s = "Aprobarea ordinii de zi 30–31 3. Numirea domnului Constantin"
    out = _clip_contamination_tail(s)
    assert out == "Aprobarea ordinii de zi 30–31"


def test_clip_contamination_tier1_multipage_list():
    """Multi-page list `4–5; 27; 38 3. Notă ...` clips at `3. N`."""
    s = "Declarații politice prezentate 4–5; 27; 38 3. Notă pentru exercitarea"
    out = _clip_contamination_tail(s)
    assert out.startswith("Declarații politice prezentate 4–5; 27; 38")
    assert "3. Notă" not in out


def test_clip_contamination_tier2_table_separator_artifact():
    """Tier 2: `--- ---` artifact (from `|---|---|` after `|` is stripped to
    space) signals the end of the SUMAR table cell."""
    s = "Item title here --- --- next-table-content with bills"
    out = _clip_contamination_tail(s)
    assert out == "Item title here"


def test_clip_contamination_tier2_table_separator_at_end_of_string():
    """Tier 2 must also clip when the artifact sits at the very end of the
    string (no trailing whitespace) — happens after `_clean_sumar_title` has
    already `.strip()`-ed the cleaned form. 2000-era SUMAR rows whose entire
    contaminated tail collapses to a `... 18 --- ---` ending fell through the
    pre-fix regex because it required `\\s+` after the last dash group."""
    s = "Aprobarea ordinii de zi 14-18 februarie 2000 Pagina 4-5 5 5 18 --- ---"
    out = _clip_contamination_tail(s)
    assert out == "Aprobarea ordinii de zi 14-18 februarie 2000 Pagina 4-5 5 5 18"


def test_clip_contamination_tier3_bullet_ordinal():
    """Tier 3: markdown bullet ordinal `\\s+- N. C` (some 2010-era SUMARs)."""
    s = "Approved declarations - 4. Domnul senator anunță demisia"
    out = _clip_contamination_tail(s)
    assert out == "Approved declarations"


def test_clip_contamination_clean_title_unchanged():
    """Negative: a clean short title with no boundary stays as-is."""
    s = "Aprobarea ordinii de zi și a programului de lucru"
    assert _clip_contamination_tail(s) == s


def test_clip_contamination_legitimate_long_title_unchanged():
    """Negative: a legitimate single-bill title with the bill cite at the
    end (no page+ord pattern, no artifact) stays untouched.
    """
    s = (
        "Dezbaterea Proiectului de lege privind aprobarea Ordonanței de "
        "urgență a Guvernului nr. 142/2007 privind indemnizația acordată "
        "membrilor din România în Parlamentul European (L947/2007)"
    )
    assert _clip_contamination_tail(s) == s


def test_clip_contamination_inline_article_reference_unchanged():
    """Negative: titles citing constitutional/regulation articles like
    `art. 17 alin. (2) și (3) din Legea nr. 47/1992` must NOT be clipped —
    `art. 17 alin.` lacks the `\\d{1,3}\\.\\s+[A-Z]` pattern (lowercase
    `alin`), and `47/1992` has no period after `47`. This is the regex's
    safety guarantee against legitimate inline numbering.
    """
    s = (
        "Notă pentru exercitarea de către senatori a dreptului de sesizare "
        "a Curții Constituționale conform prevederilor art. 17 alin. (2) "
        "și (3) din Legea nr. 47/1992 privind organizarea și funcționarea"
    )
    assert _clip_contamination_tail(s) == s


def test_clip_contamination_legitimate_bill_cite_with_year_unchanged():
    """Negative: a bill cite with year `(L947/2007)` doesn't trigger clip
    even though it has digits — no `\\d. <Capital>` pattern after."""
    s = "Dezbaterea (L947/2007) – continuare a procesului legislativ"
    assert _clip_contamination_tail(s) == s


def test_clip_contamination_earliest_cut_wins():
    """When multiple tiers match, the earliest cut position wins (avoids
    clipping further than necessary)."""
    s = (
        "Item title 4–5 2. Next item content --- --- continuation with "
        "more text 7. Another"
    )
    out = _clip_contamination_tail(s)
    # The page+ord match `4–5 2.` is the earliest boundary
    assert out == "Item title 4–5"


def test_clean_sumar_title_clips_and_preserves_pages():
    """End-to-end: `_clean_sumar_title` clips contamination AND extracts the
    item's trailing pages (which sit just before the next ordinal in the
    captured rest)."""
    rest = (
        "Aprobarea ordinii de zi și a programului de lucru 15 "
        "4. Aprobarea unei modificări 27"
    )
    cleaned, pages = _clean_sumar_title(rest)
    # The clipped title keeps the page `15` for THIS item; the trailing-pages
    # extractor pulls it out and trims it from the cleaned string.
    assert cleaned == "Aprobarea ordinii de zi și a programului de lucru"
    assert pages == [15]


def test_extract_agenda_does_not_misattribute_bills_to_procedural_item():
    """Regression guard for the linker FP root cause: a SUMAR over-capture
    that glues a procedural item (`Aprobarea ordinii de zi`) to a later
    bill_debate item (with an `L99/2010` cite) MUST NOT bleed the bill
    cite into the procedural item's `primary_references[]`.
    """
    body = (
        "SUMAR\n\n"
        "|Nr.<br>1.<br>Aprobarea ordinii de zi și a programului de lucru "
        "15 4. Dezbaterea Proiectului de lege pentru modificarea Legii "
        "nr. 188/1999 privind Statutul funcționarilor publici (L99/2010)|"
        "Pagina<br>15|\n"
        "|---|---|\n"
        "## **Domnul X:**\nText.\n"
    )
    items, _ = extract_agenda(body, len(body), _ctx(body))
    assert items, "expected at least one agenda item"
    # The first item should NOT carry the L99/2010 bill cite — its title
    # ends at `15` (page) and the L99/2010 belongs to the next ordinal that
    # the over-capturing regex glued in.
    bills = [
        r for r in (items[0].get("primary_references") or []) if r["type"] == "bill"
    ]
    assert bills == [], (
        f"procedural agenda item should have no bill refs after clip, got: {bills}"
    )
    # And the title should have been clipped at the contamination boundary
    assert items[0]["title"] == "Aprobarea ordinii de zi și a programului de lucru"


def test_extract_agenda_keeps_legitimate_inline_bill_cite():
    """Positive: when the bill cite is genuinely part of the title's first
    item (no contamination boundary present), it stays in
    `primary_references[]`."""
    body = (
        "SUMAR\n\n"
        "|Nr.<br>1.<br>Dezbaterea Proiectului de lege pentru aprobarea "
        "Ordonanței de urgență a Guvernului nr. 19/2008 (L172/2008)|"
        "Pagina<br>3|\n"
        "|---|---|\n"
        "## **Domnul X:**\nText.\n"
    )
    items, _ = extract_agenda(body, len(body), _ctx(body))
    bills = [
        r for r in (items[0].get("primary_references") or []) if r["type"] == "bill"
    ]
    raws = {b.get("raw") for b in bills}
    assert "L172/2008" in raws
