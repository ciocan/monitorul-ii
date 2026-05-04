from __future__ import annotations

from datetime import date

from monitorul_ii.extraction.extractors.question_register import (
    EXTRACTOR_VERSION,
    _ADDRESSEE_HEADER_RE,
    _QUESTION_HEADER_RE,
    _clean_topic,
    _derive_ministry,
    _is_real_addressee,
    _parse_addressee,
    extract,
)
from monitorul_ii.extraction.envelope import EnvelopeMeta
from monitorul_ii.extraction.pipeline import ExtractContext


def test_extractor_version_format():
    parts = EXTRACTOR_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


# --- addressee parsing -------------------------------------------------------


def test_parse_personal_addressee_simple():
    a = _parse_addressee("Domnului Bogdan-Gruia Ivan, ministrul energiei")
    assert a["name"] == "Bogdan-Gruia Ivan"
    assert a["role"] == "ministrul energiei"
    assert a["ministry"] == "Ministerul Energiei"
    assert a["ministry_normalized"] is None


def test_parse_personal_addressee_strips_rank():
    a = _parse_addressee(
        "Domnului general Răzvan Ionescu, directorul interimar al Serviciului Român de Informații"
    )
    assert a["name"] == "Răzvan Ionescu"
    # "directorul al X" — al-pattern not handled by v0.1 ministry derivation
    assert a["role"].startswith("directorul")


def test_parse_personal_addressee_doamnei_form():
    a = _parse_addressee(
        "Doamnei Lucia Ana Varga, ministrul delegat pentru ape, păduri și piscicultură, Ministerul Mediului și Schimbărilor Climatice"
    )
    assert a["name"] == "Lucia Ana Varga"
    # The role text carries the parent ministry verbatim — explicit-Ministerul match wins
    assert a["ministry"] == "Ministerul Mediului și Schimbărilor Climatice"


def test_parse_institutional_addressee():
    a = _parse_addressee("Curții de Conturi")
    assert a["name"] is None
    assert a["role"] is None
    assert a["ministry"] == "Curții de Conturi"


def test_derive_ministry_genitive():
    assert _derive_ministry("ministrul energiei") == "Ministerul Energiei"
    assert (
        _derive_ministry("ministrul educației și cercetării")
        == "Ministerul Educației și Cercetării"
    )


def test_derive_ministry_explicit_ministerul_in_text_wins():
    role = "ministrul delegat pentru ape, Ministerul Mediului"
    assert _derive_ministry(role) == "Ministerul Mediului"


def test_derive_ministry_returns_none_for_non_ministry():
    assert _derive_ministry(None) is None
    assert _derive_ministry("") is None
    assert _derive_ministry("directorul interimar al SRI") is None


# --- header regex ------------------------------------------------------------


def test_question_header_modern_with_hash_prefix():
    line = "## 1. **Patricia-Simina-Arina Moș, deputat PNL, Circumscripția electorală nr. 5 Bihor**"
    m = _QUESTION_HEADER_RE.match(line)
    assert m is not None
    assert m.group("ord") == "1"
    assert "deputat" in m.group("inner")


def test_question_header_2012_no_hash_prefix():
    line = "1. **Sorin Serioja Chivu, senator progresist, Circumscripția electorală nr. 31 Prahova**"
    m = _QUESTION_HEADER_RE.match(line)
    assert m is not None
    assert m.group("ord") == "1"


def test_question_header_requires_deputat_or_senator():
    """Numbered bold list items in a question body must NOT match."""
    line = "1. **Some random bold text without a title**"
    m = _QUESTION_HEADER_RE.match(line)
    assert m is None


def test_addressee_header_personal():
    line = "## **Domnului Bogdan-Gruia Ivan, ministrul energiei**"
    m = _ADDRESSEE_HEADER_RE.match(line)
    assert m is not None
    assert "Bogdan-Gruia Ivan" in m.group("inner")


def test_addressee_header_institutional():
    line = "## **Curții de Conturi**"
    m = _ADDRESSEE_HEADER_RE.match(line)
    assert m is not None
    assert _is_real_addressee(m.group("inner"))


def test_addressee_blacklist_filters_chamber_heading():
    """`## **CAMERA DEPUTAȚILOR**` syntactically matches but must be
    filtered out — it's shared boilerplate, not an addressee."""
    line = "## **CAMERA DEPUTAȚILOR**"
    m = _ADDRESSEE_HEADER_RE.match(line)
    assert m is not None
    assert not _is_real_addressee(m.group("inner"))


def test_addressee_blacklist_filters_lista():
    line = "## **L I S T A**"
    m = _ADDRESSEE_HEADER_RE.match(line)
    assert m is not None
    assert not _is_real_addressee(m.group("inner"))


# --- topic trim --------------------------------------------------------------


def test_clean_topic_strips_trailing_salutation():
    raw = (
        "Finanțări și măsuri pentru creșterea rezilienței rețelelor de transport "
        "al energiei din județul Bihor Stimate domnule ministru,"
    )
    cleaned = _clean_topic(raw)
    assert "Stimate" not in cleaned
    assert cleaned.endswith("Bihor")


def test_clean_topic_passes_through_when_no_salutation():
    raw = "Subiect simplu fără salutare"
    assert _clean_topic(raw) == raw


# --- end-to-end extract on a synthetic minimal MD ---------------------------


def _make_ctx(body: str, *, chamber: str = "Camera Deputaților") -> ExtractContext:
    from monitorul_ii.extraction.coverage import line_offsets

    meta = EnvelopeMeta(
        issue="1",
        year=2026,
        part="II",
        published=date(2026, 1, 1),
        chamber=chamber,
        session="SESIUNEA TEST",
    )
    return ExtractContext(
        body_text=body,
        line_offsets=line_offsets(body),
        content_sha="0123456789ab",
        meta=meta,
        frontmatter={
            "chamber": chamber,
            "session": "SESIUNEA TEST",
            "issue": "1",
            "year": 2026,
            "part": "II",
            "published": meta.published,
        },
    )


def test_extract_synthetic_two_questions():
    body = (
        "## **Domnului Test Minister, ministrul testelor**\n"
        "\n"
        "## 1. **Alice Test, deputat PNL, Circumscripția electorală nr. 1 Bihor**\n"
        "\n"
        "Obiectul întrebării: prima întrebare de test\n"
        "\n"
        "Stimate domnule ministru,\n"
        "Body of question 1.\n"
        "\n"
        "_Alice Test_, deputat PNL\n"
        "\n"
        "Nr. 100A/01.01.2026\n"
        "\n"
        "## 2. **Bob Test, senator USR, Circumscripția electorală nr. 2 Cluj**\n"
        "\n"
        "Subiectul: a doua întrebare\n"
        "\n"
        "Body of question 2.\n"
        "\n"
        "_Bob Test_, senator USR\n"
        "\n"
        "Nr. 200B/15.02.2026\n"
    )
    ctx = _make_ctx(body)
    body_dict, claims = extract(ctx)
    assert body_dict["chamber"] == "Camera Deputaților"
    assert len(body_dict["questions"]) == 2

    q1, q2 = body_dict["questions"]
    assert q1["ordinal"] == 1
    assert q1["addressee"]["name"] == "Test Minister"
    assert q1["questioner"]["name"] == "Alice Test"
    assert q1["questioner"]["party_group"] == "PNL"
    assert q1["registration_number"] == "100A"
    assert q1["registration_date"] == "2026-01-01"
    assert q1["topic"] == "prima întrebare de test"

    assert q2["ordinal"] == 2
    assert q2["questioner"]["title"] == "senator"
    assert q2["registration_number"] == "200B"
    assert q2["registration_date"] == "2026-02-15"

    # All record claims have a source span; at least one boilerplate-claim
    # should be present (the qr-specific list header doesn't fire on this
    # synthetic body, but the LISTA word doesn't either — the synthetic
    # has none, which is fine. We only assert non-zero record claims.)
    assert any(c.kind == "record" for c in claims)


def test_extract_question_with_no_addressee_emits_empty_addressee():
    body = (
        "## 1. **Solo Test, deputat PNL**\n"
        "\nObiectul: standalone\n_Solo Test_, deputat PNL\n"
    )
    ctx = _make_ctx(body)
    body_dict, _ = extract(ctx)
    assert len(body_dict["questions"]) == 1
    a = body_dict["questions"][0]["addressee"]
    assert a["name"] is None
    assert a["ministry"] is None
    assert a["role"] is None


def test_extract_chamber_falls_back_to_none_when_unknown():
    body = "## 1. **X, deputat A**\nObiectul: y\n"
    ctx = _make_ctx(body, chamber="Some Other Chamber")
    body_dict, _ = extract(ctx)
    # Frontmatter chamber wasn't a recognised enum → body.chamber is None
    assert body_dict["chamber"] is None
