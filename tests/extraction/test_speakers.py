from __future__ import annotations

from monitorul_ii.extraction.speakers import (
    SPEAKERS_VERSION,
    extract_delivery_mode,
    make_speaker,
    parse_honorific_speaker,
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


# --- v0.2.0 shared primitives ------------------------------------------------


def test_extract_delivery_mode_tribune():
    stripped, mode = extract_delivery_mode("Domnul X (de la tribună)")
    assert mode == "tribune"
    assert "(de la tribună)" not in stripped


def test_extract_delivery_mode_din_sala():
    _, mode = extract_delivery_mode("Domnul Y (din sală)")
    assert mode == "from_floor"


def test_extract_delivery_mode_audio_collapses_online():
    _, mode = extract_delivery_mode("Domnul Z (prin audioconferință)")
    assert mode == "online"


def test_extract_delivery_mode_video_collapses_online():
    _, mode = extract_delivery_mode("Doamna A (prin videoconferință)")
    assert mode == "online"


def test_extract_delivery_mode_balcony():
    _, mode = extract_delivery_mode("Domnul B (de la balcon)")
    assert mode == "from_balcony"


def test_extract_delivery_mode_written():
    _, mode = extract_delivery_mode("Domnul C (în scris)")
    assert mode == "written"


def test_extract_delivery_mode_no_match():
    stripped, mode = extract_delivery_mode("Domnul D")
    assert mode is None
    assert stripped == "Domnul D"


def test_parse_honorific_speaker_simple_domnul():
    s = parse_honorific_speaker("Domnul Sorin-Mihai Grindeanu")
    assert s["name"] == "Sorin-Mihai Grindeanu"
    assert s["title"] is None
    assert s["role"] is None


def test_parse_honorific_speaker_doamna():
    s = parse_honorific_speaker("Doamna Alina-Ștefania Gorghiu")
    assert s["name"] == "Alina-Ștefania Gorghiu"


def test_parse_honorific_speaker_with_role():
    s = parse_honorific_speaker("Domnul Mircea Abrudean, președintele Senatului")
    assert s["name"] == "Mircea Abrudean"
    assert s["role"] == "președintele Senatului"


def test_parse_honorific_speaker_with_rank_deputat():
    s = parse_honorific_speaker("Domnul deputat Daniel Grofu")
    assert s["name"] == "Daniel Grofu"
    assert s["title"] == "deputat"


def test_parse_honorific_speaker_with_rank_senator_and_role():
    s = parse_honorific_speaker(
        "Doamna senator Doina-Elena Federovici, secretar al Senatului"
    )
    assert s["name"] == "Doina-Elena Federovici"
    assert s["title"] == "senator"
    assert s["role"] == "secretar al Senatului"


def test_parse_honorific_speaker_strips_definite_article():
    """Romanian: deputatul → deputat, senatorul → senator."""
    s = parse_honorific_speaker("Domnul deputatul Test Person")
    assert s["title"] == "deputat"


def test_parse_honorific_speaker_empty_string():
    s = parse_honorific_speaker("")
    assert s["raw"] == ""
    assert s["name"] is None


def test_parse_honorific_speaker_no_honorific_falls_back():
    s = parse_honorific_speaker("Test Name, some role")
    assert s["name"] == "Test Name"
    assert s["role"] == "some role"
