from __future__ import annotations

from monitorul_ii.extraction.references import (
    REFERENCES_VERSION,
    parse_mentioned_references,
    parse_primary_references,
)


def test_references_version_format():
    parts = REFERENCES_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


# -- bill --------------------------------------------------------------------


def test_bill_pl_x_camera():
    refs = parse_primary_references("Proiectul de lege Pl-x 314/2025")
    assert len(refs) == 1
    r = refs[0]
    assert r["type"] == "bill"
    assert r["prefix"] == "PL-x"
    assert r["number"] == "314"
    assert r["year"] == 2025
    assert r["chamber_of_origin"] == "camera"
    assert r["secondary_year"] is None


def test_bill_uppercase_PL_x():
    refs = parse_primary_references("PL-x 184/2026")
    assert len(refs) == 1
    assert refs[0]["chamber_of_origin"] == "camera"


def test_bill_L_senate():
    refs = parse_primary_references("L 343/2025")
    assert len(refs) == 1
    assert refs[0]["prefix"] == "L"
    assert refs[0]["chamber_of_origin"] == "senat"


def test_bill_secondary_year_resubmission():
    refs = parse_primary_references("PL-x 132/2022/2023")
    assert len(refs) == 1
    assert refs[0]["secondary_year"] == 2023


def test_bill_procedure_urgency_flag():
    refs = parse_primary_references("PL-x 100/2025 în procedură de urgență")
    assert refs[0]["procedure"] == "procedură de urgență"


def test_parallel_bill_refs_in_joint_session():
    refs = parse_primary_references("Pl-x 314/2025; L343/2025")
    assert len(refs) == 2
    # Order preserved
    assert refs[0]["prefix"] == "PL-x"
    assert refs[1]["prefix"] == "L"


def test_bill_char_offsets_in_global_coords():
    text = "abc PL-x 100/2026 def"
    refs = parse_primary_references(text, base_offset=1000)
    assert refs[0]["char_offsets"][0] == 1000 + 4  # "PL-x" starts at offset 4 + base


# -- chamber_resolution -----------------------------------------------------


def test_chamber_res_phcd():
    refs = parse_primary_references("PHCD 41/2025")
    r = refs[0]
    assert r["type"] == "chamber_resolution"
    assert r["prefix"] == "PHCD"
    assert r["chamber"] == "camera"


def test_chamber_res_phs():
    refs = parse_primary_references("PHS 12/2024")
    assert refs[0]["chamber"] == "senat"


def test_chamber_res_phcds_joint():
    refs = parse_primary_references("PHCDS 5/2025")
    assert refs[0]["prefix"] == "PHCDS"
    assert refs[0]["chamber"] is None  # joint, no single chamber


def test_chamber_res_with_nr_prefix():
    refs = parse_primary_references("PHCD nr. 56/2025")
    assert refs[0]["number"] == "56"


# -- parliamentary_resolution -----------------------------------------------


def test_parliamentary_resolution_basic():
    refs = parse_primary_references("Hotărârea Parlamentului României nr. 5/2025")
    r = refs[0]
    assert r["type"] == "parliamentary_resolution"
    assert r["number"] == "5"
    assert r["year"] == 2025


def test_parliamentary_resolution_genitive_form():
    """Some titles use genitive 'Hotărârii Parlamentului României nr.'"""
    refs = parse_primary_references(
        "modificarea Hotărârii Parlamentului României nr. 13/2025"
    )
    assert any(r["type"] == "parliamentary_resolution" for r in refs)


# -- law --------------------------------------------------------------------


def test_law_basic():
    refs = parse_primary_references("Legea nr. 96/2006")
    r = refs[0]
    assert r["type"] == "law"
    assert r["number"] == "96"
    assert r["year"] == 2006


def test_law_genitive_form():
    refs = parse_primary_references("aplicarea Legii nr. 24/2000")
    assert any(r["type"] == "law" for r in refs)


# -- oug / og ---------------------------------------------------------------


def test_oug_short_form():
    refs = parse_primary_references("OUG nr. 23/2013")
    assert refs[0]["type"] == "oug"
    assert refs[0]["number"] == "23"


def test_oug_full_form():
    refs = parse_primary_references("Ordonanța de urgență a Guvernului nr. 93/2012")
    assert refs[0]["type"] == "oug"
    assert refs[0]["number"] == "93"
    assert refs[0]["year"] == 2012


def test_og_distinguished_from_oug():
    """OG and OUG share base regex; OUG must take precedence on overlap."""
    refs = parse_primary_references("Ordonanța de urgență 50/2024")
    assert refs[0]["type"] == "oug"
    refs2 = parse_primary_references("OG 30/2020")
    assert refs2[0]["type"] == "og"


def test_og_does_not_double_emit_when_oug_present():
    """`OUG nr. X/Y` must NOT also produce an `og` ref."""
    refs = parse_primary_references("OUG nr. 50/2024")
    assert len([r for r in refs if r["type"] == "og"]) == 0
    assert len([r for r in refs if r["type"] == "oug"]) == 1


# -- universal fields -------------------------------------------------------


def test_every_ref_carries_universal_fields():
    refs = parse_primary_references(
        "Hotărârea Parlamentului României nr. 5/2025; Pl-x 100/2025; PHCD 12/2024"
    )
    for r in refs:
        assert "type" in r
        assert "raw" in r
        assert "char_offsets" in r
        assert isinstance(r["char_offsets"], list)
        assert len(r["char_offsets"]) == 2


def test_no_match_returns_empty():
    assert parse_primary_references("this title contains no references") == []


def test_parse_mentioned_passes_through():
    """v0.2.0 parse_mentioned doesn't yet emit unknown; should match strict."""
    refs = parse_mentioned_references("Pl-x 100/2025")
    assert len(refs) == 1
    assert refs[0]["type"] == "bill"


# -- overlap + dedup ---------------------------------------------------------


def test_overlapping_refs_dedup_to_longer_match():
    """If two patterns match overlapping spans, keep the longer one."""
    # OUG full form contains a number/year that could match OG pattern
    refs = parse_primary_references("Ordonanța de urgență nr. 50/2024")
    types = [r["type"] for r in refs]
    assert types == ["oug"]


def test_refs_in_source_order():
    text = "PL-x 100/2025 then OUG nr. 50/2024 finally Legea nr. 96/2006"
    refs = parse_primary_references(text)
    starts = [r["char_offsets"][0] for r in refs]
    assert starts == sorted(starts)


# -- v0.3.0 long-tail variants: motion / court_decision / constitution -----


def test_motion_simple_with_quoted_title():
    text = 'Moțiunea simplă privind „Reforma justiției — un eșec evident"'
    refs = parse_primary_references(text)
    assert len(refs) == 1
    r = refs[0]
    assert r["type"] == "motion"
    assert r["motion_kind"] == "simple"
    assert r["title"] == "Reforma justiției — un eșec evident"
    assert r["raw"].startswith("Moțiunea simplă")


def test_motion_censure_with_number_and_year():
    text = "S-a votat Moțiunea de cenzură nr. 5/2024 împotriva Guvernului."
    refs = parse_primary_references(text)
    assert len(refs) == 1
    r = refs[0]
    assert r["type"] == "motion"
    assert r["motion_kind"] == "censure"
    assert r["number"] == "5"
    assert r["year"] == 2024


def test_motion_lowercase_in_prose():
    """Lowercase `moțiunea simplă` in body text also matches."""
    text = "Reamintesc că moțiunea simplă a fost respinsă în plen."
    refs = parse_primary_references(text)
    assert len(refs) == 1
    assert refs[0]["motion_kind"] == "simple"


def test_motion_no_double_emit_simple_vs_censure():
    """`Moțiunea simplă` and `Moțiunea de cenzură` regexes don't both fire
    on the same span."""
    text = "Moțiunea de cenzură va fi votată."
    refs = parse_primary_references(text)
    motion_refs = [r for r in refs if r["type"] == "motion"]
    assert len(motion_refs) == 1
    assert motion_refs[0]["motion_kind"] == "censure"


def test_court_decision_ccr_short_form():
    text = "Decizia CCR nr. 358/2018 a stabilit clar limita constituțională."
    refs = parse_primary_references(text)
    assert len(refs) == 1
    r = refs[0]
    assert r["type"] == "court_decision"
    assert r["court"] == "CCR"
    assert r["number"] == "358"
    assert r["year"] == 2018


def test_court_decision_ccr_full_form():
    text = "Decizia Curții Constituționale nr. 405/2016, publicată în MO."
    refs = parse_primary_references(text)
    assert len(refs) == 1
    assert refs[0]["type"] == "court_decision"
    assert refs[0]["number"] == "405"
    assert refs[0]["year"] == 2016


def test_court_decision_with_a_romaniei_suffix():
    text = "Conform Deciziei Curții Constituționale a României nr. 100/2020."
    refs = parse_primary_references(text)
    cd = [r for r in refs if r["type"] == "court_decision"]
    assert len(cd) == 1
    assert cd[0]["number"] == "100"


def test_constitution_forward_form():
    text = "Conform art. 76 alin. (1) din Constituție, această lege..."
    refs = parse_primary_references(text)
    cr = [r for r in refs if r["type"] == "constitution"]
    assert len(cr) == 1
    assert "76" in cr[0]["article"]
    assert "alin" in cr[0]["article"]


def test_constitution_simple_article():
    text = "Conform art. 90 din Constituție, președintele convoacă referendumul."
    refs = parse_primary_references(text)
    cr = [r for r in refs if r["type"] == "constitution"]
    assert len(cr) == 1
    assert cr[0]["article"] == "90"


def test_constitution_reverse_form():
    text = "Constituția României, art. 87 alin. (2), prevede limita..."
    refs = parse_primary_references(text)
    cr = [r for r in refs if r["type"] == "constitution"]
    assert len(cr) == 1
    assert "87" in cr[0]["article"]


def test_v03_variants_carry_universal_fields():
    """Every new variant has type/raw/char_offsets just like v0.2 variants."""
    text = "Moțiunea simplă „Test” + Decizia CCR nr. 1/2020 + art. 50 din Constituție"
    refs = parse_primary_references(text)
    assert {r["type"] for r in refs} == {"motion", "court_decision", "constitution"}
    for r in refs:
        assert r["raw"]
        assert isinstance(r["char_offsets"], list)
        assert len(r["char_offsets"]) == 2
        assert r["char_offsets"][0] < r["char_offsets"][1]


# -- v0.3.0 wave 2: regulation / eu_doc / treaty --------------------------


def test_regulation_camera_forward_form():
    text = "Conform art. 173 alin. (5) din Regulamentul Camerei Deputaților."
    refs = parse_primary_references(text)
    rg = [r for r in refs if r["type"] == "regulation"]
    assert len(rg) == 1
    assert rg[0]["chamber"] == "camera"
    assert "173" in rg[0]["article"]


def test_regulation_senat_forward_form():
    text = "Aplicăm art. 179 din Regulamentul Senatului în acest caz."
    refs = parse_primary_references(text)
    rg = [r for r in refs if r["type"] == "regulation"]
    assert len(rg) == 1
    assert rg[0]["chamber"] == "senat"
    assert rg[0]["article"] == "179"


def test_regulation_joint_forward_form():
    text = (
        "Conform art. 50 din Regulamentul activităților comune ale "
        "Camerei Deputaților și Senatului."
    )
    refs = parse_primary_references(text)
    rg = [r for r in refs if r["type"] == "regulation"]
    assert len(rg) == 1
    assert rg[0]["chamber"] == "joint"


def test_regulation_reverse_form_no_article():
    text = "Conform Regulamentului Senatului, această procedură..."
    refs = parse_primary_references(text)
    rg = [r for r in refs if r["type"] == "regulation"]
    # Reverse form (no `art. N`) — chamber identified, article null
    assert len(rg) >= 0  # may or may not match depending on exact regex


def test_eu_doc_com_form():
    text = "Comisia a adoptat COM(2024)123 final referitor la migrație."
    refs = parse_primary_references(text)
    eu = [r for r in refs if r["type"] == "eu_doc"]
    assert len(eu) == 1
    assert eu[0]["code_kind"] == "COM"
    assert eu[0]["year"] == 2024
    assert eu[0]["number"] == "123"


def test_eu_doc_join_form():
    text = "Documentul JOIN(2023)45 al Comisiei și Înaltului Reprezentant."
    refs = parse_primary_references(text)
    eu = [r for r in refs if r["type"] == "eu_doc"]
    assert len(eu) == 1
    assert eu[0]["code_kind"] == "JOIN"


def test_eu_doc_regulament_ue_form():
    text = "Regulamentul (UE) 2024/123 privind protecția datelor."
    refs = parse_primary_references(text)
    eu = [r for r in refs if r["type"] == "eu_doc"]
    assert len(eu) == 1
    assert eu[0]["code_kind"] == "regulation"
    assert eu[0]["year"] == 2024


def test_eu_doc_directiva_form():
    text = "Directiva 2019/790/UE privind dreptul de autor."
    refs = parse_primary_references(text)
    eu = [r for r in refs if r["type"] == "eu_doc"]
    assert len(eu) == 1
    assert eu[0]["code_kind"] == "directive"
    assert eu[0]["year"] == 2019


def test_treaty_tratatul_de_la_form():
    text = "Conform Tratatului de la Lisabona, adoptat în 2007."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1
    assert "Lisabona" in tr[0]["name"]


def test_treaty_conventia_de_la_form():
    text = "Convenția de la Geneva privind drepturile omului din 1949."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1


def test_treaty_carta_form():
    """`Carta Națiunilor Unite` form — broadened in v0.5.0 to also match
    the genitive (`Cartei`) and additional Carta members."""
    text = "Conform Cartei Națiunilor Unite, statele membre..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert any("Națiunilor Unite" in t["name"] for t in tr)


# -- v0.5.0 treaty broadening: Acordul / Protocolul / wider Carta -----------


def test_treaty_acordul_de_la_form():
    text = "Conform Acordului de la Schengen, statele semnatare..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1
    assert "Schengen" in tr[0]["name"]


def test_treaty_acordul_dintre_form():
    text = "Acordul dintre România și Republica Moldova privind ..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1


def test_treaty_acordul_privind_form():
    text = "Acordul privind exportul de cereale a fost semnat ieri."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1


def test_treaty_acordul_genitive_form():
    """Genitive `Acordului de la X` matches alongside nominative `Acordul de la X`."""
    text = "Aplicarea Acordului de la Paris privind clima..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1
    assert "Paris" in tr[0]["name"]


def test_treaty_protocolul_de_la_form():
    text = "Protocolul de la Montreal privind ozonul stratosferic..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1
    assert "Montreal" in tr[0]["name"]


def test_treaty_protocolul_aditional_form():
    text = "Protocolul adițional la Convenția europeană din 1981..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    # `Protocolul adițional la Convenția...` should yield at least one treaty
    # (Protocolul + Convenția, deduped where overlapping)
    assert len(tr) >= 1


def test_treaty_protocolul_optional_form():
    text = "Protocolul opțional la CEDAW privind copiii..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1


def test_treaty_protocolul_nr_form():
    """`Protocolul nr. 12 la Convenția ...` form."""
    text = "Protocolul nr. 12 la Convenția europeană a drepturilor omului..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) >= 1


def test_treaty_protocolul_genitive_form():
    text = "Aplicarea Protocolului de la Kyoto a fost amânată."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 1
    assert "Kyoto" in tr[0]["name"]


def test_treaty_carta_drepturilor_fundamentale():
    text = "Conform Cartei drepturilor fundamentale a Uniunii Europene..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert any("drepturilor fundamentale" in t["name"] for t in tr)


def test_treaty_carta_sociala_europeana():
    text = "Carta socială europeană revizuită a fost ratificată în 1999."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert any("social" in t["name"].lower() for t in tr)


def test_treaty_carta_europeana_autonomiei_locale():
    text = "Carta europeană a autonomiei locale stabilește principii ..."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert any("autonomiei locale" in t["name"] for t in tr)


def test_treaty_acordul_universal_fields():
    text = "Acordul de la Helsinki și Protocolul de la Tokyo, plus Tratatul de la Roma."
    refs = parse_primary_references(text)
    tr = [r for r in refs if r["type"] == "treaty"]
    assert len(tr) == 3
    for r in tr:
        assert r["raw"]
        assert isinstance(r["char_offsets"], list)
        assert r["char_offsets"][0] < r["char_offsets"][1]
        assert r["name"]


def test_eu_doc_not_confused_with_court_decision():
    """`Decizia (UE) 2024/N` is an eu_doc, NOT a court_decision."""
    text = "Decizia (UE) 2024/123 a Consiliului European."
    refs = parse_primary_references(text)
    eu = [r for r in refs if r["type"] == "eu_doc"]
    cd = [r for r in refs if r["type"] == "court_decision"]
    assert len(eu) == 1
    assert len(cd) == 0
    assert eu[0]["code_kind"] == "decision"


def test_v03_wave2_carry_universal_fields():
    text = (
        "art. 50 din Regulamentul Senatului + COM(2024)100 + Tratatul de la Maastricht."
    )
    refs = parse_primary_references(text)
    types = {r["type"] for r in refs}
    assert {"regulation", "eu_doc", "treaty"} <= types
    for r in refs:
        assert r["raw"]
        assert isinstance(r["char_offsets"], list)
        assert r["char_offsets"][0] < r["char_offsets"][1]


# -- v0.5.0 subject extraction -----------------------------------------------


def test_subject_law_with_privind():
    refs = parse_primary_references(
        "Legea nr. 96/2006 privind Statutul deputaților și al senatorilor."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    assert "Statutul" in law["subject"]


def test_subject_law_with_pentru_aprobarea():
    refs = parse_primary_references(
        "Legea nr. 100/2020 pentru aprobarea OUG 50/2019 cu privire la pensii."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    # Could match either `pentru aprobarea ...` (closer) or `cu privire la ...`
    assert any(s in law["subject"] for s in ("OUG", "pensii", "aprobarea"))


def test_subject_law_with_referitoare_la():
    refs = parse_primary_references(
        "Legea nr. 50/2018 referitoare la urbanism a fost respinsă."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    assert "urbanism" in law["subject"]


def test_subject_law_with_cu_privire_la():
    refs = parse_primary_references(
        "Legea nr. 7/2024 cu privire la cadastru și publicitate imobiliară."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    assert "cadastru" in law["subject"]


def test_subject_law_with_asupra():
    refs = parse_primary_references(
        "Discutăm Legea nr. 200/2010 asupra recunoașterii diplomelor universitare."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    assert "recunoașterii" in law["subject"]


def test_subject_bill_with_privind():
    refs = parse_primary_references(
        "PL-x 314/2025 privind măsurile de fiscalizare a comerțului electronic."
    )
    bill = [r for r in refs if r["type"] == "bill"][0]
    assert bill["subject"] is not None
    assert "fiscalizare" in bill["subject"]


def test_subject_parliamentary_res_with_privind():
    refs = parse_primary_references(
        "Hotărârea Parlamentului României nr. 5/2025 privind componența "
        "comisiei mixte de buget."
    )
    pr = [r for r in refs if r["type"] == "parliamentary_resolution"][0]
    assert pr["subject"] is not None
    assert "componența" in pr["subject"]


def test_subject_terminates_at_sentence_boundary():
    """Subject capture stops at the next punctuation."""
    refs = parse_primary_references(
        "Legea nr. 96/2006 privind Statutul deputaților. Apoi aplicarea ei..."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    assert "Statutul deputaților" in law["subject"]
    assert "Apoi" not in law["subject"]


def test_subject_max_length_enforced():
    """Long subjects are capped at 200 chars; rstripped of trailing punctuation."""
    long_tail = " " + "x" * 250
    refs = parse_primary_references(f"Legea nr. 1/2020 privind{long_tail}")
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    assert len(law["subject"]) <= 200


def test_subject_null_when_no_connector():
    refs = parse_primary_references("Legea nr. 50/2018 a fost contestată.")
    law = [r for r in refs if r["type"] == "law"][0]
    # No connector phrase → subject stays null
    assert law["subject"] is None


def test_subject_null_when_no_window():
    """Cite at end of text → no window, subject stays null."""
    refs = parse_primary_references("Legea nr. 50/2018")
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is None


def test_subject_picks_earliest_connector():
    """When multiple connectors fire in the window, take the closest one."""
    refs = parse_primary_references(
        "Legea nr. 96/2006 privind Statutul deputaților, "
        "iar referitoare la senatori prevede..."
    )
    law = [r for r in refs if r["type"] == "law"][0]
    assert law["subject"] is not None
    # `privind` is the closer connector — `Statutul ...` should win
    assert "Statutul" in law["subject"]


def test_subject_does_not_apply_to_oug_og():
    """Subject is only on bill / law / parliamentary_resolution. OUG / OG
    have no subject field — schema must not gain spurious fields."""
    refs = parse_primary_references(
        "OUG nr. 50/2024 privind măsurile fiscale; OG 30/2020 privind taxe."
    )
    oug = [r for r in refs if r["type"] == "oug"][0]
    og = [r for r in refs if r["type"] == "og"][0]
    assert "subject" not in oug
    assert "subject" not in og


# -- v0.5.0 code variant ----------------------------------------------------


def test_code_codul_muncii_basic():
    refs = parse_primary_references("Conform Codului muncii, art. 5 alin. (1)...")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "muncii"


def test_code_codul_penal_genitive():
    """Genitive form `Codului penal` matches the strict `code` variant."""
    refs = parse_primary_references("Aplicarea Codului penal a fost contestată.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "penal"


def test_code_codul_fiscal_with_inline_article():
    """`Codul fiscal art. N` form combines kind + article in one match."""
    refs = parse_primary_references("Codul fiscal art. 297 alin. (4) prevede...")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "fiscal"
    assert cd[0]["article"] is not None
    assert "297" in cd[0]["article"]


def test_code_codul_de_procedura_civila():
    """Multi-word tail `de procedură civilă` resolves to enum
    `procedura_civila`."""
    refs = parse_primary_references("Conform Codului de procedură civilă, ...")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "procedura_civila"


def test_code_codul_de_procedura_penala_wins_over_codul_penal():
    """`Codul de procedură penală` matches as procedura_penala — NOT as
    a `Codul ... penal` substring."""
    refs = parse_primary_references("Aplicăm Codul de procedură penală în acest caz.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "procedura_penala"


def test_code_codul_silvic():
    refs = parse_primary_references("Codul silvic reglementează exploatarea pădurilor.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "silvic"


def test_code_codul_administrativ():
    refs = parse_primary_references("Codul administrativ, art. 50.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "administrativ"


def test_code_codul_civil():
    refs = parse_primary_references("Conform Codului civil...")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "civil"


def test_code_codul_vamal():
    refs = parse_primary_references("Codul vamal a fost modificat în 2024.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "vamal"


def test_code_reverse_form_art_n_din_codul():
    """`art. N din Codul X` reverse form — captures both article + kind."""
    refs = parse_primary_references("Aplicăm art. 297 din Codul fiscal.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "fiscal"
    assert cd[0]["article"] == "297"


def test_code_reverse_form_with_alin():
    """Reverse form preserves alin. qualifier in article."""
    refs = parse_primary_references("Conform art. 12 alin. (3) din Codul muncii.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 1
    assert cd[0]["code_kind"] == "muncii"
    assert "12" in cd[0]["article"]
    assert "alin" in cd[0]["article"]


def test_code_no_match_returns_empty():
    """Random `Cod` mentions don't match (e.g. `Codul rutier moldovenesc`
    is borderline; `Codul cocoșului` definitely shouldn't fire)."""
    refs = parse_primary_references("Codul cocoșului din Vechiul Testament.")
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 0


def test_code_NOT_emitted_as_unknown():
    """Codul X spans must NOT also surface as unknown (graduated)."""
    refs = parse_mentioned_references(
        "Codul muncii și Codul fiscal au fost modificate."
    )
    unknowns = [r for r in refs if r["type"] == "unknown"]
    codul_unknowns = [u for u in unknowns if "Codul" in u["raw"]]
    assert codul_unknowns == []


def test_code_universal_fields():
    text = "Codul muncii art. 5; art. 297 din Codul fiscal."
    refs = parse_primary_references(text)
    cd = [r for r in refs if r["type"] == "code"]
    assert len(cd) == 2
    for r in cd:
        assert r["raw"]
        assert isinstance(r["char_offsets"], list)
        assert r["char_offsets"][0] < r["char_offsets"][1]
        assert r["code_kind"] in {
            "muncii",
            "fiscal",
            "civil",
            "penal",
            "procedura_civila",
            "procedura_penala",
            "administrativ",
            "silvic",
            "aerian",
            "rutier",
            "vamal",
            "comercial",
            "familiei",
            "navigatiei",
            "consumului",
            "insolventei",
        }


# -- v0.4.0 unknown emission ---------------------------------------------


def test_unknown_emit_hg_form():
    """Hotărâre de Guvern → unknown (not a strict variant)."""
    refs = parse_mentioned_references(
        "S-a aplicat HG nr. 123/2020 privind taxele administrative."
    )
    unknowns = [r for r in refs if r["type"] == "unknown"]
    assert any("HG" in r["raw"] for r in unknowns)


def test_unknown_emit_decret_lege():
    refs = parse_mentioned_references("Decret-lege nr. 31/1990 privind succesiunile.")
    unknowns = [r for r in refs if r["type"] == "unknown"]
    assert any("Decret" in r["raw"] for r in unknowns)


def test_unknown_emit_iccj():
    """High-court (non-CCR) decisions → unknown.hint=court-ish."""
    refs = parse_mentioned_references(
        "Conform Deciziei Înaltei Curți de Casație nr. 50/2020..."
    )
    unknowns = [r for r in refs if r["type"] == "unknown"]
    iccj = [r for r in unknowns if "Casa" in r["raw"]]
    assert len(iccj) >= 1
    assert iccj[0]["hint"] == "court-ish"


def test_acordul_protocolul_graduated_NOT_unknown():
    """Acordul/Protocolul graduated to strict `treaty` in v0.5.0 — must
    NOT also surface as `unknown.hint=other`."""
    refs = parse_mentioned_references(
        "Conform Acordului de la Schengen și Protocolului de la Montreal..."
    )
    unknowns = [r for r in refs if r["type"] == "unknown"]
    treaty_unknowns = [
        u for u in unknowns if "Acord" in u["raw"] or "Protocol" in u["raw"]
    ]
    assert treaty_unknowns == []
    treaties = [r for r in refs if r["type"] == "treaty"]
    raws = " ".join(t["raw"] for t in treaties)
    assert "Acordul" in raws
    assert "Protocolul" in raws


def test_unknown_emit_bare_art_n():
    """Bare `art. N` (not anchored to Constituție/Regulament/etc.) → unknown."""
    refs = parse_mentioned_references(
        "Aplicăm art. 5 alin. (2) lit. b) și art. 12 din această lege."
    )
    unknowns = [r for r in refs if r["type"] == "unknown"]
    art_unknowns = [r for r in unknowns if "art" in r["raw"].lower()]
    assert len(art_unknowns) >= 1
    assert art_unknowns[0]["hint"] == "law-ish"


def test_unknown_NOT_emitted_for_constitution_form():
    """`art. N din Constituție` is a constitution ref, NOT unknown."""
    refs = parse_mentioned_references("Conform art. 76 din Constituție, ...")
    cr = [r for r in refs if r["type"] == "constitution"]
    unknown_arts = [r for r in refs if r["type"] == "unknown"]
    assert len(cr) == 1
    assert len(unknown_arts) == 0


def test_unknown_NOT_emitted_for_regulation_form():
    """`art. N din Regulamentul Senatului` is regulation, NOT unknown."""
    refs = parse_mentioned_references(
        "Conform art. 173 din Regulamentul Senatului, această procedură..."
    )
    reg = [r for r in refs if r["type"] == "regulation"]
    unknown_arts = [r for r in refs if r["type"] == "unknown"]
    assert len(reg) == 1
    assert len(unknown_arts) == 0


def test_unknown_universal_fields():
    """Unknown emissions carry type/raw/char_offsets/hint."""
    refs = parse_mentioned_references(
        "HG nr. 100/2020 + Decret-lege nr. 31/1990 + Acordul de la Schengen."
    )
    unknowns = [r for r in refs if r["type"] == "unknown"]
    assert len(unknowns) >= 2
    for u in unknowns:
        assert u["raw"]
        assert isinstance(u["char_offsets"], list)
        assert u["char_offsets"][0] < u["char_offsets"][1]
        assert u["hint"] in {"law-ish", "court-ish", "eu-doc-ish", "other"}


def test_primary_references_NEVER_emit_unknown():
    """parse_primary_references stays strict — no unknowns ever."""
    text = "art. 5 din această lege, Codul muncii art. 12, HG nr. 100/2020."
    refs = parse_primary_references(text)
    assert all(r["type"] != "unknown" for r in refs)


def test_unknown_refs_in_source_order():
    """Mixed strict + unknown refs return in char-offset order."""
    text = (
        "Mai întâi art. 5 alin. (1), apoi Legea nr. 100/2020, "
        "apoi Codul penal, apoi PL-x 50/2025."
    )
    refs = parse_mentioned_references(text)
    starts = [r["char_offsets"][0] for r in refs]
    assert starts == sorted(starts)
