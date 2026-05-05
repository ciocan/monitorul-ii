from __future__ import annotations

from datetime import datetime, timezone

import pytest

from monitorul_ii.classifier import TYPED_DOCUMENT_TYPES
from monitorul_ii.extraction.schema import SchemaError, schema_dict, validate


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minimal_question_register_sidecar() -> dict:
    return {
        "schema_version": "1.12.0",
        "document_id": "mo://2026/II/29",
        "content_sha": "0123456789ab",
        "document_type": "question_register",
        "metadata": {
            "issue": "29",
            "year": 2026,
            "part": "II",
            "published": "2026-03-25",
            "chamber": "Camera Deputaților",
            "session": "SESIUNEA A II-A ORDINARĂ – SEPTEMBRIE–DECEMBRIE 2025",
            "session_type": "ordinary",
            "session_date": None,
            "legislature": "X",
        },
        "raw_markdown_path": "pdfs/x.md",
        "raw_pdf_path": "pdfs/x.pdf",
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": _now_iso(),
            "extractor_versions": {"question_register": "0.1.0"},
            "confidence": 0.9,
        },
        "coverage": {
            "body_chars": 100,
            "claimed_chars": 100,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
        "body": {
            "session_label": "SESIUNEA A II-A ORDINARĂ – SEPTEMBRIE–DECEMBRIE 2025",
            "chamber": "Camera Deputaților",
            "questions": [],
        },
    }


def test_schema_loads_and_lists_six_document_types():
    sd = schema_dict()
    enum = sd["$defs"]["DocumentType"]["enum"]
    expected = list(TYPED_DOCUMENT_TYPES) + ["other"]
    assert sorted(enum) == sorted(expected)


def test_validate_minimal_question_register_passes():
    validate(_minimal_question_register_sidecar())


def test_validate_rejects_unknown_top_level_key():
    sc = _minimal_question_register_sidecar()
    sc["bogus_field"] = 123
    with pytest.raises(SchemaError):
        validate(sc)


def test_validate_rejects_wrong_schema_version():
    sc = _minimal_question_register_sidecar()
    sc["schema_version"] = "1.9.0"
    with pytest.raises(SchemaError):
        validate(sc)


def test_validate_rejects_bad_document_id():
    sc = _minimal_question_register_sidecar()
    sc["document_id"] = "not-a-uri"
    with pytest.raises(SchemaError):
        validate(sc)


def test_validate_rejects_short_content_sha():
    sc = _minimal_question_register_sidecar()
    sc["content_sha"] = "abc"
    with pytest.raises(SchemaError):
        validate(sc)


def test_validate_rejects_invalid_chamber_enum():
    sc = _minimal_question_register_sidecar()
    sc["body"]["chamber"] = "Both"  # not in ["Camera Deputaților", "Senatul", null]
    with pytest.raises(SchemaError):
        validate(sc)


def test_unknown_reference_defines_resolved_to_field():
    """Schema 1.12.0 adds the additive `resolved_to` field on UnknownReference,
    populated by the cross-reference linker (xref_linker). The field's
    value is either a RefOffset object pointing at another reference's
    char_offsets, or null. Default null at extract time."""
    sd = schema_dict()
    unknown = sd["$defs"]["UnknownReference"]
    assert "resolved_to" in unknown["properties"]
    options = unknown["properties"]["resolved_to"]["anyOf"]
    has_ref = any(o.get("$ref") == "#/$defs/RefOffset" for o in options)
    has_null = any(o.get("type") == "null" for o in options)
    assert has_ref and has_null


def test_ref_offset_def_shape():
    """RefOffset is a minimal pointer: required `char_offsets` only."""
    sd = schema_dict()
    ref_offset = sd["$defs"]["RefOffset"]
    assert ref_offset["type"] == "object"
    assert ref_offset["additionalProperties"] is False
    assert ref_offset["required"] == ["char_offsets"]
    assert ref_offset["properties"]["char_offsets"]["$ref"] == "#/$defs/CharRange"


def test_unknown_reference_accepts_valid_resolved_to_pointer():
    """A populated resolved_to with the canonical RefOffset shape must
    validate cleanly against the UnknownReference $def."""
    from jsonschema import Draft202012Validator

    schema = schema_dict()
    v = Draft202012Validator(
        {**schema["$defs"]["UnknownReference"], "$defs": schema["$defs"]}
    )
    good = {
        "type": "unknown",
        "raw": "art. 25",
        "char_offsets": [0, 7],
        "hint": "law-ish",
        "resolved_to": {"char_offsets": [42, 60]},
    }
    assert list(v.iter_errors(good)) == []
    # Null is also acceptable
    null_form = {**good, "resolved_to": None}
    assert list(v.iter_errors(null_form)) == []
    # Field omitted entirely is acceptable (not required) — backwards-
    # compatible with pre-1.12.0 sidecars
    omitted = {k: v for k, v in good.items() if k != "resolved_to"}
    assert list(v.iter_errors(omitted)) == []


def test_unknown_reference_rejects_bad_resolved_to_shape():
    """A non-RefOffset-shape resolved_to (string, missing char_offsets,
    extra props) must fail validation against the UnknownReference $def."""
    from jsonschema import Draft202012Validator

    schema = schema_dict()
    v = Draft202012Validator(
        {**schema["$defs"]["UnknownReference"], "$defs": schema["$defs"]}
    )
    bad_string = {
        "type": "unknown",
        "raw": "art. 25",
        "char_offsets": [0, 7],
        "hint": "law-ish",
        "resolved_to": "not-an-object",
    }
    assert list(v.iter_errors(bad_string))
    bad_missing = {
        "type": "unknown",
        "raw": "art. 25",
        "char_offsets": [0, 7],
        "hint": "law-ish",
        "resolved_to": {},  # missing char_offsets
    }
    assert list(v.iter_errors(bad_missing))
    bad_extra = {
        "type": "unknown",
        "raw": "art. 25",
        "char_offsets": [0, 7],
        "hint": "law-ish",
        "resolved_to": {"char_offsets": [0, 7], "bogus": True},
    }
    assert list(v.iter_errors(bad_extra))


def test_pending_body_def_remains_permissive():
    """`PendingBody` is kept as a `$def` placeholder for future types that
    may need staged graduation. As of v1.11.0 the discriminator references
    it for no document_type — but the $def itself stays permissive
    (`additionalProperties: true`) so it's drop-in-ready when needed.
    """
    sd = schema_dict()
    pb = sd["$defs"]["PendingBody"]
    assert pb["type"] == "object"
    assert pb["additionalProperties"] is True
    # Sanity: the discriminator's oneOf doesn't currently point at PendingBody
    refs = [b.get("properties", {}).get("body", {}).get("$ref") for b in sd["oneOf"]]
    assert "#/$defs/PendingBody" not in refs


def test_validate_question_record_requires_all_fields():
    sc = _minimal_question_register_sidecar()
    sc["body"]["questions"] = [
        {
            "ordinal": 1,
            # addressee missing
            "questioner": {
                "raw": "x",
                "name": None,
                "title": None,
                "role": None,
                "party_group": None,
                "person_id": None,
            },
            "registration_number": None,
            "registration_date": None,
            "topic": None,
            "question_text": None,
            "source_span": {
                "chars": [0, 10],
                "lines": [1, 1],
                "content_sha": "0123456789ab",
            },
            "extraction": {
                "extractor": "x",
                "confidence": 1.0,
                "source_span": {
                    "chars": [0, 10],
                    "lines": [1, 1],
                    "content_sha": "0123456789ab",
                },
            },
        }
    ]
    with pytest.raises(SchemaError):
        validate(sc)
