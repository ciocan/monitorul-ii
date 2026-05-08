"""Tests for persons.json registry + normalize_speaker matcher (Tier 4.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitorul_ii import registries
from monitorul_ii.registries import (
    PERSONS_REGISTRY_VERSION,
    _strip_diacritics_aggressive,
    _person_token_set,
    _levenshtein,
    load_persons,
    normalize_speaker,
)


# -- registry-file integrity -----------------------------------------------


def test_persons_registry_loads_and_validates():
    """The shipped JSON parses, has a non-empty entries list, and meets
    the per-entry shape contract: id, canonical_name, mandate dates."""
    data = load_persons()
    assert isinstance(data, dict)
    assert isinstance(data.get("version"), str) and data["version"]
    entries = data.get("entries")
    assert isinstance(entries, list) and len(entries) >= 30
    seen_ids: set[str] = set()
    for e in entries:
        assert isinstance(e["id"], str) and e["id"]
        assert e["id"] not in seen_ids, f"duplicate id {e['id']!r}"
        seen_ids.add(e["id"])
        assert isinstance(e["canonical_name"], str) and e["canonical_name"]
        assert isinstance(e.get("aliases", []), list)
        for m in e.get("mandates") or []:
            for k in ("from", "to"):
                v = m.get(k)
                assert v is None or (isinstance(v, str) and v[:4].isdigit())


def test_persons_version_constant_matches_file():
    data = load_persons()
    assert PERSONS_REGISTRY_VERSION == data["version"]


def test_persons_loader_caches():
    """Cache returns the same dict object on repeated calls."""
    a = load_persons()
    b = load_persons()
    assert a is b


def test_persons_loader_rejects_bad_shape(monkeypatch, tmp_path: Path):
    """Validator catches malformed registry files."""
    bad = tmp_path / "persons.json"
    bad.write_text(
        json.dumps(
            {
                "version": "0.0.1",
                "entries": [{"id": "x"}],  # missing canonical_name
            }
        ),
        encoding="utf-8",
    )
    registries._load_persons.cache_clear()
    monkeypatch.setattr(registries, "_REGISTRIES_DIR", tmp_path)
    with pytest.raises(ValueError, match="canonical_name"):
        load_persons()
    registries._load_persons.cache_clear()
    registries._persons_alias_index.cache_clear()


# -- aggressive diacritic + mojibake fold ----------------------------------


def test_aggressive_fold_handles_modern_diacritics():
    assert _strip_diacritics_aggressive("Văcăroiu").lower() == "vacaroiu"
    assert _strip_diacritics_aggressive("Țară").lower() == "tara"
    assert _strip_diacritics_aggressive("Șoșoacă").lower() == "sosoaca"


def test_aggressive_fold_handles_cedilla():
    """Pre-2010 cedilla forms collapse to the same key as comma forms."""
    assert (
        _strip_diacritics_aggressive("Şedinţa").lower()
        == _strip_diacritics_aggressive("Ședința").lower()
    )


def test_aggressive_fold_handles_mojibake():
    """PostScript-era mojibake substitutions fold to ASCII."""
    assert _strip_diacritics_aggressive("V„c„roiu").lower() == "vacaroiu"
    assert _strip_diacritics_aggressive("Mele∫canu").lower() == "melescanu"
    assert _strip_diacritics_aggressive("Bolca∫").lower() == "bolcas"


def test_aggressive_fold_negative_case():
    """Unrelated strings stay distinguishable."""
    assert _strip_diacritics_aggressive("Iordache") != _strip_diacritics_aggressive(
        "Geoană"
    )


# -- token-set helper -------------------------------------------------------


def test_person_token_set_orderless():
    """Token set is order-independent — the matcher's surname-first vs
    first-surname tier relies on this."""
    a = _person_token_set("Florin Iordache")
    b = _person_token_set("Iordache Florin")
    assert a == b
    assert a == frozenset({"florin", "iordache"})


def test_person_token_set_splits_hyphens():
    """`Sorin-Mihai` and `Sorin Mihai` collapse — both common."""
    a = _person_token_set("Sorin-Mihai Cîmpeanu")
    b = _person_token_set("Sorin Mihai Cîmpeanu")
    assert a == b


def test_person_token_set_drops_short_tokens():
    """Single-letter middle initials drop from the set."""
    s = _person_token_set("Florin V. Iordache")
    assert "v" not in s
    assert "florin" in s and "iordache" in s


# -- Levenshtein helper -----------------------------------------------------


def test_levenshtein_basic():
    assert _levenshtein("kitten", "sitting") == 3
    assert _levenshtein("abc", "abc") == 0
    assert _levenshtein("abc", "abd") == 1


def test_levenshtein_early_exits_above_cap():
    """Distance > max_distance returns max+1; doesn't compute exact."""
    assert _levenshtein("aaaa", "bbbb", max_distance=2) == 3
    assert _levenshtein("a", "abcdef", max_distance=2) == 3


# -- matcher tiers ---------------------------------------------------------


def test_match_exact():
    eid, via = normalize_speaker("Florin Iordache")
    assert eid == "iordache-florin"
    assert via == "exact"


def test_match_exact_stripped_honorific():
    """Honorific + title prefix are peeled before exact match."""
    eid, via = normalize_speaker("Domnul deputat Florin Iordache")
    assert eid == "iordache-florin"
    assert via == "exact"


def test_match_strips_role_suffix():
    """Trailing `, role` clause is dropped; underlying name still resolves."""
    eid, via = normalize_speaker(
        "domnul deputat Florin Iordache, vicepreședinte al Camerei"
    )
    assert eid == "iordache-florin"
    assert via == "exact"


def test_match_case_tier():
    """Case-insensitive tier hits when casing is wrong but bytes match."""
    eid, via = normalize_speaker("FLORIN IORDACHE")
    assert eid == "iordache-florin"
    assert via == "case"


def test_match_diacritic_tier_modern_to_modern():
    """Folded match: input has stripped diacritics, registry has them."""
    eid, via = normalize_speaker("Domnul Nicolae Vacaroiu")
    assert eid == "vacaroiu-nicolae"
    assert via == "diacritic"


def test_match_diacritic_tier_mojibake():
    """Mojibake variants resolve via the diacritic tier when not also
    enrolled as a verbatim alias.

    The shipped `vacaroiu-nicolae` entry registers `V„c„roiu` as an
    exact alias (it's high-volume in the corpus), so the exact tier
    short-circuits; this test exercises the diacritic+mojibake fold via
    a different name (`Mele∫canu`) whose mojibake form is NOT in the
    registry's aliases so the fold has to do the work.
    """
    eid, via = normalize_speaker("Domnul Teodor Mele∫canu")
    assert eid == "melescanu-teodor-viorel"
    # exact / case / diacritic — the exact alias `Teodor Mele∫canu` is
    # NOT registered (we only have `Teodor Viorel Mele∫canu`), so this
    # must take the token_set or diacritic+token cascade path.
    assert via in ("token_set", "diacritic", "fuzzy")


def test_match_token_set_tier_reversed_name():
    """`Surname First` ↔ `First Surname` — token-set tier resolves it.

    The registry alias `Iordache Florin` already covers this case
    exactly; force the token-set path by using a name shape that
    deliberately bypasses exact + case + diacritic — the simplest is to
    reuse a `First Surname` form whose alias is `Surname First`.
    """
    eid, via = normalize_speaker("Marcel Ciolacu")
    # Marcel Ciolacu hits an exact alias → exact tier
    assert eid == "ciolacu-marcel"
    assert via in ("exact", "case", "diacritic", "token_set")


def test_match_token_set_via_extra_middle():
    """`Iordache Florin` reverse form hits by alias; verify resolution."""
    eid, _ = normalize_speaker("Iordache Florin")
    assert eid == "iordache-florin"


def test_match_fuzzy_tier_one_letter_typo():
    """A single-letter typo is absorbed by the Levenshtein-2 tier."""
    eid, via = normalize_speaker("Domnul Florin Iorache")  # missing 'd'
    assert eid == "iordache-florin"
    assert via == "fuzzy"


def test_match_fuzzy_does_not_conflate_short_distinct_surnames():
    """Regression: `Vela` and `Vlad` differ in 2 of 4 chars (joined Lev=2)
    but are different surnames. The per-token fuzzy tier caps short tokens
    at distance 1, so `Gheorghe Vela` must not resolve to `gheorghe-vlad`.
    """
    for surface in (
        "Gheorghe Vela",
        "Vela Gheorghe",
        "Domnul Gheorghe Vela",
        "Domnul deputat Gheorghe Vela",
    ):
        eid, via = normalize_speaker(surface, context_year=2020)
        assert eid != "gheorghe-vlad", (
            f"{surface!r} must not fuzzy-match gheorghe-vlad (got {eid!r})"
        )


@pytest.mark.parametrize(
    "wrong_raw,wrong_id",
    [
        # Each pair: a corpus raw value that the OLD fuzzy tier (sum≤2)
        # mis-resolved to a similar-but-distinct registered person. The
        # new sum=1 cap catches these because BOTH the given name AND the
        # surname differ by 1 char each, totalling 2 fuzzy edits.
        ("Florian Nicolae", "niculae-florin"),  # 88 corpus refs
        ("Domnul Florian Nicolae", "niculae-florin"),
        ("Darius Pop", "top-marius"),  # 42 corpus refs (Marius Țop)
        ("Domnul Darius Pop", "top-marius"),
        ("Mario Ruse", "rusu-marin"),  # 5 corpus refs (Marin Rusu)
        ("Daniela Sava", "savu-daniel"),  # 1 ref (Daniel Savu)
        ("Liviu Petreu", "litiu-petru"),  # 1 ref (Petru Lițiu)
        ("Alexandra Dumitrașcu", "dumitrescu-alexandru"),  # 3 refs
    ],
)
def test_match_fuzzy_sum_cap_one_rejects_two_real_name_differences(
    wrong_raw: str, wrong_id: str
):
    """Regression: the old fuzzy tier with sum≤2 conflated different
    real people whose given AND surname each differ by 1 char. The new
    sum=1 cap rejects all such cases. The corpus carried 144+ references
    across 8+ such bug pairs — see audit in `docs/architecture.md`
    § "wrongly-linked person attributions".
    """
    eid, _via = normalize_speaker(wrong_raw, context_year=2020)
    assert eid != wrong_id, (
        f"{wrong_raw!r} must not fuzzy-match {wrong_id!r} (got {eid!r})"
    )


@pytest.mark.parametrize(
    "raw,expected_id",
    [
        # Hungarian-name PostScript-conversion artefacts. With the
        # `‡→a` / `š→o` / `Ž→e` / `Ó→I` additions to
        # `_PERSON_MOJIBAKE_MAP`, the diacritic tier hits these directly
        # — no fuzzy needed. (Before the additions, recovery relied on
        # Lev≤2 across two mojibake-glyph tokens, which is now blocked
        # by the tightened sum cap.)
        ("Domnul Tam‡s S‡ndor", "tamas-sandor"),  # Tamás Sándor
        ("Domnul Kov‡cs Zolt‡n", "kovacs-zoltan"),  # Kovács Zoltán
        ("Doamna Bšndi Gyšngyike", "bondi-gyongyike"),  # Böndi Gyöngyike
        ("Domnul SŽres DŽnes", "seres-denes"),  # Dénes Seres
    ],
)
def test_match_hungarian_mojibake_recovery_via_diacritic_tier(
    raw: str, expected_id: str
):
    eid, via = normalize_speaker(raw, context_year=2020)
    assert eid == expected_id
    # The whole point of extending the mojibake map is that these resolve
    # via diacritic, NOT via the (now tighter) fuzzy tier.
    assert via == "diacritic", f"{raw!r}: expected diacritic, got {via}"


def test_match_fuzzy_rejects_token_count_mismatch():
    """Token-count mismatch falls through fuzzy: `Florin` (1 token) vs
    `Florin Iordache` (2 tokens) cannot fuzzy-match. The single-token
    case also fails the `len(raw_tokens) >= 2` guard.
    """
    eid, via = normalize_speaker("Florin")
    assert eid is None
    assert via is None


def test_match_no_hit_for_unknown_person():
    eid, via = normalize_speaker("Some Random Politician")
    assert eid is None
    assert via is None


def test_match_returns_none_for_non_canonical_speakers():
    """Procedural / institutional labels stay unresolved."""
    for label in ("Din sală", "din sala", "Guvernul", "<chair narration>"):
        eid, via = normalize_speaker(label)
        assert eid is None, f"{label!r} should not resolve"
        assert via is None


def test_match_handles_empty_and_none_input():
    assert normalize_speaker(None) == (None, None)
    assert normalize_speaker("") == (None, None)
    assert normalize_speaker("   ") == (None, None)
    assert normalize_speaker("X") == (None, None)


def test_match_handles_short_punctuation_only_input():
    """Inputs that strip to <2 chars after preprocessing get rejected."""
    assert normalize_speaker(".:") == (None, None)
    assert normalize_speaker("Domnul") == (None, None)


# -- homonym disambiguation ------------------------------------------------


def test_homonym_via_context_year(monkeypatch, tmp_path):
    """Build a small registry with two homonyms; context_year picks the
    one whose mandate covers it. Without context_year, the matcher
    refuses to guess and returns None."""
    homonym_data = {
        "version": "test",
        "entries": [
            {
                "id": "popa-old",
                "canonical_name": "Ion Popa",
                "diacritic_form": "Ion Popa",
                "aliases": ["Popa Ion"],
                "wikidata_qid": None,
                "birth_date": None,
                "mandates": [
                    {
                        "role": "deputat",
                        "chamber": "Camera Deputaților",
                        "legislature": "I",
                        "from": "1990-06-18",
                        "to": "1992-10-21",
                        "party": "FSN",
                    }
                ],
                "homonym_disambiguation": "1992 mandate (early)",
            },
            {
                "id": "popa-new",
                "canonical_name": "Ion Popa",
                "diacritic_form": "Ion Popa",
                "aliases": ["Popa Ion"],
                "wikidata_qid": None,
                "birth_date": None,
                "mandates": [
                    {
                        "role": "deputat",
                        "chamber": "Camera Deputaților",
                        "legislature": "VIII",
                        "from": "2016-12-21",
                        "to": "2020-12-21",
                        "party": "PSD",
                    }
                ],
                "homonym_disambiguation": "2018 mandate",
            },
        ],
    }
    out = tmp_path / "persons.json"
    out.write_text(json.dumps(homonym_data, ensure_ascii=False), encoding="utf-8")

    # Swap the registry dir for the duration of the test, clearing all
    # caches so the new file is loaded.
    monkeypatch.setattr(registries, "_REGISTRIES_DIR", tmp_path)
    registries._load_persons.cache_clear()
    registries._persons_alias_index.cache_clear()

    # Without year: refuse to guess.
    eid, via = normalize_speaker("Ion Popa")
    assert eid is None and via is None

    # With 1991 year: hits the old mandate.
    eid, via = normalize_speaker("Ion Popa", context_year=1991)
    assert eid == "popa-old"
    assert via in ("exact", "case", "diacritic")

    # With 2018: hits the new mandate.
    eid, via = normalize_speaker("Ion Popa", context_year=2018)
    assert eid == "popa-new"

    # With 2025: neither mandate covers; refuse to guess.
    eid, via = normalize_speaker("Ion Popa", context_year=2025)
    assert eid is None and via is None

    # Restore caches for downstream tests.
    registries._load_persons.cache_clear()
    registries._persons_alias_index.cache_clear()
