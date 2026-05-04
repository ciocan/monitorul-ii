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
