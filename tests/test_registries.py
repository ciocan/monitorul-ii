"""Tests for the curated registries package (Tier 4 — task 4.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitorul_ii import registries
from monitorul_ii.registries import (
    INSTITUTIONAL_BODIES_REGISTRY_VERSION,
    MINISTRIES_REGISTRY_VERSION,
    _strip_diacritics,
    _tokenise,
    load_institutional_bodies,
    load_ministries,
    normalize_addressee,
    normalize_institutional_body,
    normalize_ministry,
)


# -- registry-file integrity ------------------------------------------------


def test_institutional_registry_loads_and_validates():
    """The shipped JSON parses, has a non-empty entries list, and meets
    the per-entry shape contract."""
    data = load_institutional_bodies()
    assert isinstance(data, dict)
    assert isinstance(data.get("version"), str) and data["version"]
    entries = data.get("entries")
    assert isinstance(entries, list) and len(entries) >= 15  # acceptance floor
    seen_ids: set[str] = set()
    for e in entries:
        assert isinstance(e["id"], str) and e["id"]
        assert e["id"] not in seen_ids, f"duplicate id {e['id']!r}"
        seen_ids.add(e["id"])
        assert isinstance(e["canonical_name"], str) and e["canonical_name"]
        assert isinstance(e.get("aliases", []), list)


def test_registry_version_constant_matches_file():
    data = load_institutional_bodies()
    assert INSTITUTIONAL_BODIES_REGISTRY_VERSION == data["version"]


def test_registry_loader_caches():
    """Cached so each subprocess re-parses the JSON at most once."""
    a = load_institutional_bodies()
    b = load_institutional_bodies()
    assert a is b


def test_registry_loader_rejects_bad_shape(monkeypatch, tmp_path: Path):
    """Validator catches malformed registry files."""
    bad = tmp_path / "institutional_bodies.json"
    bad.write_text(
        json.dumps({"version": "0.0.1", "entries": [{"id": "x"}]}),
        encoding="utf-8",
    )
    # Reach into the lazy cache: clear it, then point the loader at the
    # bad file.
    registries._load_institutional_bodies.cache_clear()
    monkeypatch.setattr(registries, "_REGISTRIES_DIR", tmp_path)
    with pytest.raises(ValueError, match="canonical_name"):
        load_institutional_bodies()
    # Reset for downstream tests
    registries._load_institutional_bodies.cache_clear()
    registries._institutional_alias_index.cache_clear()


# -- diacritic helper -------------------------------------------------------


def test_strip_diacritics_handles_modern_and_cedilla_forms():
    assert _strip_diacritics("Țară") == "Tara"
    assert _strip_diacritics("Ţară") == "Tara"  # cedilla form
    assert _strip_diacritics("Şedinţa") == "Sedinta"
    assert _strip_diacritics("ședință") == "sedinta"


def test_strip_diacritics_drops_replacement_chars():
    assert _strip_diacritics("piaţa� serviciilor") == "piata serviciilor"


def test_tokenise_orderless():
    a = _tokenise("Curtea de Conturi a României")
    b = _tokenise("Conturi de Curtea României a")
    assert a == b


# -- normalize match tiers --------------------------------------------------


def test_normalize_returns_none_for_empty_input():
    assert normalize_institutional_body(None) == (None, None)
    assert normalize_institutional_body("") == (None, None)
    assert normalize_institutional_body("   ") == (None, None)


def test_normalize_exact_canonical():
    """Exact match against a canonical_name field."""
    eid, via = normalize_institutional_body("Consiliul Suprem de Apărare a Țării")
    assert eid == "csat"
    assert via == "exact"


def test_normalize_exact_alias_genitive():
    """Genitive declensions are first-class aliases."""
    eid, via = normalize_institutional_body("Consiliului Suprem de Apărare a Țării")
    assert eid == "csat"
    assert via == "exact"


def test_normalize_exact_alias_acronym():
    eid, via = normalize_institutional_body("CSAT")
    assert eid == "csat"
    assert via == "exact"


def test_normalize_case_insensitive():
    eid, via = normalize_institutional_body("consiliul suprem de apărare a țării")
    assert eid == "csat"
    assert via == "case"


def test_normalize_diacritic_strip_modern_to_cedilla():
    """Cedilla-formatted raw matches comma-formatted alias via the
    diacritic-strip tier — mojibake / pre-2010 docs stay matched."""
    eid, via = normalize_institutional_body("Consiliului Suprem de Aparare a Tarii")
    assert eid == "csat"
    assert via == "diacritic"


def test_normalize_replacement_chars_do_not_crash_or_misclassify():
    """Replacement chars `�` in pre-2018 mojibake'd PDFs *delete the
    underlying letter* — ANCOM's `Națională` becomes `Naional` after
    stripping, which won't match by exact / case / diacritic.
    Token-set also fails because token spellings differ. The registry
    must return `(None, None)` on these, NOT match the wrong body —
    precision over recall is the contract."""
    eid, via = normalize_institutional_body(
        "Autoritatea Na�ional� pentru Administrare �i Reglementare �n Comunica�ii"
    )
    assert eid is None
    assert via is None


def test_normalize_token_set_handles_word_order():
    eid, via = normalize_institutional_body("Conturi de Curtea a României")
    assert eid == "curtea_de_conturi"
    assert via == "token_set"


def test_normalize_strips_trailing_artefacts():
    """The extractor sometimes leaves a stray `**` footnote marker on
    the raw value; the normaliser cleans it before alias lookup."""
    eid, via = normalize_institutional_body("Asociaţiei Pro Democraţia**")
    # Pro Democraţia isn't in the registry — but we must NOT crash and
    # we must NOT return a wrong match. Trailing `**` is also stripped
    # cheaply for entries that ARE in the registry.
    assert eid is None
    assert via is None
    eid, via = normalize_institutional_body("CSAT**")
    assert eid == "csat"
    assert via == "exact"


def test_normalize_returns_none_for_unknown_body():
    """Unknown institutions return (None, None) — never a wrong match."""
    eid, via = normalize_institutional_body("Comitetul pentru Lucruri Care Nu Există")
    assert eid is None
    assert via is None


def test_normalize_handles_br_artefact():
    """The converter occasionally leaves a literal `<br>` mid-string;
    the cleaner strips it."""
    eid, via = normalize_institutional_body(
        "Autoritatea Națională de Reglementare<br>în Domeniul Energiei"
    )
    assert eid == "anre"
    assert via in ("exact", "case", "diacritic", "token_set")


# -- ministry registry ------------------------------------------------------


def test_ministry_registry_loads_and_validates():
    data = load_ministries()
    entries = data.get("entries")
    assert isinstance(entries, list) and len(entries) >= 25  # ≥25 entries floor
    seen_ids: set[str] = set()
    for e in entries:
        assert isinstance(e["id"], str) and e["id"] not in seen_ids
        seen_ids.add(e["id"])


def test_ministry_registry_version_constant_matches_file():
    data = load_ministries()
    assert MINISTRIES_REGISTRY_VERSION == data["version"]


def test_normalize_ministry_exact():
    eid, via = normalize_ministry("Ministerul Sănătății")
    assert eid == "health"
    assert via == "exact"


def test_normalize_ministry_alias_historical():
    """Historical names collapse into the current ministry's id."""
    eid, via = normalize_ministry("Ministerul Sănătății Publice")
    assert eid == "health"
    assert via == "exact"


def test_normalize_ministry_alias_genitive():
    eid, via = normalize_ministry("Ministerului Sănătății")
    assert eid == "health"
    assert via == "exact"


def test_normalize_ministry_case_insensitive():
    eid, via = normalize_ministry("ministerul sănătății")
    assert eid == "health"
    assert via == "case"


def test_normalize_ministry_diacritic_strip():
    eid, via = normalize_ministry("Ministerul Sanatatii")
    assert eid == "health"
    assert via == "diacritic"


def test_normalize_ministry_token_set_floor_rejects_lone_token():
    """A single-token raw must NEVER token-set match a multi-token alias.

    `_tokenise('Ministerul')` → `{ministerul}`. Without the floor this
    set-equals every alias whose token set is also `{ministerul}` —
    none in our registry, but defensive coverage matters because some
    institutional aliases happen to be single tokens (`AGERPRES`,
    `CSAT`).
    """
    eid, via = normalize_ministry("Ministerul")
    assert eid is None
    assert via is None


def test_normalize_ministry_prefix_tier_recovers_sentence_bleed():
    """The plenary extractor sometimes bleeds sentence prose into
    `addressed_to` (e.g. `Ministerul Justiției a fost să modifice
    legislația...`). The prefix tier recovers the canonical id when
    the cleaned input starts with a registered alias at a token
    boundary."""
    eid, via = normalize_ministry(
        "Ministerul Justiției a fost să modifice legislația de așa natură"
    )
    assert eid == "justice"
    assert via == "prefix"


def test_normalize_ministry_prefix_picks_longest_match():
    """`Ministerul Apărării Naționale` registered alongside `Ministerul
    Apărării` — the longer alias must win when the input starts with
    the longer form."""
    eid, via = normalize_ministry(
        "Ministerul Apărării Naționale s-au îndreptat către anularea"
    )
    assert eid == "defense"
    assert via == "prefix"


def test_normalize_ministry_prefix_rejects_partial_word_match():
    """Match boundary must be a whitespace; `Ministerul nostru` doesn't
    start with a registered alias (no registered alias is bare
    `Ministerul`), so it falls through to no-match."""
    eid, via = normalize_ministry("Ministerul nostru")
    assert eid is None
    assert via is None


def test_normalize_ministry_returns_none_for_extractor_garbage():
    """The plenary extractor sometimes pulls a single letter into
    `addressed_to`. The registry must reject these — they would
    otherwise be forced into a bogus match by some token-set tier."""
    for noise in ("m", "p", "S", "I", "ABC"):
        eid, via = normalize_ministry(noise)
        assert eid is None, f"noise {noise!r} matched as {eid}"
        assert via is None


def test_normalize_ministry_returns_none_for_unknown():
    eid, via = normalize_ministry("Ministerul Pentru Lucruri Inventate")
    assert eid is None
    assert via is None


# -- composite addressee normalizer (ministry → institutional fallback) -----


def test_normalize_addressee_prefers_ministry():
    eid, via = normalize_addressee("Ministerul Sănătății")
    assert eid == "health"
    assert via == "exact"


def test_normalize_addressee_falls_back_to_institutional():
    """`Curtea de Conturi` isn't a ministry; the fallback resolves it."""
    eid, via = normalize_addressee("Curtea de Conturi a României")
    assert eid == "curtea_de_conturi"
    assert via == "exact"


def test_normalize_addressee_returns_none_when_neither_matches():
    eid, via = normalize_addressee("Ministerul nostru")
    assert eid is None
    assert via is None


def test_corpus_ministries_top_raws_all_match():
    """Top 30 ministry raws across qr + plenary corpus all resolve."""
    top_raws = [
        "Ministerul Sănătății",
        "Ministerul Culturii",
        "Ministerul Culturii și Identității Naționale",
        "Ministerul Transporturilor",
        "Ministerul Educației",
        "Ministerul Finanțelor Publice",
        "Ministerul Afacerilor Externe",
        "Ministerul Transporturilor și Infrastructurii",
        "Ministerul Mediului și Pădurilor",
        "Ministerul Economiei",
        "Ministerul Mediului și Schimbărilor Climatice",
        "Ministerul Energiei",
        "Ministerul Dezvoltării Regionale și Turismului",
        "Ministerul Mediului",
        "Ministerul Tineretului și Sportului",
        "Ministerul Agriculturii și Dezvoltării Rurale",
        "Ministerul Sănătății Publice",
        "Ministerul Comunicațiilor și Societății Informaționale",
        "Ministerul Dezvoltării Regionale și Administrației Publice",
        "Ministerul Muncii",
        "Ministerul Justiției",
        "Ministerul Educației Naționale",
        "Ministerul Fondurilor Europene",
        "Ministerul Economiei și Finanțelor",
        "Ministerul Turismului",
        "Ministerul Afacerilor Interne",
        "Ministerul Finanțelor",
        "Ministerul Familiei",
        "Ministerul Apărării",
        "Ministerul Apelor și Pădurilor",
    ]
    for raw in top_raws:
        eid, _via = normalize_ministry(raw)
        assert eid is not None, f"corpus raw {raw!r} unmatched"


def test_corpus_raws_all_match():
    """Every distinct raw value seen across the 52 R-suffix corpus docs
    must resolve to a canonical id. This is the regression guard for
    the registry's 90% production target.
    """
    corpus_raws = [
        "Consiliului Suprem de Apărare a Țării",
        "Agenției Naționale Anti-Doping",
        "Agenției Naționale de Presă AGERPRES",
        "Societății Române de Televiziune",
        "Serviciul Român de Informații",
        "Autorității Electorale Permanente",
        "Autorității Naționale de Reglementare în Domeniul Energiei",
        "Consiliul Legislativ",
        "Autoritatea Națională pentru Administrare și Reglementare în Comunicații",
    ]
    for raw in corpus_raws:
        eid, _via = normalize_institutional_body(raw)
        assert eid is not None, f"corpus raw {raw!r} unmatched"
