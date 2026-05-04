from __future__ import annotations

from datetime import datetime, timezone

import pytest

from monitorul_ii.classifier import TYPED_DOCUMENT_TYPES
from monitorul_ii.extraction.schema import SchemaError, schema_dict, validate


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minimal_question_register_sidecar() -> dict:
    return {
        "schema_version": "1.8.0",
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
    sc["schema_version"] = "1.7.0"
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


def test_pending_body_def_remains_permissive():
    """`PendingBody` is kept as a `$def` placeholder for future types that
    may need staged graduation. As of v1.8.0 the discriminator references
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
