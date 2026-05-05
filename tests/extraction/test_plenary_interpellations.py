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
    assert INTERPELLATIONS_VERSION == "0.2.2"
    assert "0.2.2" in INTERPELLATIONS_LABEL


# -- precision fixes (v0.2.1+) -------------------------------------------


def test_header_regex_rejects_mo_footer():
    """`## **EDITOR: GUVERNUL ROMÂNIEI**` is the trailing MO footer, not a
    speaker header — must NOT be matched as an interpellation header.
    Regression for v0.2.1 fix.
    """
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Real Senator:**\n\n"
        "Real interpellation body content here, sufficient text to count.\n"
        "Solicit răspuns în scris.\n\n"
        "## **EDITOR: GUVERNUL ROMÂNIEI**\n\n"
        "„Monitorul Oficial” R.A., Str. Parcului nr. 65...\n"
    )
    span = find_interpellation_block(body)
    assert span is not None
    interps, _ = extract_interpellations(body, span, _ctx(body))
    # Only the real senator should be emitted, not the EDITOR footer
    assert len(interps) == 1
    assert interps[0]["questioner"]["name"] == "Real Senator"
    # No interpellation should have "EDITOR" in its name
    for it in interps:
        n = (it["questioner"].get("name") or "").upper()
        assert "EDITOR" not in n
        assert "MONITORUL" not in n


def test_header_regex_rejects_abonamente_block():
    """`## **A B O N A M E N T E**` subscription rate-card is not a header."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Speaker:**\n\nReal body text long enough to count.\n"
        "## **A B O N A M E N T E**\nSubscription rates and pricing.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    assert "ABONAMENTE" not in (
        interps[0]["questioner"].get("name") or ""
    ).upper().replace(" ", "")


def test_pure_declaration_turn_skipped():
    """A turn whose first content is `Titlul declarației:` and contains no
    interpellation phrasing is a political declaration only — must NOT
    appear in interpellations[].
    """
    body = (
        "Declar deschisă sesiunea de întrebări, interpelări și răspunsuri.\n"
        "## **Doamna Carmen Orban:**\n\n"
        "Mulțumesc, domnule președinte.\n\n"
        "Titlul declarației: „Soarta învățământului în limba română”.\n\n"
        "Stimați colegi, vin în fața dumneavoastră cu un profund sentiment...\n"
        "Cultura nu poate fi negociată. Vă mulțumesc.\n\n"
        "Senator Carmen Orban.\n"
    )
    span = find_interpellation_block(body)
    interps, claims = extract_interpellations(body, span, _ctx(body))
    assert interps == []
    # The skipped span should be claimed as boilerplate
    bp_reasons = {c.reason for c in claims if c.kind == "boilerplate"}
    assert "plenary_stenogram.interpellation_pure_declaration" in bp_reasons


def test_pure_declaration_kept_when_interpellation_also_present():
    """When the same speaker reads both a declaration AND an interpellation,
    keep the entry — `_extract_question_text` caps at the declaration break.
    """
    body = (
        "Declar deschisă sesiunea de întrebări, interpelări și răspunsuri.\n"
        "## **Doamna X:**\n\n"
        "Domnule ministru,\n\n"
        "Adresez această interpelare cu privire la o problemă reală importantă\n"
        "pentru cetățenii din circumscripție. Vă rog răspuns concret.\n\n"
        "Voi citi și declarația politică cu titlul: „Mediul rural”.\n"
        "Conținutul declarației...\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    qt = interps[0]["question_text"]
    assert qt is not None
    assert "Domnule ministru" in qt
    assert "declarația politică" not in qt


def test_titlul_declaratiei_caps_question_text():
    """`Titlul declarației:` mid-turn caps question_text before the declaration."""
    turn = (
        "\nDomnule ministru,\n\n"
        "Aici este corpul interpelării — chestiune importantă pentru cetățeni\n"
        "care necesită un răspuns concret din partea Guvernului.\n\n"
        "Titlul declarației: „Mediul rural”.\n\n"
        "Stimați colegi, declarația politică continuă aici cu alt subiect.\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert "Domnule ministru" in qt
    assert "Titlul declarației" not in qt
    assert "Mediul rural" not in qt


def test_chair_filter_skips_known_chair_speakers():
    """Headers whose speaker matches session.chair[] / secretaries[] names
    are emitted as boilerplate, not as interpellation records."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Senate President:**\n\n"
        "Vă rog să luați loc. Are cuvântul domnul senator Real Speaker.\n\n"
        "## **Domnul Real Speaker:**\n\n"
        "Domnule ministru, am o interpelare reală cu privire la sănătate care\n"
        "merită un răspuns concret. Vă rog să răspundeți.\n"
    )
    span = find_interpellation_block(body)
    chair_names = {"Senate President"}
    interps, claims = extract_interpellations(
        body, span, _ctx(body), chair_names=chair_names
    )
    assert len(interps) == 1
    assert interps[0]["questioner"]["name"] == "Real Speaker"
    bp_reasons = {c.reason for c in claims if c.kind == "boilerplate"}
    assert "plenary_stenogram.interpellation_chair_turn" in bp_reasons


def test_chair_filter_case_insensitive():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul SENATE PRESIDENT:**\n\nProcedural text from chair.\n"
        "## **Domnul Real Speaker:**\n\n"
        "Domnule ministru, am o interpelare reală cu privire la sănătate care\n"
        "merită un răspuns concret.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(
        body, span, _ctx(body), chair_names={"senate president"}
    )
    assert len(interps) == 1
    assert interps[0]["questioner"]["name"] == "Real Speaker"


def test_extract_interpellations_no_chair_names_back_compat():
    """Calling without chair_names (legacy) keeps every header as before."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul A:**\n\nBody A.\n"
        "## **Doamna B:**\n\nBody B.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 2


def test_titlul_declaratiei_este_form_caps_qt():
    """`Titlul declarației politice este: „...”` form (no colon after
    declarației, uses `este`) caps question_text."""
    turn = (
        "\nDomnule ministru,\n\n"
        "Aceasta este o întrebare reală despre o problemă de interes public\n"
        "care merită un răspuns concret din partea Guvernului.\n\n"
        "Titlul declarației mele politice de astăzi este: „Stop abuzurilor”.\n"
        "Conținutul declarației politice...\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert "Domnule ministru" in qt
    assert "Stop abuzurilor" not in qt
    assert "Titlul declar" not in qt


def test_declaratie_politica_adresata_form():
    """`Declarație politică adresată...` form treated as section break."""
    turn = (
        "\nDomnule ministru,\n\n"
        "Aceasta este o întrebare reală despre o problemă de interes public\n"
        "care merită un răspuns concret din partea Guvernului.\n\n"
        "Declarație politică adresată Președintelui României, "
        "domnului Klaus Iohannis.\n"
    )
    qt = _extract_question_text(turn)
    assert qt is not None
    assert "Domnule ministru" in qt
    assert "Klaus Iohannis" not in qt


def test_pure_declaration_titlul_este_form_skipped():
    """Turn whose head says `Titlul declarației ... este X` is declaration-only."""
    body = (
        "Declar deschisă sesiunea de întrebări, interpelări și răspunsuri.\n"
        "## **Domnul X:**\n\n"
        "Mulțumesc.\n\n"
        "Titlul declarației mele politice de astăzi este: „Tema de azi”.\n"
        "Conținutul declarației...\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps == []


def test_abonamente_footer_rejected_as_boilerplate():
    """`## **ABONAMENTE LA PUBLICAȚIILE OFICIALE ... DISTRIBUȚIE:**` is
    the MO subscriptions footer, not a speaker — must be claimed as
    boilerplate and not appear in interpellations[]."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Real:**\n\n"
        "Real interpellation body content with substance for the question.\n"
        "## **ABONAMENTE LA PUBLICAȚIILE OFICIALE ȘI COMENZI CĂTRE „MONITORUL "
        'OFICIAL" R.A. SE POT EFECTUA PRIN URMĂTOARELE SOCIETĂȚI DE DISTRIBUȚIE:**\n\n'
        "List of distribution channels...\n"
    )
    span = find_interpellation_block(body)
    interps, claims = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    assert interps[0]["questioner"]["name"] == "Real"
    bp_reasons = {c.reason for c in claims if c.kind == "boilerplate"}
    assert "plenary_stenogram.interpellation_footer_match" in bp_reasons


def test_all_caps_short_header_rejected():
    """A 30+ char ALL-CAPS inner is NOT a real speaker name."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Real:**\n\n"
        "Real body content with sufficient text.\n"
        "## **ANUNȚ IMPORTANT PENTRU TOȚI SENATORII PREZENȚI ÎN SALĂ:**\n\n"
        "Some announcement.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    assert interps[0]["questioner"]["name"] == "Real"


# -- response extraction (v0.2.2) ----------------------------------------


def test_response_paired_single_line_role():
    """`## **NAME** _– secretar de stat în Ministerul X_ **:**` single-line form
    pairs with the most recent questioner."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Senator A:**\n\n"
        "Domnule ministru, am o întrebare reală despre o problemă din "
        "circumscripție care merită un răspuns concret și ferm.\n\n"
        "## **Domnul Minister X** _– secretar de stat în Ministerul Sănătății_ **:**\n\n"
        "Mulțumesc, domnule senator.\n\n"
        "Ca urmare a întrebării formulate de dumneavoastră, vă comunicăm "
        "următoarele: Ministerul Sănătății analizează problema și va emite un răspuns scris.\n"
    )
    span = find_interpellation_block(body)
    interps, claims = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    resp = interps[0].get("response")
    assert resp is not None
    assert resp["speaker"]["name"] == "Minister X"
    assert "secretar de stat" in resp["speaker"]["role"]
    assert "Ministerul Sănătății" in resp["speaker"]["role"]
    assert "Ca urmare a întrebării" in resp["text"]
    assert "Mulțumesc" not in resp["text"]  # pleasantry stripped


def test_response_paired_multiline_role():
    """`## **NAME** _– secretar de stat_\\n\\n_în Ministerul Y_ **:**`
    multi-line form (continuation has no `## ` prefix)."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Doamna Senator B:**\n\n"
        "Domnule ministru, mi-am exprimat preocuparea cu privire la o problemă "
        "importantă care necesită clarificare și un răspuns oficial.\n\n"
        "## **Domnul Y** _– secretar de stat_\n\n"
        "_în Ministerul Educației_ **:**\n\n"
        "Stimată doamnă senator,\n\n"
        "În legătură cu întrebarea adresată Ministerului Educației, vă "
        "comunicăm că analiza se află în curs de finalizare.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    resp = interps[0].get("response")
    assert resp is not None
    assert resp["speaker"]["name"] == "Y"
    assert "secretar de stat" in resp["speaker"]["role"]
    assert "Ministerul Educației" in resp["speaker"]["role"]
    assert "În legătură cu întrebarea" in resp["text"]


def test_response_paired_multiline_with_hash_continuation():
    """Continuation line CARRIES the `## ` prefix
    (`## _în Ministerul X_ **:**`)."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Senator C:**\n\n"
        "Domnule ministru, întrebarea mea este simplă: cum veți rezolva "
        "problema raportată de cetățeni? Aștept un răspuns concret.\n\n"
        "## **Domnul Z** _– secretar de stat_\n\n"
        "## _în Ministerul Muncii_ **:**\n\n"
        "Vă răspund pe această problemă imediat. Ministerul Muncii a "
        "analizat și constatăm că situația va fi rezolvată în 30 de zile.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    resp = interps[0].get("response")
    assert resp is not None
    assert "Muncii" in resp["speaker"]["role"]


def test_response_skipped_when_role_not_responder():
    """A `## **NAME** _– president of group_` italic-decorated header
    (NOT a minister role) does NOT count as a response."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Senator D:**\n\n"
        "Domnule ministru, întrebarea mea este simplă: când veți "
        "răspunde concret la cererile cetățenilor?\n\n"
        "## **Domnul Random** _– deputat PNL_ **:**\n\n"
        "Some text from a deputy, not a minister response.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    assert interps[0].get("response") is None


def test_response_orphan_no_questioner_before():
    """If the responder header appears before any emitted questioner,
    no pairing happens (no crash either)."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Z** _– secretar de stat în Ministerul X_ **:**\n\n"
        "Some response text without a preceding questioner.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    # No questioner emitted, no record to attach response to
    assert interps == []


def test_topic_prefers_quoted_subject():
    """Topic detection prefers Romanian-quoted subject over first line."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul X:**\n\n"
        "Mulțumesc, domnule președinte.\n\n"
        "Adresez această interpelare cu obiectul: „Reforma sistemului "
        "sanitar din România în 2025”.\n\n"
        "Domnule ministru, vă rog răspuns concret.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    assert interps[0]["topic"] == "Reforma sistemului sanitar din România în 2025"


def test_topic_skips_minister_salutation_in_fallback():
    """When no quote/object marker exists, fallback skips `Domnule ministru,`
    and similar salutation lines."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Y:**\n\n"
        "Mulțumesc, domnule președinte.\n\n"
        "Domnule ministru,\n\n"
        "Vă întreb concret: care este planul de finanțare pentru spitale?\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    topic = interps[0]["topic"]
    assert topic is not None
    assert "Domnule ministru" not in topic
    assert "Mulțumesc" not in topic
    # Should land on the actual sentence with content
    assert (
        "plan" in topic.lower()
        or "finanț" in topic.lower()
        or "spital" in topic.lower()
    )


def test_topic_uses_obiectul_capture():
    """`obiectul interpelării: ...` capture used when no quotes present."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Z:**\n\n"
        "Domnule ministru, obiectul interpelării: situația financiară a "
        "spitalelor din regiune. Vă rog răspuns scris.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    topic = interps[0]["topic"]
    assert topic is not None
    assert "situația financiară" in topic.lower()


def test_topic_french_quotes_supported():
    """«...» quotes also recognised as subject markers."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul W:**\n\n"
        "Adresez interpelarea cu obiectul «Educația în limba maternă».\n"
        "Domnule ministru, vă rog răspuns concret.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["topic"] == "Educația în limba maternă"


def test_response_pairs_to_immediately_preceding_questioner():
    """When multiple questioners exist, response pairs to the LAST one."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul A:**\n\n"
        "Domnule ministru, am o primă întrebare despre o problemă care "
        "necesită răspuns concret din partea ministerului.\n\n"
        "## **Doamna B:**\n\n"
        "Domnule ministru, am a doua întrebare despre o altă problemă "
        "complet diferită care merită atenția dumneavoastră.\n\n"
        "## **Domnul Y** _– ministru al sănătății_ **:**\n\n"
        "Mulțumesc.\n\n"
        "Vă răspund la a doua întrebare cu o explicație detaliată "
        "despre problema raportată de doamna senator.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 2
    # First questioner has no response; second does
    assert interps[0].get("response") is None
    assert interps[1].get("response") is not None
    assert "a doua întrebare" in interps[1]["response"]["text"]
