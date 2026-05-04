from __future__ import annotations

from monitorul_ii.extraction.schema import schema_dict
from monitorul_ii.extraction.topics import (
    PRIMARY_TOPICS,
    TOPICS_VERSION,
    detect_primary_topics,
    make_topics,
)


def test_topics_version_format():
    parts = TOPICS_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_primary_topics_align_with_schema_enum():
    """The 15 canonical primary topics must equal the schema's enum."""
    sd = schema_dict()
    schema_enum = sd["$defs"]["Topics"]["properties"]["primary"]["items"]["enum"]
    assert sorted(schema_enum) == sorted(PRIMARY_TOPICS)


def test_primary_topics_count_is_15():
    assert len(PRIMARY_TOPICS) == 15


def test_no_match_returns_empty_list():
    assert detect_primary_topics("Punctul 5 din ordinea de zi") == []


def test_empty_input_returns_empty_list():
    assert detect_primary_topics("") == []


def test_education_keyword_match():
    assert "Educație" in detect_primary_topics(
        "Proiectul de lege privind învățământul preuniversitar"
    )


def test_education_committee_name_match():
    assert "Educație" in detect_primary_topics(
        "Raportul Comisiei pentru învățământ asupra Pl-x 123/2026"
    )


def test_health_keyword_match():
    assert "Sănătate" in detect_primary_topics("Modificarea Legii spitalelor publice")


def test_transport_keyword_match():
    assert "Transporturi" in detect_primary_topics(
        "Proiectul de lege privind drumurile naționale și autostrăzile"
    )


def test_budget_keyword_match():
    assert "Buget-finanțe" in detect_primary_topics(
        "Aprobarea Codului fiscal pentru anul 2026"
    )


def test_eu_match():
    topics = detect_primary_topics(
        "Examinarea Comunicării Comisiei Europene COM(2025) 100"
    )
    assert "Afaceri europene" in topics


def test_multi_match_returns_full_list():
    """A title that hits multiple topics returns all of them."""
    topics = detect_primary_topics("Modificarea Codului fiscal pentru sectorul medical")
    assert "Buget-finanțe" in topics
    assert "Sănătate" in topics


def test_make_topics_returns_canonical_shape():
    t = make_topics("Proiectul de lege privind învățământul preuniversitar")
    assert sorted(t.keys()) == ["primary", "secondary"]
    assert "Educație" in t["primary"]
    assert t["secondary"] == []


def test_make_topics_handles_no_match():
    t = make_topics("Punct neutru fără cuvinte cheie")
    assert t == {"primary": [], "secondary": []}


def test_diacritic_variants_recognized():
    """Romanian texts sometimes drop diacritics; common substitutions work."""
    # Note: only patterns explicitly tolerant via [țt]/[ăa]/[șs]/[âa]
    assert "Educație" in detect_primary_topics("invatamant primar")
    assert "Justiție" in detect_primary_topics("Comisia juridica")


def test_defense_committee_name():
    assert "Apărare" in detect_primary_topics(
        "Comisia pentru apărare, ordine publică și siguranță națională"
    )


def test_environment_match():
    assert "Mediu" in detect_primary_topics(
        "Protecția mediului înconjurător și biodiversitate"
    )
