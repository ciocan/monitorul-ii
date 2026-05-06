"""Tests for tools.add_unresolved_speakers — the persons-registry stub
adder used to close the long-tail 15% of corpus speakers that don't
match any Wikidata-imported entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.add_unresolved_speakers import (
    _build_id,
    _fold_key,
    _is_junk_name,
    _is_non_canonical,
    _peel,
    _slugify,
    add_unresolved,
)


def _registry(entries: list[dict]) -> dict:
    return {"version": "0.1.0", "updated": "2026-05-06", "entries": entries}


def _write_speakers_raw(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolate_normalize_speaker(monkeypatch):
    """Stub `normalize_speaker` (which the script calls as a fuzzy-tier
    safety net against the real registry) to always miss. Tests then
    only see the script's fold-key index against the file passed in via
    `persons_path`.
    """
    monkeypatch.setattr(
        "tools.add_unresolved_speakers.normalize_speaker",
        lambda raw, **_kwargs: (None, None),
    )


# ---- _peel ---------------------------------------------------------------


def test_peel_strips_honorific():
    assert _peel("Domnul Florin Iordache") == "Florin Iordache"
    assert _peel("Doamna Raluca Turcan") == "Raluca Turcan"


def test_peel_strips_title_after_honorific():
    assert _peel("Domnul deputat Florin Iordache") == "Florin Iordache"


def test_peel_strips_trailing_role_clause():
    assert (
        _peel("Domnul deputat Florin Iordache, vicepreședinte al Camerei")
        == "Florin Iordache"
    )


def test_peel_handles_already_clean():
    assert _peel("Florin Iordache") == "Florin Iordache"


def test_peel_handles_empty():
    assert _peel("") == ""
    assert _peel("   ") == ""


# ---- _fold_key -----------------------------------------------------------


def test_fold_key_collapses_diacritics():
    """Two raws differing only by diacritics share a fold-key."""
    assert _fold_key("Văcăroiu") == _fold_key("Vacaroiu")


def test_fold_key_collapses_mojibake():
    """The aggressive fold collapses PostScript-era mojibake onto the
    same key as the modern form."""
    assert _fold_key("V„c„roiu") == _fold_key("Vacaroiu")


def test_fold_key_orderless():
    """`Florin Iordache` and `Iordache Florin` share a fold-key."""
    assert _fold_key("Florin Iordache") == _fold_key("Iordache Florin")


def test_fold_key_drops_short_tokens():
    """Tokens shorter than 2 chars are filtered out."""
    assert _fold_key("X Florin") == _fold_key("Florin")


# ---- _slugify ------------------------------------------------------------


def test_slugify_basic():
    assert _slugify("Klaus Iohannis") == "klaus-iohannis"


def test_slugify_strips_diacritics():
    assert _slugify("Vasile Văcăroiu") == "vasile-vacaroiu"


def test_slugify_handles_mojibake():
    assert _slugify("V„c„roiu") == "vacaroiu"


# ---- _build_id -----------------------------------------------------------


def test_build_id_surname_first():
    """Speaker.name is `Firstname Surname`; id is `surname-given`."""
    assert _build_id("Florin Iordache", set()) == "iordache-florin"


def test_build_id_collision_suffix():
    """A second cluster with the same kebab id gets `-2`."""
    existing = {"iordache-florin"}
    assert _build_id("Florin Iordache", existing) == "iordache-florin-2"


def test_build_id_third_collision():
    existing = {"iordache-florin", "iordache-florin-2"}
    assert _build_id("Florin Iordache", existing) == "iordache-florin-3"


def test_build_id_single_token_falls_back():
    """A single-token name kebabs the whole string."""
    assert _build_id("Bolcaș", set()) == "bolcas"


# ---- _is_non_canonical ---------------------------------------------------


def test_is_non_canonical_chair_narration():
    assert _is_non_canonical("<chair narration>")


def test_is_non_canonical_din_sala():
    assert _is_non_canonical("Din sală")
    assert _is_non_canonical("din sala")


def test_is_non_canonical_guvern():
    assert _is_non_canonical("Guvernul")


def test_is_non_canonical_negative():
    assert not _is_non_canonical("Florin Iordache")


# ---- _is_junk_name -------------------------------------------------------


def test_is_junk_name_too_short():
    assert _is_junk_name("Ax")


def test_is_junk_name_single_token():
    assert _is_junk_name("Iordache")


def test_is_junk_name_mostly_digits():
    assert _is_junk_name("12 34")


def test_is_junk_name_accepts_multi_token():
    assert not _is_junk_name("Florin Iordache")


# ---- add_unresolved (orchestration) --------------------------------------


def test_add_unresolved_mints_stub_for_unknown_speaker(
    tmp_path: Path, isolate_normalize_speaker
):
    """A cluster with no registry match yields a stub entry with the
    expected shape: id, canonical_name, diacritic_form, aliases (raw +
    name variants), wikidata_qid=null, mandates=[]."""
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(
        json.dumps(
            _registry(
                [
                    {
                        "id": "iohannis-klaus",
                        "canonical_name": "Klaus Iohannis",
                        "diacritic_form": "Klaus Iohannis",
                        "aliases": ["Klaus Iohannis"],
                        "wikidata_qid": "Q187920",
                        "birth_date": None,
                        "mandates": [],
                        "homonym_disambiguation": None,
                    }
                ]
            )
        ),
        encoding="utf-8",
    )

    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [
            {
                "raw": "Domnul deputat Ion Popescu",
                "name": "Ion Popescu",
                "count": 5,
            },
            {"raw": "Ion Popescu", "name": "Ion Popescu", "count": 3},
        ],
    )

    added, sk_resolved, sk_non_canon, sk_junk = add_unresolved(
        persons_path, speakers_raw
    )
    assert added == 1
    assert sk_resolved == 0
    assert sk_non_canon == 0
    assert sk_junk == 0

    data = json.loads(persons_path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 2
    new = next(e for e in data["entries"] if e["id"] != "iohannis-klaus")
    assert new["id"] == "popescu-ion"
    assert new["canonical_name"] == "Ion Popescu"
    assert new["wikidata_qid"] is None
    assert new["mandates"] == []
    # Aliases: the un-peeled raw form differs from canonical, so it goes
    # into aliases. We deliberately do NOT add the peeled form because it
    # would collide with existing canonical entries on the diacritic key
    # (see add_unresolved docstring).
    assert "Domnul deputat Ion Popescu" in new["aliases"]
    assert "Ion Popescu" not in new["aliases"]  # peeled form, redundant with canonical


def test_add_unresolved_skips_clusters_already_in_registry(
    tmp_path: Path, isolate_normalize_speaker
):
    """A cluster whose canonical_name fold-key matches an existing entry
    is skipped (not minted as a duplicate)."""
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(
        json.dumps(
            _registry(
                [
                    {
                        "id": "iordache-florin",
                        "canonical_name": "Florin Iordache",
                        "diacritic_form": "Florin Iordache",
                        "aliases": ["Iordache Florin"],
                        "wikidata_qid": "Q1234",
                        "birth_date": None,
                        "mandates": [],
                        "homonym_disambiguation": None,
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [{"raw": "Domnul Florin Iordache", "name": "Florin Iordache", "count": 10}],
    )

    added, sk_resolved, _, _ = add_unresolved(persons_path, speakers_raw)
    assert added == 0
    assert sk_resolved == 1
    data = json.loads(persons_path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 1


def test_add_unresolved_skips_non_canonical_labels(
    tmp_path: Path, isolate_normalize_speaker
):
    """`Din sală` / `Guvernul` / `<chair narration>` are denylisted —
    they're procedural labels, not people."""
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(
        json.dumps(
            _registry(
                [
                    {
                        "id": "seed",
                        "canonical_name": "Seed Person",
                        "diacritic_form": "Seed Person",
                        "aliases": [],
                        "wikidata_qid": None,
                        "birth_date": None,
                        "mandates": [],
                        "homonym_disambiguation": None,
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [
            {"raw": "Din sală", "name": "Din sală", "count": 200},
            {"raw": "Guvernul", "name": "Guvernul", "count": 50},
            {"raw": "<chair narration>", "name": "<chair narration>", "count": 30},
        ],
    )

    added, _, sk_non_canon, _ = add_unresolved(persons_path, speakers_raw)
    assert added == 0
    assert sk_non_canon == 3


def test_add_unresolved_skips_junk_names(tmp_path: Path, isolate_normalize_speaker):
    """Single-token / too-short / digit-heavy candidates are rejected."""
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(
        json.dumps(_registry([])),
        encoding="utf-8",
    )
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [
            {"raw": "Iordache", "name": "Iordache", "count": 5},  # single token
            {"raw": "Ax", "name": "Ax", "count": 5},  # too short
            {"raw": "12 34", "name": "12 34", "count": 5},  # mostly digits
        ],
    )

    added, _, _, sk_junk = add_unresolved(persons_path, speakers_raw)
    assert added == 0
    assert sk_junk == 3


def test_add_unresolved_collapses_diacritic_variants(
    tmp_path: Path, isolate_normalize_speaker
):
    """Two raws that differ only by diacritics / mojibake share a
    fold-key; the script should mint one stub, not two."""
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(
        json.dumps(_registry([])),
        encoding="utf-8",
    )
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [
            {"raw": "Vasile Văcăroiu", "name": "Vasile Văcăroiu", "count": 10},
            {"raw": "Vasile V„c„roiu", "name": "Vasile V„c„roiu", "count": 3},
        ],
    )

    added, _, _, _ = add_unresolved(persons_path, speakers_raw)
    assert added == 1


def test_add_unresolved_dry_run_does_not_write(
    tmp_path: Path, isolate_normalize_speaker
):
    """`write=False` keeps the registry file untouched."""
    persons_path = tmp_path / "persons.json"
    initial = _registry([])
    persons_path.write_text(json.dumps(initial), encoding="utf-8")
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [{"raw": "Ion Popescu", "name": "Ion Popescu", "count": 1}],
    )

    added, _, _, _ = add_unresolved(persons_path, speakers_raw, write=False)
    assert added == 1
    # File on disk is unchanged.
    after = json.loads(persons_path.read_text(encoding="utf-8"))
    assert after == initial


def test_add_unresolved_empty_speakers_raw(tmp_path: Path, isolate_normalize_speaker):
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(json.dumps(_registry([])), encoding="utf-8")
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    speakers_raw.write_text("", encoding="utf-8")

    added, sk_r, sk_nc, sk_j = add_unresolved(persons_path, speakers_raw)
    assert (added, sk_r, sk_nc, sk_j) == (0, 0, 0, 0)


def test_add_unresolved_skips_blank_lines_in_speakers_raw(
    tmp_path: Path, isolate_normalize_speaker
):
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(json.dumps(_registry([])), encoding="utf-8")
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    speakers_raw.write_text(
        "\n\n"
        + json.dumps({"raw": "Ion Popescu", "name": "Ion Popescu", "count": 1})
        + "\n\n",
        encoding="utf-8",
    )

    added, _, _, _ = add_unresolved(persons_path, speakers_raw)
    assert added == 1


def test_add_unresolved_handles_malformed_jsonl_rows(
    tmp_path: Path, isolate_normalize_speaker
):
    """A corrupt JSONL row is skipped; valid rows still process."""
    persons_path = tmp_path / "persons.json"
    persons_path.write_text(json.dumps(_registry([])), encoding="utf-8")
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    speakers_raw.write_text(
        "{ not json }\n"
        + json.dumps({"raw": "Ion Popescu", "name": "Ion Popescu", "count": 1})
        + "\n",
        encoding="utf-8",
    )

    added, _, _, _ = add_unresolved(persons_path, speakers_raw)
    assert added == 1


def test_add_unresolved_polluted_cluster_does_not_break_canonical_match(
    tmp_path: Path, monkeypatch, isolate_normalize_speaker
):
    """Regression: a cluster keyed on a polluted form (e.g. `Domnule X
    Y`) must NOT add the peeled `X Y` as an alias on the new stub —
    that would collide with an existing canonical `X Y` entry on the
    matcher's diacritic key, and the matcher would refuse to resolve
    BOTH the polluted and the clean form.
    """
    from monitorul_ii import registries as reg

    persons_path = tmp_path / "persons.json"
    persons_path.write_text(
        json.dumps(
            _registry(
                [
                    {
                        "id": "vacaroiu-nicolae",
                        "canonical_name": "Nicolae Văcăroiu",
                        "diacritic_form": "Nicolae Văcăroiu",
                        "aliases": ["Văcăroiu Nicolae"],
                        "wikidata_qid": "Q126671",
                        "birth_date": None,
                        "mandates": [],
                        "homonym_disambiguation": None,
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [
            {
                "raw": "Domnule Nicolae Văcăroiu",
                "name": "Domnule Nicolae Văcăroiu",
                "count": 5,
            },
        ],
    )
    add_unresolved(persons_path, speakers_raw)

    # Point the matcher at the temp registry so we can verify the clean
    # form still resolves to the canonical entry.
    monkeypatch.setattr(reg, "_REGISTRIES_DIR", tmp_path)
    reg._load_persons.cache_clear()
    reg._persons_alias_index.cache_clear()

    # Clean form must still resolve to the canonical entry.
    eid, _via = reg.normalize_speaker("Nicolae Văcăroiu")
    assert eid == "vacaroiu-nicolae", (
        "polluted stub leaked a clean alias and now homonym-collides with canonical"
    )

    # Polluted form resolves to the new stub.
    eid_pol, _via_pol = reg.normalize_speaker("Domnule Nicolae Văcăroiu")
    assert eid_pol is not None and eid_pol != "vacaroiu-nicolae"


def test_add_unresolved_stub_round_trips_through_normalize_speaker(
    tmp_path: Path, monkeypatch, isolate_normalize_speaker
):
    """A minted stub must resolve back to its own person_id when the
    matcher's persons.json points at the merged file."""
    from monitorul_ii import registries as reg

    persons_path = tmp_path / "persons.json"
    persons_path.write_text(json.dumps(_registry([])), encoding="utf-8")
    speakers_raw = tmp_path / "speakers_raw.jsonl"
    _write_speakers_raw(
        speakers_raw,
        [{"raw": "Ion Popescu", "name": "Ion Popescu", "count": 1}],
    )

    add_unresolved(persons_path, speakers_raw)

    # Point the matcher's loader at the temp registry and clear caches so
    # the new entries are visible.
    monkeypatch.setattr(reg, "_REGISTRIES_DIR", tmp_path)
    reg._load_persons.cache_clear()
    reg._persons_alias_index.cache_clear()

    eid, via = reg.normalize_speaker("Ion Popescu")
    assert eid == "popescu-ion"
    assert via == "exact"
