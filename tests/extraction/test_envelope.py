from __future__ import annotations

from datetime import date

import pytest

from monitorul_ii.extraction.envelope import (
    EnvelopeMeta,
    document_id,
    envelope_meta_from_frontmatter,
    split_md,
)


def test_split_md_returns_frontmatter_and_body():
    text = (
        "---\n"
        'issue: "29"\n'
        "year: 2026\n"
        'part: "II"\n'
        "published: 2026-03-25\n"
        'chamber: "Camera Deputaților"\n'
        "---\n"
        "\nbody starts here\n"
    )
    fm, body = split_md(text)
    assert fm["issue"] == "29"
    assert fm["year"] == 2026
    assert fm["part"] == "II"
    assert fm["published"] == date(2026, 3, 25)
    assert fm["chamber"] == "Camera Deputaților"
    # split_md returns body bytes after the closing `---\n` — leading
    # whitespace inside the body is preserved as-is.
    assert "body starts here" in body
    assert body.startswith("\n") or body.startswith("body")


def test_split_md_no_frontmatter():
    text = "no frontmatter here\nbody only\n"
    fm, body = split_md(text)
    assert fm == {}
    assert body == text


def test_envelope_meta_requires_core_fields():
    with pytest.raises(ValueError):
        envelope_meta_from_frontmatter({})  # missing issue
    with pytest.raises(ValueError):
        envelope_meta_from_frontmatter({"issue": "29"})  # missing year


def test_envelope_meta_to_metadata_dict_session_type_inferred():
    meta = EnvelopeMeta(
        issue="29",
        year=2026,
        part="II",
        published=date(2026, 3, 25),
        session="SESIUNEA I ORDINARĂ – FEBRUARIE 2026",
    )
    md = meta.to_metadata_dict()
    assert md["session_type"] == "ordinary"
    assert md["chamber"] is None
    assert md["session_date"] is None


def test_envelope_meta_session_type_extraordinary():
    meta = EnvelopeMeta(
        issue="29",
        year=2026,
        part="II",
        published=date(2026, 3, 25),
        session="SESIUNEA EXTRAORDINARĂ – AUGUST 2024",
    )
    assert meta.to_metadata_dict()["session_type"] == "extraordinary"


def test_envelope_meta_session_type_unknown_when_empty():
    meta = EnvelopeMeta(issue="29", year=2026, part="II", published=date(2026, 3, 25))
    assert meta.to_metadata_dict()["session_type"] is None


def test_document_id_format():
    meta = EnvelopeMeta(issue="29", year=2026, part="II", published=date(2026, 3, 25))
    assert document_id(meta) == "mo://2026/II/29"


def test_document_id_alphanumeric_issue():
    meta = EnvelopeMeta(
        issue="358Bis", year=2024, part="II", published=date(2024, 4, 15)
    )
    assert document_id(meta) == "mo://2024/II/358Bis"
