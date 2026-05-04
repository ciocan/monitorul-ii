from __future__ import annotations

from monitorul_ii.extraction.speakers import (
    SPEAKERS_VERSION,
    make_speaker,
    parse_questioner,
)


def test_speakers_version_format():
    parts = SPEAKERS_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_make_speaker_canonical_keys():
    s = make_speaker(raw="X", name="X")
    assert set(s.keys()) == {"raw", "name", "title", "role", "party_group", "person_id"}


def test_parse_questioner_modern_format_with_constituency():
    q = parse_questioner(
        "Patricia-Simina-Arina Moș, deputat PNL, Circumscripția electorală nr. 5 Bihor"
    )
    assert q["name"] == "Patricia-Simina-Arina Moș"
    assert q["title"] == "deputat"
    assert q["party_group"] == "PNL"


def test_parse_questioner_multi_word_party():
    q = parse_questioner(
        "Simona-Elena Macovei Ilie, deputat SOS România, Circumscripția electorală nr. 24 Iași"
    )
    assert q["name"] == "Simona-Elena Macovei Ilie"
    assert q["party_group"] == "SOS România"


def test_parse_questioner_2012_lowercase_party():
    q = parse_questioner(
        "Sorin Serioja Chivu, senator progresist, Circumscripția electorală nr. 31 Prahova"
    )
    assert q["name"] == "Sorin Serioja Chivu"
    assert q["title"] == "senator"
    assert q["party_group"] == "progresist"


def test_parse_questioner_no_constituency():
    q = parse_questioner("Adrian George Scutaru, deputat PNL")
    assert q["name"] == "Adrian George Scutaru"
    assert q["title"] == "deputat"
    assert q["party_group"] == "PNL"


def test_parse_questioner_falls_back_to_raw_only():
    q = parse_questioner("Just a name without a title")
    assert q["raw"] == "Just a name without a title"
    assert q["title"] is None
    # name still parsed from before-comma chunk if comma exists; otherwise falls back


def test_parse_questioner_empty_string_safe():
    q = parse_questioner("")
    assert q["raw"] == ""
    assert q["name"] is None
    assert q["title"] is None


def test_parse_questioner_strips_whitespace():
    q = parse_questioner("  Tudor   Ciuhodaru ,  deputat  PP-DD  ")
    assert q["name"] == "Tudor Ciuhodaru"
    assert q["title"] == "deputat"
    assert q["party_group"] == "PP-DD"
