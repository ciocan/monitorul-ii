"""Unit tests for the plenary interpellation extractor."""

from __future__ import annotations

from datetime import date

from monitorul_ii.extraction.envelope import EnvelopeMeta
from monitorul_ii.extraction.extractors.plenary.interpellations import (
    INTERPELLATIONS_LABEL,
    INTERPELLATIONS_VERSION,
    _extract_question_text,
    extract_interpellations,
    find_interpellation_block,
)
from monitorul_ii.extraction.pipeline import ExtractContext


def _ctx(body: str) -> ExtractContext:
    from monitorul_ii.extraction.coverage import line_offsets

    meta = EnvelopeMeta(issue="1", year=2025, part="II", published=date(2025, 1, 1))
    return ExtractContext(
        body_text=body,
        line_offsets=line_offsets(body),
        content_sha="0123456789ab",
        meta=meta,
        frontmatter={},
    )


# -- boundary detection ---------------------------------------------------


def test_find_block_with_canonical_transition():
    body = (
        "## **Domnul X:**\n\n"
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Doamna Y:**\nInterpellation content.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None
    assert span[0] < span[1]
    assert span[1] == len(body)


def test_find_block_no_transition_returns_none():
    body = "## **Domnul X:**\n\nRegular debate without interpellation block.\n"
    assert find_interpellation_block(body) is None


def test_find_block_intrebari_orale_header():
    body = "Plenary content.\n## **Întrebări orale adresate Guvernului**\nText.\n"
    span = find_interpellation_block(body)
    assert span is not None


def test_find_block_picks_earliest_transition():
    """When multiple transition phrases appear, earliest wins."""
    body = (
        "Pre-content.\n"
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "Some intermediate text.\n"
        "Începem ora interpelărilor.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None
    # Earliest = "Trecem la primirea..."
    assert "primirea" in body[span[0] : span[0] + 100]


def test_find_block_declar_deschisa_form():
    """Modern chair opener: 'Declar deschisă sesiunea de întrebări, interpelări'."""
    body = (
        "## **Domnul Mircea Abrudean:**\n\n"
        "Stimați colegi, declar deschisă sesiunea de întrebări, interpelări "
        "și răspunsuri.\n"
        "## **Doamna X:**\nInterpellation body.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None


def test_find_block_incepem_sesiunea_form():
    """2017 chair opener: 'Începem sesiunea de întrebări și interpelări'."""
    body = (
        "Procedural content.\n"
        "Începem sesiunea de întrebări și interpelări.\n"
        "## **Domnul X:**\nBody.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None


def test_find_block_urmeaza_prezentarea_form():
    """2009-era chair opener: 'Urmează prezentarea pe scurt a interpelărilor'."""
    body = (
        "Final votes done.\n"
        "În continuare, urmează prezentarea pe scurt a interpelărilor.\n"
        "## **Domnul X:**\nBody.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None


def test_find_block_deschidem_sedinta_consacrata_form():
    """2009-era chair opener: 'Deschidem ședința consacrată răspunsurilor'."""
    body = (
        "Pre-content.\n"
        "Deschidem ședința consacrată răspunsurilor orale la întrebări.\n"
        "## **Domnul X:**\nBody.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None


def test_find_block_excludes_sumar_table_row():
    """SUMAR table cells like '|N.|Răspunsuri la interpelări|' must not fire.

    The 'Răspunsuri la interpelări.' bare-line pattern is line-anchored and
    rejects lines containing pipes (table cells).
    """
    body = (
        "|3.|Răspunsuri la interpelări și răspunsuri ......... |1–12|\n"
        "Other content with no real transition.\n"
    )
    assert find_interpellation_block(body) is None


# -- per-interpellation parsing ------------------------------------------


def test_extract_interpellations_basic():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Test Senator:**\n\n"
        "Adresez această interpelare Ministerului Educației.\n"
        "Solicit răspuns în scris.\n"
        "Nr. 1.234A\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    interp = interps[0]
    assert interp["genre"] in ("interpelare", "întrebare")
    assert interp["questioner"]["name"] == "Test Senator"
    assert interp["interpellation_number"] == "1.234A"
    assert interp["response_deferred"] is True


def test_extract_interpellations_default_genre_interpelare():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Speaker One:**\n\n"
        "Interpelare adresată ministrului.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["genre"] == "interpelare"


def test_extract_interpellations_intrebare_genre():
    """When 'întrebare' phrasing dominates, genre flips to întrebare."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Speaker One:**\n\n"
        "Adresez o întrebare ministrului. Întrebarea mea este simplă.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["genre"] == "întrebare"


def test_extract_interpellations_response_deferred_in_scris():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Test (în scris):**\n\n"
        "Body of interpellation.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["response_deferred"] is True


def test_extract_interpellations_emits_record_claims():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul A:**\n\nInterpellation A.\n"
        "## **Doamna B:**\n\nInterpellation B.\n"
    )
    span = find_interpellation_block(body)
    interps, claims = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 2
    record_claims = [c for c in claims if c.kind == "record"]
    assert len(record_claims) == 2


def test_extract_interpellations_empty_block():
    """Block with transition but no interpellation headers returns []."""
    body = "Trecem la primirea răspunsurilor la interpelări.\nNo headers here.\n"
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps == []


# -- question_text extraction (v0.2.0) -----------------------------------


def test_question_text_strips_pleasantries_and_preamble():
    """Opening "Mulțumesc..." + "Voi da curs citirii..." preamble dropped."""
    turn = (
        "\n"
        "Mulțumesc, domnule președinte de ședință.\n\n"
        "Voi da curs citirii interpelării adresate ministrului culturii, "
        "domnului Demeter András, cu obiectul: „Biertan – patrimoniu UNESCO”.\n\n"
        "Stimate domnule ministru,\n\n"
        "În comuna Biertan, județul Sibiu, s-au desfășurat lucrări fără "
        "supraveghere arheologică, deși localitatea are statut de sit UNESCO.\n\n"
        "Vă rog să-mi răspundeți la întrebare: ce măsuri veți lua?\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert qt.startswith("Stimate domnule ministru,")
    assert "Mulțumesc" not in qt
    assert "Voi da curs" not in qt
    assert "Vă rog să-mi răspundeți" in qt


def test_question_text_caps_at_solicit_raspuns():
    """ "Solicit răspuns în scris" trims the closing + signature tail."""
    turn = (
        "\nDomnule ministru,\n\n"
        "Aici este corpul interpelării despre o problemă reală care necesită "
        "atenție și un răspuns concret din partea Guvernului.\n\n"
        "Solicit răspuns în scris, în termen de 15 zile.\n\n"
        "Cu stimă, Senator AUR.\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert qt.startswith("Domnule ministru,")
    assert "Solicit răspuns" not in qt
    assert "Cu stimă" not in qt


def test_question_text_caps_at_section_break_political_declaration():
    """Same speaker continues into a political declaration → cap there."""
    turn = (
        "\nDomnule ministru,\n\n"
        "Iată întrebarea mea cu privire la un subiect important pentru "
        "cetățeni, care necesită un răspuns concret din partea Executivului.\n\n"
        "Voi citi și declarația politică cu titlul: „Modernizarea barajelor”.\n\n"
        "Doamnelor și domnilor senatori, această declarație tratează un alt subiect.\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert "Domnule ministru" in qt
    assert "declarația politică" not in qt
    assert "Modernizarea barajelor" not in qt


def test_question_text_returns_none_when_too_short():
    """Tiny residue after stripping → None (no real content)."""
    turn = "\nMulțumesc.\nSolicit răspuns în scris.\n"
    assert _extract_question_text(turn) is None


def test_question_text_returns_none_for_empty_turn():
    assert _extract_question_text("") is None
    assert _extract_question_text("\n\n") is None


def test_question_text_drops_old_form_interpelarea_este_adresata():
    """2009-era "Interpelarea este adresată..." preamble line dropped."""
    turn = (
        "\nDoamnă președinte,\n\n"
        "Stimați colegi,\n\n"
        "Interpelarea este adresată domnului ministru Adriean Videanu și are "
        "ca subiect strategia pe termen lung.\n\n"
        "Domnule ministru,\n\n"
        "Anul 2009 este unul deosebit de slab din punct de vedere economic, "
        "iar firmele și-au închis porțile pe fondul crizei mondiale.\n\n"
        "Vă întreb, domnule ministru, când va avea România o strategie?\n\n"
        "Solicit răspuns în scris și oral.\n\n"
        "Deputat Gheorghe Ana. Vă mulțumesc.\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert qt.startswith("Domnule ministru,")
    assert "Doamnă președinte" not in qt
    assert "Stimați colegi" not in qt
    assert "Interpelarea este adresată" not in qt
    assert "Solicit răspuns" not in qt
    assert "Deputat Gheorghe Ana" not in qt


def test_question_text_filled_in_extracted_interpellation():
    """End-to-end: question_text appears on the emitted interpellation dict."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Test Senator:**\n\n"
        "Domnule ministru,\n\n"
        "Aceasta este o întrebare reală despre o problemă importantă pentru "
        "cetățenii din circumscripția mea.\n\n"
        "Solicit răspuns în scris.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    qt = interps[0]["question_text"]
    assert qt is not None
    assert qt.startswith("Domnule ministru,")
    assert "Solicit răspuns" not in qt
    # Confidence bumped when question_text is recovered
    assert interps[0]["extraction"]["confidence"] >= 0.85
    assert interps[0]["extraction"]["extractor"] == INTERPELLATIONS_LABEL


def test_question_text_null_when_only_preamble():
    """End-to-end: residue too short → question_text remains None."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Test:**\n\n"
        "Mulțumesc.\n"
        "Solicit răspuns în scris.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    assert interps[0]["question_text"] is None
    # Confidence stays at the no-question-text baseline
    assert interps[0]["extraction"]["confidence"] < 0.85


def test_interpellations_version_is_v0_2():
    assert INTERPELLATIONS_VERSION == "0.2.0"
    assert "0.2.0" in INTERPELLATIONS_LABEL
