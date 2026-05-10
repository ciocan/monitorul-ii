"""Denormaliser tests — sidecar dicts → per-grain ES doc shapes.

We assert against synthetic fixtures so the tests don't depend on
which sidecars are checked into the corpus. The mapping JSONs are the
authoritative shape contract — `_assert_doc_matches_mapping` walks the
mapping tree and asserts that every property the doc carries either
matches the mapping's declared type family or is null.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest

from monitorul_ii.elasticsearch import denormalize


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _plenary_sidecar() -> dict[str, Any]:
    """Minimal plenary_stenogram sidecar with one agenda item carrying
    one speech, one vote, and a few ref-mention shapes — enough to
    exercise every speech / vote / agenda field in the mapping.
    """
    return {
        "schema_version": "1.13.0",
        "document_id": "mo://2018/II/168",
        "content_sha": "820fc06c1cd1",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": "168",
            "year": 2018,
            "part": "II",
            "published": "2018-11-20",
            "chamber": "Camera Deputaților",
            "session_type": "ordinary",
            "session_date": "2018-11-13",
            "legislature": "VIII",
        },
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2026-05-06T12:05:02Z",
            "extractor_versions": {
                "boilerplate": "0.1.0",
                "plenary_stenogram": "0.2.9",
                "identity": "0.1.0",
            },
            "identity": {"record_id": "mo://2018/II/168"},
        },
        "coverage": {
            "body_chars": 79565,
            "claimed_chars": 79555,
            "claimed_pct": 0.999874,
            "gaps": [],
        },
        "body": {
            "session": {
                "chair": [{"raw": "Eugen Nicolicea", "person_id": "nicolicea-eugen"}],
                "secretaries": [],
                "format": "in_person",
                "outcome": "completed",
            },
            "agenda_items": [
                {
                    "id": "mo://2018/II/168#agenda-1",
                    "content_fingerprint": "6b5e48f21d33",
                    "slug": "ordinea-de-zi-mwd32kaa6u",
                    "ordinal": 1,
                    "title": "Aprobarea modificării ordinii de zi",
                    "category": "other",
                    "outcome": "votul_final_deferred",
                    "primary_references": [
                        {
                            "type": "bill",
                            "raw": "PL-x 542/2018",
                            "prefix": "PL-x",
                            "number": "542",
                            "year": 2018,
                        }
                    ],
                    "topics": {"primary": ["procedure"], "secondary": []},
                    "activities": [
                        {
                            "id": "mo://2018/II/168#agenda-1#act-1",
                            "content_fingerprint": "9c23a7599310",
                            "slug": "doamnelor-domnilor-jc5ej4l4x4",
                            "type": "speech",
                            "speaker": {
                                "raw": "Domnul Eugen Nicolicea",
                                "name": "Eugen Nicolicea",
                                "person_id": "nicolicea-eugen",
                                "party_group": "PSD",
                                "title": None,
                                "role": None,
                            },
                            "delivery_mode": None,
                            "text": "## Doamnelor și domnilor,\n\n"
                            + "Aceasta este o intervenție substanțială cu mai multe "
                            + "puncte de discuție pentru a depăși pragul de 100 de caractere.",
                            "references_mentioned": [
                                {
                                    "type": "bill",
                                    "raw": "PL-x 542/2018",
                                    "prefix": "PL-x",
                                    "number": "542",
                                    "year": 2018,
                                },
                                {
                                    "type": "law",
                                    "raw": "Legea nr. 24/2000",
                                    "number": "24",
                                    "year": 2000,
                                },
                            ],
                            "source_span": {
                                "chars": [3048, 3897],
                                "lines": [28, 41],
                                "content_sha": "820fc06c1cd1",
                            },
                            "extraction": {"confidence": 0.85},
                        },
                        {
                            "id": "mo://2018/II/168#agenda-1#vote-1",
                            "content_fingerprint": "82fa46b69479",
                            "slug": "supun-votului-jc5ej4l4x4",
                            "type": "vote",
                            "motion_text": "Vă rog să votați.",
                            "motion_type": "procedural",
                            "voting_method": None,
                            "counts": {
                                "for": 132,
                                "against": 5,
                                "abstain": 2,
                                "not_voting": None,
                                "total_voting": 139,
                            },
                            "outcome": "approved",
                            "proposed_by": None,
                            "defers_to": None,
                            "resolves": [],
                            "source_span": {"chars": [4112, 4226], "lines": [50, 52]},
                        },
                        {
                            "id": "mo://2018/II/168#agenda-1#act-2",
                            "content_fingerprint": "deadbeef0001",
                            "slug": "scurta-ai9k0",
                            "type": "speech",
                            "speaker": {
                                "raw": "Domnul Iordache",
                                "name": "Florin Iordache",
                                "person_id": "iordache-florin",
                            },
                            "text": "Mulțumesc.",  # < 100 chars → not substantive
                            "references_mentioned": [],
                            "source_span": {"chars": [4226, 4240], "lines": [52, 53]},
                        },
                    ],
                    "source_span": {"chars": [0, 4240], "lines": [1, 53]},
                    "extraction": {"confidence": 0.85},
                },
            ],
            "interpellations": [],
        },
    }


def _question_register_sidecar() -> dict[str, Any]:
    return {
        "schema_version": "1.13.0",
        "document_id": "mo://2007/II/107",
        "content_sha": "e75a0080edf2",
        "document_type": "question_register",
        "metadata": {
            "issue": "107",
            "year": 2007,
            "part": "II",
            "published": "2007-07-11",
            "chamber": "Senatul",
        },
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2026-05-06T12:05:02Z",
            "extractor_versions": {"question_register": "0.2.0"},
            "identity": {"record_id": "mo://2007/II/107"},
        },
        "coverage": {"claimed_pct": 0.95, "body_chars": 1000},
        "body": {
            "session_label": "iulie 2007",
            "chamber": "Senatul",
            "questions": [
                {
                    "id": "mo://2007/II/107#q-570",
                    "content_fingerprint": "e50052da8a4d",
                    "slug": "continutul-intrebarii-aw7hk57j44",
                    "ordinal": 1,
                    "addressee": {
                        "ministry": "Ministerul Guvernului României",
                        "ministry_normalized": None,
                        "name": "Călin Popescu Tăriceanu",
                        "role": "prim-ministru",
                    },
                    "questioner": {
                        "raw": "Gheorghe Viorel Dumitrescu",
                        "name": "Gheorghe Viorel Dumitrescu",
                        "title": "senator",
                        "party_group": "P.R.M.",
                        "person_id": "dumitrescu-viorel",
                    },
                    "registration_number": "570",
                    "registration_date": None,
                    "topic": None,
                    "question_text": "Conținutul întrebării: ...",
                    "source_span": {"chars": [397, 1428], "lines": [17, 38]},
                }
            ],
        },
    }


def _committee_sidecar() -> dict[str, Any]:
    return {
        "schema_version": "1.13.0",
        "document_id": "mo://2017/II/27c",
        "content_sha": "abcd1234abcd",
        "document_type": "committee_synthesis",
        "metadata": {
            "issue": "27c",
            "year": 2017,
            "part": "II",
            "published": "2017-10-12",
            "chamber": "Camera Deputaților",
        },
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2026-05-06T12:05:02Z",
            "extractor_versions": {"committee_synthesis": "0.2.0"},
            "identity": {"record_id": "mo://2017/II/27c"},
        },
        "coverage": {"claimed_pct": 0.99, "body_chars": 5000},
        "body": {
            "period": {"from": "2017-09-13", "to": "2017-09-14"},
            "committees": [
                {
                    "name": "Comisia pentru buget",
                    "kind": "permanent",
                    "joint_with": [],
                    "signatures": {
                        "president": {"name": "X Y", "person_id": "x-y"},
                        "secretary": {"name": "A B", "person_id": "a-b"},
                    },
                    "meetings": [
                        {
                            "id": "mo://2017/II/27c#cmt-buget-1",
                            "content_fingerprint": "fingerprint1",
                            "slug": "comisia-buget-4h72gq6jxy",
                            "dates": ["2017-09-13", "2017-09-14"],
                            "format": "in_person",
                            "purpose": "Aprobarea bugetului",
                            "joint_with": [],
                            "agenda": [
                                {
                                    "id": "mo://2017/II/27c#cmt-buget-1#item-1",
                                    "ordinal": 1,
                                    "title": "Examinare bilanț",
                                    "primary_references": [],
                                    "vote_summary": {"outcome": "approved"},
                                }
                            ],
                            "roster": [
                                {
                                    "speaker": {
                                        "raw": "Ioana Bran",
                                        "name": "Ioana Bran",
                                        "party_group": "PSD",
                                        "person_id": "bran-ioana",
                                    },
                                    "mode": "physical",
                                    "intra_committee_role": None,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }


def _report_sidecar() -> dict[str, Any]:
    return {
        "schema_version": "1.13.0",
        "document_id": "mo://2014/II/300R",
        "content_sha": "rep1234567890",
        "document_type": "report_facsimile",
        "metadata": {
            "issue": "300R",
            "year": 2014,
            "part": "II",
            "published": "2014-12-15",
            "chamber": "Parlamentul",
        },
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2026-05-06T12:05:02Z",
            "extractor_versions": {"report_facsimile": "0.2.1"},
            "identity": {"record_id": "mo://2014/II/300R"},
        },
        "coverage": {"claimed_pct": 0.99, "body_chars": 50000},
        "body": {
            "report": {
                "id": "mo://2014/II/300R",
                "content_fingerprint": "rep1234567890",
                "slug": "raport-csat-2013",
                "title": "Raportul CSAT pe anul 2013",
                "issuing_body": "Consiliul Suprem de Apărare a Țării",
                "issuing_body_normalized": "csat",
                "reporting_period": {"from": "2013-01-01", "to": "2013-12-31"},
                "received_at": {
                    "session_date": "2014-12-10",
                    "session_kind": "joint",
                    "received_in_document": None,
                },
            },
            "headings": [
                {"level": 1, "text": "Introducere"},
                {"level": 2, "text": "Activitatea Consiliului"},
            ],
        },
    }


# ----------------------------------------------------------------------
# Mapping-driven shape assertions
# ----------------------------------------------------------------------


def _load_mapping(grain: str) -> dict[str, Any]:
    pkg = "monitorul_ii.elasticsearch.mappings"
    text = resources.files(pkg).joinpath(f"{grain}.json").read_text(encoding="utf-8")
    return json.loads(text)


_KIND_FOR = {
    "keyword": (str, type(None)),
    "text": (str, type(None)),
    "date": (str, int, type(None)),
    "boolean": (bool, type(None)),
    "byte": (int, type(None)),
    "short": (int, type(None)),
    "integer": (int, type(None)),
    "long": (int, type(None)),
    "float": (float, int, type(None)),
    "double": (float, int, type(None)),
    "dense_vector": (list, type(None)),
    "integer_range": (dict, type(None)),
}


def _walk_props(props: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a mapping `properties` dict into `{path: type}` entries."""
    out: dict[str, str] = {}
    for name, body in props.items():
        path = f"{prefix}.{name}" if prefix else name
        body_type = body.get("type")
        if body_type == "nested" or "properties" in body:
            sub = body.get("properties") or {}
            out.update(_walk_props(sub, path))
            continue
        out[path] = body_type or "object"
    return out


def _resolve(source: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Walk dotted path through nested dicts; return (present, value)."""
    cur: Any = source
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return False, None
    return True, cur


def _assert_doc_matches_mapping(grain: str, doc: dict[str, Any]) -> None:
    """For each declared mapping field that's present in the doc,
    assert the value's Python type is in the family allowed by the
    mapping's declared type. Missing fields are tolerated (ES handles
    sparse docs); extra fields would still index but are flagged.
    """
    mapping = _load_mapping(grain)
    template = (mapping.get("template") or mapping).get(
        "mappings", mapping.get("mappings")
    )
    props = (template or {}).get("properties", {})
    expected = _walk_props(props)
    source = doc["_source"]
    for path, kind in expected.items():
        present, value = _resolve(source, path)
        if not present:
            continue
        # Skip whole nested-object subtrees — we already walk into them.
        if kind in ("object",):
            continue
        if isinstance(value, list):
            allowed = _KIND_FOR.get(kind, (object,))
            for elem in value:
                assert elem is None or isinstance(elem, allowed), (
                    f"{grain}.{path}: list elem {elem!r} ({type(elem).__name__}) "
                    f"violates ES type {kind!r}"
                )
            continue
        allowed = _KIND_FOR.get(kind, (object,))
        assert value is None or isinstance(value, allowed), (
            f"{grain}.{path}: value {value!r} ({type(value).__name__}) "
            f"violates ES type {kind!r}"
        )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_documents_doc_basic_shape():
    sidecar = _plenary_sidecar()
    doc = denormalize.to_documents_doc(
        sidecar,
        s3_urls={
            "pdf": "s3://b/a.pdf",
            "md": "s3://b/a.md",
            "sidecar": "s3://b/a.json",
        },
    )
    assert doc["_index"] == "mo-documents"
    assert doc["_id"] == "mo://2018/II/168"
    src = doc["_source"]
    assert src["chamber"] == "Camera Deputaților"
    assert src["year"] == 2018
    assert src["agenda_count"] == 1
    assert src["speech_count"] == 2
    assert src["vote_count"] == 1
    assert src["url_path"] == "/mo/2018/II/168"
    assert src["s3_url_pdf"] == "s3://b/a.pdf"
    assert src["coverage"]["claimed_pct"] == pytest.approx(0.999874)
    _assert_doc_matches_mapping("mo-documents", doc)


def test_speeches_substantive_filter():
    sidecar = _plenary_sidecar()
    docs = denormalize.to_speeches_docs(sidecar)
    assert len(docs) == 2
    by_id = {d["_id"]: d["_source"] for d in docs}
    long = by_id["mo://2018/II/168#agenda-1#act-1"]
    short = by_id["mo://2018/II/168#agenda-1#act-2"]
    assert long["is_substantive"] is True
    assert short["is_substantive"] is False
    assert long["text_length"] >= 100
    assert short["text_length"] < 100
    # Refs flattened
    assert "PL-x:542/2018" in long["refs"]["bills"]
    assert "24/2000" in long["refs"]["laws"]
    assert long["url_path"] == "/discurs/doamnelor-domnilor-jc5ej4l4x4"
    _assert_doc_matches_mapping("mo-speeches", docs[0])


def test_votes_doc_shape():
    sidecar = _plenary_sidecar()
    docs = denormalize.to_votes_docs(sidecar)
    assert len(docs) == 1
    src = docs[0]["_source"]
    assert src["counts"]["for"] == 132
    assert src["counts"]["against"] == 5
    assert src["counts"]["for_unanimous"] is False
    assert src["outcome"] == "approved"
    assert src["url_path"].startswith("/vot/")
    _assert_doc_matches_mapping("mo-votes", docs[0])


def test_agenda_items_doc_shape():
    sidecar = _plenary_sidecar()
    docs = denormalize.to_agenda_items_docs(sidecar)
    assert len(docs) == 1
    src = docs[0]["_source"]
    assert src["category"] == "other"
    assert src["topics_primary"] == ["procedure"]
    assert "PL-x:542/2018" in src["refs"]["bills"]
    assert src["vote_summary"]["total_votes"] == 1
    assert "approved" in src["vote_summary"]["outcomes"]
    assert src["url_path"] == "/agenda/ordinea-de-zi-mwd32kaa6u"
    _assert_doc_matches_mapping("mo-agenda-items", docs[0])


def test_questions_doc_shape():
    sidecar = _question_register_sidecar()
    docs = denormalize.to_questions_docs(sidecar)
    assert len(docs) == 1
    src = docs[0]["_source"]
    assert src["regnum"] == "570"
    assert src["chamber"] == "Senatul"
    assert src["questioner"]["person_id"] == "dumitrescu-viorel"
    assert src["addressee"]["raw"] == "Ministerul Guvernului României"
    assert src["url_path"].startswith("/intrebare/")
    _assert_doc_matches_mapping("mo-questions", docs[0])


def test_committee_meeting_shape():
    sidecar = _committee_sidecar()
    docs = denormalize.to_committee_meetings_docs(sidecar)
    assert len(docs) == 1
    src = docs[0]["_source"]
    assert src["committee_kind"] == "permanent"
    assert src["committee_name"] == "Comisia pentru buget"
    assert src["meeting_date"] == "2017-09-13"
    assert src["agenda_items"][0]["title"] == "Examinare bilanț"
    assert src["roster"][0]["person_id"] == "bran-ioana"
    assert src["signatures"]["president_person_id"] == "x-y"
    _assert_doc_matches_mapping("mo-committee-meetings", docs[0])


def test_reports_shape():
    sidecar = _report_sidecar()
    docs = denormalize.to_reports_docs(sidecar)
    assert len(docs) == 1
    src = docs[0]["_source"]
    assert src["title"] == "Raportul CSAT pe anul 2013"
    assert src["issuing_body_normalized"] == "csat"
    assert src["reporting_period"]["from"] == "2013-01-01"
    assert len(src["headings"]) == 2
    assert src["headings"][0]["level"] == 1
    _assert_doc_matches_mapping("mo-reports", docs[0])


def test_persons_docs():
    persons = {
        "version": "0.1.0",
        "entries": [
            {
                "id": "iordache-florin",
                "canonical_name": "Florin Iordache",
                "diacritic_form": "Florin Iordache",
                "aliases": ["F. Iordache"],
                "wikidata_qid": "Q12728424",
                "birth_date": "1960-12-14",
                "mandates": [],
            }
        ],
    }
    docs = denormalize.to_persons_docs(persons)
    assert len(docs) == 1
    src = docs[0]["_source"]
    assert src["id"] == "iordache-florin"
    assert src["url_path"] == "/politicieni/iordache-florin"
    _assert_doc_matches_mapping("mo-persons", docs[0])


def test_dispatcher_plenary_full():
    sidecar = _plenary_sidecar()
    docs = denormalize.denormalize_sidecar(sidecar)
    grains = {d["_index"] for d in docs}
    assert "mo-documents" in grains
    assert "mo-agenda-items" in grains
    assert "mo-speeches" in grains
    assert "mo-votes" in grains
    # Plenary with empty interpellations[] doesn't emit interpellation docs.
    assert "mo-interpellations" not in grains


def test_dispatcher_grain_filter():
    sidecar = _plenary_sidecar()
    docs = denormalize.denormalize_sidecar(sidecar, grains=("mo-speeches",))
    assert all(d["_index"] == "mo-speeches" for d in docs)
    assert len(docs) == 2


def test_dispatcher_question_register_only_documents_and_questions():
    sidecar = _question_register_sidecar()
    docs = denormalize.denormalize_sidecar(sidecar)
    grains = {d["_index"] for d in docs}
    assert grains == {"mo-documents", "mo-questions"}


def test_dispatcher_committee_synthesis_only_documents_and_committees():
    sidecar = _committee_sidecar()
    docs = denormalize.denormalize_sidecar(sidecar)
    grains = {d["_index"] for d in docs}
    assert grains == {"mo-documents", "mo-committee-meetings"}


def test_dispatcher_report_facsimile_only_documents_and_reports():
    sidecar = _report_sidecar()
    docs = denormalize.denormalize_sidecar(sidecar)
    grains = {d["_index"] for d in docs}
    assert grains == {"mo-documents", "mo-reports"}


def test_child_record_ids_groups_by_grain():
    sidecar = _plenary_sidecar()
    docs = denormalize.denormalize_sidecar(sidecar)
    grouped = denormalize.child_record_ids(docs)
    assert "mo-speeches" in grouped
    assert len(grouped["mo-speeches"]) == 2
    assert grouped["mo-documents"] == ["mo://2018/II/168"]


def test_proposed_by_government_detection():
    sidecar = _plenary_sidecar()
    sidecar["body"]["agenda_items"][0]["activities"][1]["proposed_by"] = {
        "raw": "Guvernul",
        "name": "Guvernul",
        "person_id": "guvern",
    }
    docs = denormalize.to_votes_docs(sidecar)
    assert docs[0]["_source"]["proposed_by"]["is_government"] is True


def test_safe_date_rejects_calendar_impossible_dates():
    """Real corpus failure: committee extractor sometimes emits
    `2022-11-31` (November has 30 days) — ES's date parser would
    reject the whole bulk. The sanitiser drops to None.
    """
    from monitorul_ii.elasticsearch.denormalize import _safe_date

    # Valid forms pass through unchanged
    assert _safe_date("2022-11-30") == "2022-11-30"
    assert _safe_date("2022-11-30T10:00:00Z") == "2022-11-30T10:00:00Z"
    assert _safe_date("2022-11-30T10:00:00+00:00") == "2022-11-30T10:00:00+00:00"
    # Invalid calendar dates → None
    assert _safe_date("2022-11-31") is None
    assert _safe_date("2022-02-30") is None
    assert _safe_date("not a date") is None
    # Empty / None → None
    assert _safe_date(None) is None
    assert _safe_date("") is None
    # Non-string truthy passes through (caller's mapping decides)
    assert _safe_date(1234567890) == 1234567890


def test_committee_meeting_date_sanitised():
    """A committee meeting whose `dates[]` carries an invalid date
    must still produce an indexable doc — `meeting_date` falls to None.
    """
    sidecar = _committee_sidecar()
    sidecar["body"]["committees"][0]["meetings"][0]["dates"] = ["2022-11-31"]
    docs = denormalize.to_committee_meetings_docs(sidecar)
    assert len(docs) == 1
    assert docs[0]["_source"]["meeting_date"] is None


def test_speeches_carry_enrichments_when_present():
    sidecar = _plenary_sidecar()
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "topics": {
                "_meta": {"producer": "topics", "version": "0.1"},
                "topics": ["justitie", "guvernare"],
            },
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    long = by_id["mo://2018/II/168#agenda-1#act-1"]
    assert "justitie" in (long["enrichments"].get("topics") or [])
    assert long["enrichment_versions"]["topics"] == "0.1"


def test_speech_embedding_flattened_to_top_level_fields():
    """The embedding producer's nested `{"vector": [...], "text_fingerprint":
    "..."}` payload must flatten to ES `enrichments.embedding` (the
    dense_vector array) and `enrichments.embedding_text_fingerprint`
    (keyword) — siblings, not nested. The mapping is shaped that way so
    kNN can score directly off `enrichments.embedding`.
    """
    sidecar = _plenary_sidecar()
    vec = [0.001 + i * 1e-6 for i in range(1024)]
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "embedding": {
                "_meta": {
                    "producer": "bge-m3",
                    "version": "0.1",
                    "model_id": "BAAI/bge-m3",
                    "dims": 1024,
                },
                "vector": vec,
                "text_fingerprint": "abc123def456",
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    speech = by_id["mo://2018/II/168#agenda-1#act-1"]
    enrich = speech["enrichments"]
    # The dense_vector field is the raw list (NOT a dict).
    assert enrich["embedding"] == vec
    assert enrich["embedding_text_fingerprint"] == "abc123def456"


def test_speech_discourse_flattened_to_per_grain_shape():
    """The discourse producer's nested
    `{hawkins, voice, dqi, vparty, _meta, text_fingerprint}` payload
    must flatten to ES `enrichments.discourse.{hawkins,voice,dqi,vparty}.*`
    plus the sibling `discourse_producer` / `discourse_text_fingerprint`
    keywords. The Hawkins / V-Party `markers[]` collapse to
    `marker_count` + `marker_kinds[]`; the voice classifications
    collapse to `dominant_voice` + `voices_seen[]`; the DQI sub-codings
    flatten verbatim.
    """
    sidecar = _plenary_sidecar()
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {
                    "producer": "flash-lite",
                    "version": "0.1",
                    "model_id": "google/gemini-3.1-flash-lite",
                },
                "text_fingerprint": "abcdef012345",
                "hawkins": {
                    "score": 2,
                    "framework_confidence": 0.85,
                    "markers": [
                        {"kind": "people_vs_elite", "evidence": {"text": "x"}},
                        {"kind": "evil_elite", "evidence": {"text": "y"}},
                        {"kind": "people_vs_elite", "evidence": {"text": "z"}},
                    ],
                    "rationale": "...",
                },
                "voice": {
                    "classifications": [
                        {"marker_id": "m_0", "voice": "speaker_first_person"},
                        {"marker_id": "m_1", "voice": "speaker_first_person"},
                        {"marker_id": "m_2", "voice": "quoted"},
                    ]
                },
                "dqi": {
                    "level_of_justification": 2,
                    "content_of_justification": "common_good",
                    "respect_for_groups": 1,
                    "respect_for_demands": 1,
                    "respect_for_counterarguments": 0,
                    "constructive_politics": "alternative_proposal",
                },
                "vparty": {
                    "score": 1,
                    "framework_confidence": 0.7,
                    "markers": [
                        {"kind": "judiciary_attack", "evidence": {"text": "a"}},
                    ],
                },
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    speech = by_id["mo://2018/II/168#agenda-1#act-1"]
    enrich = speech["enrichments"]
    assert enrich["discourse_producer"] == "flash-lite"
    assert enrich["discourse_text_fingerprint"] == "abcdef012345"
    disc = enrich["discourse"]
    # Hawkins flatten
    assert disc["hawkins"]["score"] == 2
    assert disc["hawkins"]["framework_confidence"] == 0.85
    assert disc["hawkins"]["marker_count"] == 3
    # Dedup'd kind list, first-seen order
    assert disc["hawkins"]["marker_kinds"] == ["people_vs_elite", "evil_elite"]
    # Voice flatten — argmax (speaker_first_person, count=2)
    assert disc["voice"]["dominant_voice"] == "speaker_first_person"
    assert set(disc["voice"]["voices_seen"]) == {"speaker_first_person", "quoted"}
    # DQI sub-codings flat
    assert disc["dqi"]["level_of_justification"] == 2
    assert disc["dqi"]["content_of_justification"] == "common_good"
    assert disc["dqi"]["respect_for_groups"] == 1
    assert disc["dqi"]["respect_for_demands"] == 1
    assert disc["dqi"]["respect_for_counterarguments"] == 0
    assert disc["dqi"]["constructive_politics"] == "alternative_proposal"
    # V-Party flatten
    assert disc["vparty"]["score"] == 1
    assert disc["vparty"]["framework_confidence"] == 0.7
    assert disc["vparty"]["marker_count"] == 1
    assert disc["vparty"]["marker_kinds"] == ["judiciary_attack"]


def test_speech_discourse_handles_missing_payload():
    """When a record has no discourse enrichment, the per-grain `enrichments`
    block must NOT include any `discourse*` field — sparse-tolerant.
    """
    sidecar = _plenary_sidecar()
    docs = denormalize.to_speeches_docs(sidecar, enrichments=None)
    for d in docs:
        enrich = d["_source"]["enrichments"]
        assert "discourse" not in enrich
        assert "discourse_producer" not in enrich
        assert "discourse_text_fingerprint" not in enrich


def test_speech_discourse_handles_partial_voice():
    """voice can be null when Hawkins emits no markers; the flatten
    must skip the voice subkey rather than emit an empty placeholder.
    """
    sidecar = _plenary_sidecar()
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "ff00",
                "hawkins": {
                    "score": 0,
                    "framework_confidence": 0.95,
                    "markers": [],
                },
                "voice": None,
                "dqi": {
                    "level_of_justification": 1,
                    "content_of_justification": "group_interest",
                    "respect_for_groups": 1,
                    "respect_for_demands": 1,
                    "respect_for_counterarguments": 1,
                    "constructive_politics": "positional",
                },
                "vparty": {"score": 0, "framework_confidence": 0.9, "markers": []},
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    enrich = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]
    disc = enrich["discourse"]
    # Hawkins still flattens (score-only, no markers).
    assert disc["hawkins"]["score"] == 0
    assert disc["hawkins"]["marker_count"] == 0
    # voice block omitted entirely (None payload).
    assert "voice" not in disc
    # V-Party score-0 path: marker_count = 0, no marker_kinds.
    assert disc["vparty"]["marker_count"] == 0
    assert "marker_kinds" not in disc["vparty"]


def test_agenda_item_embedding_flatten_only_emits_known_keys():
    """The agenda mapping has `embedding` (dense_vector) but does NOT
    declare `embedding_text_fingerprint`. The flatten must respect the
    per-grain allowed set so we don't accidentally store the
    fingerprint where there's no mapping for it.
    """
    sidecar = _plenary_sidecar()
    vec = [0.0] * 1024
    enrichments = {
        "mo://2018/II/168#agenda-1": {
            "embedding": {"vector": vec, "text_fingerprint": "xx"},
        }
    }
    docs = denormalize.to_agenda_items_docs(sidecar, enrichments=enrichments)
    enrich = docs[0]["_source"]["enrichments"]
    assert enrich["embedding"] == vec
    # The agenda mapping has no `embedding_text_fingerprint` slot, so
    # the flatten must NOT emit it for that grain.
    assert "embedding_text_fingerprint" not in enrich


# ----------------------------------------------------------------------
# position_in_document — source-order key for the playback page (P4c+)
# ----------------------------------------------------------------------


def test_position_in_document_helper():
    """Defensive: missing or malformed source_span returns None."""
    assert denormalize._position_in_document({"source_span": {"chars": [42, 99]}}) == 42
    assert denormalize._position_in_document({"source_span": {"chars": [0, 1]}}) == 0
    # No source_span at all
    assert denormalize._position_in_document({}) is None
    # source_span not a dict
    assert denormalize._position_in_document({"source_span": "bogus"}) is None
    # Empty chars list
    assert denormalize._position_in_document({"source_span": {"chars": []}}) is None
    # Non-numeric chars
    assert (
        denormalize._position_in_document({"source_span": {"chars": ["a", "b"]}})
        is None
    )


def test_position_in_document_emitted_on_speeches():
    sidecar = _plenary_sidecar()
    docs = denormalize.to_speeches_docs(sidecar)
    by_id = {d["_id"]: d["_source"] for d in docs}
    # act-1 source_span.chars = [3048, 3897]
    assert by_id["mo://2018/II/168#agenda-1#act-1"]["position_in_document"] == 3048
    # act-2 source_span.chars = [4226, 4240]
    assert by_id["mo://2018/II/168#agenda-1#act-2"]["position_in_document"] == 4226


def test_position_in_document_emitted_on_votes():
    sidecar = _plenary_sidecar()
    docs = denormalize.to_votes_docs(sidecar)
    # vote-1 source_span.chars = [4112, 4226]
    assert docs[0]["_source"]["position_in_document"] == 4112


def test_position_in_document_emitted_on_agenda_items():
    sidecar = _plenary_sidecar()
    docs = denormalize.to_agenda_items_docs(sidecar)
    # agenda-1 source_span.chars = [0, 4240]
    assert docs[0]["_source"]["position_in_document"] == 0


def test_position_in_document_orders_correctly():
    """The whole point of position_in_document: a unified ascending sort
    across speeches + votes + agenda items lays out the document in
    source order. agenda items first (they wrap their children), then
    activities in source order.
    """
    sidecar = _plenary_sidecar()
    speeches = denormalize.to_speeches_docs(sidecar)
    votes = denormalize.to_votes_docs(sidecar)
    agenda = denormalize.to_agenda_items_docs(sidecar)
    rows = [
        (d["_source"]["position_in_document"], d["_id"])
        for d in (*agenda, *speeches, *votes)
    ]
    rows.sort()
    ordered_ids = [r[1] for r in rows]
    # agenda-1 wraps everything (chars[0]=0), then act-1 (3048),
    # then vote-1 (4112), then act-2 (4226).
    assert ordered_ids == [
        "mo://2018/II/168#agenda-1",
        "mo://2018/II/168#agenda-1#act-1",
        "mo://2018/II/168#agenda-1#vote-1",
        "mo://2018/II/168#agenda-1#act-2",
    ]


def test_position_in_document_none_when_source_span_missing():
    """Defensive: a sidecar lacking source_span (legacy / not-yet-
    backfilled) emits None, never errors.
    """
    sidecar = _plenary_sidecar()
    # Strip source_span from an activity
    sidecar["body"]["agenda_items"][0]["activities"][0].pop("source_span", None)
    docs = denormalize.to_speeches_docs(sidecar)
    by_id = {d["_id"]: d["_source"] for d in docs}
    assert by_id["mo://2018/II/168#agenda-1#act-1"]["position_in_document"] is None
    # Other speeches still get their position
    assert by_id["mo://2018/II/168#agenda-1#act-2"]["position_in_document"] == 4226


# ----------------------------------------------------------------------
# Discourse markers: per-marker projection + char_range computation
# ----------------------------------------------------------------------


def test_speech_discourse_markers_projected_with_char_range():
    """Each Hawkins marker must surface kind, marker_confidence,
    rationale_short, and evidence.{text, char_range}. char_range is
    computed against the speech text via find_text_offsets_tolerant
    so the browser can highlight the evidence inline without porting
    the typography-tolerant matcher to JS.
    """
    sidecar = _plenary_sidecar()
    speech_text = sidecar["body"]["agenda_items"][0]["activities"][0]["text"]
    # Pick verbatim substrings from the speech body so the matcher
    # finds them strictly.
    quote = "intervenție substanțială"
    expected_start = speech_text.index(quote)
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "ff00",
                "hawkins": {
                    "score": 1,
                    "framework_confidence": 0.7,
                    "framework_version": "hawkins@2018",
                    "rationale": "Marker rationale for the framework.",
                    "markers": [
                        {
                            "kind": "people_vs_elite",
                            "marker_confidence": 0.85,
                            "rationale_short": "Anchored on people-vs-elite framing.",
                            "evidence": {"text": quote},
                        },
                    ],
                },
                "voice": None,
                "dqi": None,
                "vparty": None,
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    speech = by_id["mo://2018/II/168#agenda-1#act-1"]
    hawkins = speech["enrichments"]["discourse"]["hawkins"]
    assert hawkins["framework_version"] == "hawkins@2018"
    assert hawkins["rationale"] == "Marker rationale for the framework."
    assert len(hawkins["markers"]) == 1
    marker = hawkins["markers"][0]
    assert marker["kind"] == "people_vs_elite"
    assert marker["marker_confidence"] == 0.85
    assert marker["rationale_short"] == "Anchored on people-vs-elite framing."
    assert marker["evidence"]["text"] == quote
    assert marker["evidence"]["char_range"] == [
        expected_start,
        expected_start + len(quote),
    ]


def test_speech_discourse_marker_char_range_handles_typography():
    """Romanian-typography drift (the model paraphrasing curly-quoted
    text against the speech's straight-quote source) must still yield
    a char_range — that's the whole point of using
    find_text_offsets_tolerant over String.indexOf.
    """
    sidecar = _plenary_sidecar()
    # Inject a curly-quoted phrase into the speech text.
    long_text = (
        "## Doamnelor și domnilor,\n\nAceasta este o intervenție „substanțială"
        ' și provocatoare" care depășește pragul de 100 de caractere fără probleme.'
    )
    sidecar["body"]["agenda_items"][0]["activities"][0]["text"] = long_text
    # The model "remembers" the quote with straight quotes — typography
    # drift, not a paraphrase. The tolerant matcher folds both forms
    # to the same shape and recovers byte-correct offsets.
    drifted_quote = '"substanțială și provocatoare"'
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "aa11",
                "hawkins": {
                    "score": 1,
                    "framework_confidence": 0.8,
                    "markers": [
                        {
                            "kind": "people_vs_elite",
                            "evidence": {"text": drifted_quote},
                        }
                    ],
                },
                "voice": None,
                "dqi": None,
                "vparty": None,
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    marker = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]["discourse"][
        "hawkins"
    ]["markers"][0]
    cr = marker["evidence"]["char_range"]
    # The slice should land on the curly-quoted region in the source.
    assert cr is not None
    assert long_text[cr[0] : cr[1] + 1].startswith("„substanțială") or long_text[
        cr[0] : cr[1]
    ].startswith("„substanțială")


def test_speech_discourse_marker_char_range_omitted_for_paraphrase():
    """A genuine paraphrase (added/removed words) must NOT produce a
    char_range. The marker still surfaces with its `text`, and the
    browser can fall back to a simple substring search if it wants.
    """
    sidecar = _plenary_sidecar()
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "bb22",
                "hawkins": {
                    "score": 1,
                    "framework_confidence": 0.7,
                    "markers": [
                        {
                            "kind": "people_vs_elite",
                            "evidence": {
                                "text": "this fragment never appears in the speech body",
                            },
                        }
                    ],
                },
                "voice": None,
                "dqi": None,
                "vparty": None,
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    marker = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]["discourse"][
        "hawkins"
    ]["markers"][0]
    assert marker["evidence"]["text"] == (
        "this fragment never appears in the speech body"
    )
    assert "char_range" not in marker["evidence"]


def test_speech_discourse_dqi_markers_value_coerced_to_string():
    """DQI markers carry mixed-type `value` (int for level_of_justification
    / respect_*; str for content_of_justification / constructive_politics).
    The ES `keyword` mapping rejects ints, so the denormalizer coerces
    them to their string repr.
    """
    sidecar = _plenary_sidecar()
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "cc33",
                "hawkins": None,
                "voice": None,
                "vparty": None,
                "dqi": {
                    "level_of_justification": 2,
                    "content_of_justification": "common_good",
                    "respect_for_groups": 1,
                    "respect_for_demands": 1,
                    "respect_for_counterarguments": 1,
                    "constructive_politics": "alternative_proposal",
                    "framework_confidence": 0.85,
                    "framework_version": "steiner-bachtiger@2017",
                    "rationale": "Holistic DQI explanation.",
                    "markers": [
                        {
                            "kind": "level_of_justification",
                            "value": 2,
                            "marker_confidence": 0.9,
                            "rationale_short": "Causal reasoning.",
                            "evidence": {"text": "intervenție"},
                        },
                        {
                            "kind": "content_of_justification",
                            "value": "common_good",
                            "marker_confidence": 0.92,
                            "rationale_short": "Appeal to common good.",
                            "evidence": {"text": "Doamnelor"},
                        },
                    ],
                },
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    dqi = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]["discourse"]["dqi"]
    assert dqi["framework_confidence"] == 0.85
    assert dqi["framework_version"] == "steiner-bachtiger@2017"
    assert dqi["rationale"] == "Holistic DQI explanation."
    assert len(dqi["markers"]) == 2
    by_kind = {m["kind"]: m for m in dqi["markers"]}
    assert by_kind["level_of_justification"]["value"] == "2"
    assert by_kind["content_of_justification"]["value"] == "common_good"


def test_speech_discourse_voice_classifications_projected():
    """Voice classifications must surface marker_id, voice, voice_confidence,
    attributed_to, rationale_short, and voice_evidence.{text, char_range}
    so the web app can colour each marker chip by attributed voice and
    link it back to its source marker via marker_id.
    """
    sidecar = _plenary_sidecar()
    speech_text = sidecar["body"]["agenda_items"][0]["activities"][0]["text"]
    quote = "Doamnelor și domnilor"
    expected_start = speech_text.index(quote)
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "dd44",
                "hawkins": {
                    "score": 1,
                    "framework_confidence": 0.7,
                    "markers": [
                        {"kind": "people_vs_elite", "evidence": {"text": quote}}
                    ],
                },
                "voice": {
                    "classifications": [
                        {
                            "marker_id": "m_0",
                            "voice": "speaker_first_person",
                            "voice_confidence": 0.95,
                            "attributed_to": None,
                            "rationale_short": "Speaker speaks in their own voice.",
                            "voice_evidence": {"text": quote},
                        }
                    ]
                },
                "dqi": None,
                "vparty": None,
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    voice = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]["discourse"][
        "voice"
    ]
    # Aggregates still emit (back-compat with existing queries).
    assert voice["dominant_voice"] == "speaker_first_person"
    assert voice["voices_seen"] == ["speaker_first_person"]
    # New per-classification array.
    assert len(voice["classifications"]) == 1
    cls = voice["classifications"][0]
    assert cls["marker_id"] == "m_0"
    assert cls["voice"] == "speaker_first_person"
    assert cls["voice_confidence"] == 0.95
    assert cls["rationale_short"] == "Speaker speaks in their own voice."
    assert "attributed_to" not in cls  # null attributed_to is dropped
    assert cls["voice_evidence"]["text"] == quote
    assert cls["voice_evidence"]["char_range"] == [
        expected_start,
        expected_start + len(quote),
    ]


def test_speech_discourse_handles_malformed_marker_shapes():
    """Producer salvage tier may leave non-dict / missing-kind markers.
    The denormaliser must skip them without crashing — the indexer
    runs unattended in production and a single bad sidecar can't be
    allowed to take down a whole bulk.
    """
    sidecar = _plenary_sidecar()
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {"producer": "flash-lite", "version": "0.1"},
                "text_fingerprint": "ee55",
                "hawkins": {
                    "score": 1,
                    "framework_confidence": 0.7,
                    "markers": [
                        "stray-string-not-a-dict",  # skip
                        {"evidence": {"text": "x"}},  # missing kind → skip
                        {"kind": "people_vs_elite"},  # no evidence → keep
                        {"kind": "evil_elite", "evidence": {}},  # empty evidence → keep
                        {
                            "kind": "manichean",
                            "evidence": {"text": ""},
                        },  # empty evidence text → keep, drop evidence
                    ],
                },
                "voice": None,
                "dqi": None,
                "vparty": None,
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    hawkins = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]["discourse"][
        "hawkins"
    ]
    # Two stray entries dropped; three good (evidence-less) markers survive.
    kept_kinds = [m["kind"] for m in hawkins["markers"]]
    assert kept_kinds == ["people_vs_elite", "evil_elite", "manichean"]
    # marker_count uses the RAW count from the source list (incl. dropped
    # entries) — the metric is "how many markers did the model emit",
    # not "how many survived our projection". Keeps the cross-tab
    # aggregations stable across producer salvage policy changes.
    assert hawkins["marker_count"] == 5
    # marker_kinds dedups projected entries (not raw).
    assert set(hawkins["marker_kinds"]) == {
        "people_vs_elite",
        "evil_elite",
        "manichean",
    }


def test_speech_discourse_speech_text_optional():
    """When speech_text is None (caller wired a non-speeches grain that
    happens to carry discourse), markers still project — just without
    char_range. Non-regression for any future caller.
    """
    speech_text = "Lorem ipsum dolor sit amet."
    payload = {
        "hawkins": {
            "score": 1,
            "framework_confidence": 0.7,
            "markers": [{"kind": "people_vs_elite", "evidence": {"text": "ipsum"}}],
        }
    }
    flat = denormalize._flatten_discourse_payload(payload, speech_text=None)
    marker = flat["discourse"]["hawkins"]["markers"][0]
    assert marker["evidence"]["text"] == "ipsum"
    assert "char_range" not in marker["evidence"]
    # Sanity: same payload WITH speech_text recovers the offsets.
    flat2 = denormalize._flatten_discourse_payload(payload, speech_text=speech_text)
    cr = flat2["discourse"]["hawkins"]["markers"][0]["evidence"]["char_range"]
    assert cr == [speech_text.index("ipsum"), speech_text.index("ipsum") + 5]


# ----------------------------------------------------------------------
# End-to-end smoke: full producer-shaped payload through the speeches grain
# ----------------------------------------------------------------------


def test_speech_discourse_end_to_end_smoke():
    """Smoke test: feed a full producer-shaped payload (mirroring an
    actual `<basename>.discourse.flash-lite.v0_1.json` entry — all four
    frameworks populated) through `to_speeches_docs`, confirm every
    declared mapping field is populated correctly, and validate the
    resulting doc against the mo-speeches mapping. This is the closest
    we can get to a full integration test without hitting an ES cluster.
    """
    sidecar = _plenary_sidecar()
    # Use real substrings of the test fixture's speech text so the
    # tolerant matcher recovers byte-correct offsets.
    speech_text = sidecar["body"]["agenda_items"][0]["activities"][0]["text"]
    quote_a = "intervenție substanțială"
    quote_b = "puncte de discuție"
    quote_c = "depăși pragul"
    enrichments = {
        "mo://2018/II/168#agenda-1#act-1": {
            "discourse": {
                "_meta": {
                    "indexed_at": "2026-05-09T21:55:36.259739+00:00",
                    "model_id": "google/gemini-3.1-flash-lite",
                    "namespace": "discourse",
                    "producer": "flash-lite",
                    "prompt_versions": {
                        "dqi": "v1",
                        "hawkins": "v1",
                        "voice": "v1",
                        "vparty": "v2",
                    },
                    "source_sidecar_content_sha": "820fc06c1cd1",
                    "version": "0.1",
                },
                "text_fingerprint": "79b9a7f2af13",
                "hawkins": {
                    "framework_confidence": 0.95,
                    "framework_version": "hawkins@2018",
                    "score": 1,
                    "score_unit": "ordinal_0_2",
                    "rationale": "Discursul prezintă elemente populiste moderate.",
                    "markers": [
                        {
                            "kind": "people_vs_elite",
                            "marker_confidence": 0.9,
                            "rationale_short": "Construct people-vs-elite.",
                            "preliminary_voice": "speaker_first_person",
                            "preliminary_voice_confidence": 0.98,
                            "evidence": {"text": quote_a},
                        },
                        {
                            "kind": "evil_elite",
                            "marker_confidence": 0.85,
                            "rationale_short": "Elite character.",
                            "preliminary_voice": "speaker_first_person",
                            "preliminary_voice_confidence": 0.98,
                            "evidence": {"text": quote_b},
                        },
                    ],
                },
                "voice": {
                    "classifications": [
                        {
                            "marker_id": "m_0",
                            "voice": "speaker_first_person",
                            "voice_confidence": 0.96,
                            "attributed_to": None,
                            "rationale_short": "Speaker's own voice.",
                            "voice_evidence": {"text": quote_a},
                        },
                        {
                            "marker_id": "m_1",
                            "voice": "quoted",
                            "voice_confidence": 0.78,
                            "attributed_to": "Opoziție",
                            "rationale_short": "Quoted opposition.",
                            "voice_evidence": {"text": quote_b},
                        },
                    ]
                },
                "dqi": {
                    "constructive_politics": "positional",
                    "content_of_justification": "common_good",
                    "framework_confidence": 0.85,
                    "framework_version": "steiner-bachtiger@2017",
                    "level_of_justification": 2,
                    "respect_for_counterarguments": 1,
                    "respect_for_demands": 1,
                    "respect_for_groups": 1,
                    "rationale": "Discursul este coerent dar pozițional.",
                    "score_unit": "dqi_multi",
                    "markers": [
                        {
                            "kind": "level_of_justification",
                            "value": 2,
                            "marker_confidence": 0.88,
                            "rationale_short": "Cauzal.",
                            "evidence": {"text": quote_a},
                        },
                        {
                            "kind": "content_of_justification",
                            "value": "common_good",
                            "marker_confidence": 0.92,
                            "rationale_short": "Apel la binele comun.",
                            "evidence": {"text": quote_c},
                        },
                    ],
                },
                "vparty": {
                    "framework_confidence": 0.98,
                    "framework_version": "vparty@2020 + vdem-attacks-on@v13",
                    "score": 0,
                    "score_unit": "ordinal_0_2",
                    "rationale": "Niciun marker anti-pluralist identificat.",
                    "markers": [],
                },
            }
        }
    }
    docs = denormalize.to_speeches_docs(sidecar, enrichments=enrichments)
    by_id = {d["_id"]: d["_source"] for d in docs}
    src = by_id["mo://2018/II/168#agenda-1#act-1"]
    enrich = src["enrichments"]

    # Provenance siblings
    assert enrich["discourse_producer"] == "flash-lite"
    assert enrich["discourse_text_fingerprint"] == "79b9a7f2af13"

    disc = enrich["discourse"]

    # Hawkins: aggregate + per-marker
    h = disc["hawkins"]
    assert h["score"] == 1
    assert h["framework_confidence"] == 0.95
    assert h["framework_version"] == "hawkins@2018"
    assert h["rationale"].startswith("Discursul")
    assert h["marker_count"] == 2
    assert sorted(h["marker_kinds"]) == ["evil_elite", "people_vs_elite"]
    assert len(h["markers"]) == 2
    assert h["markers"][0]["evidence"]["text"] == quote_a
    cr_a = h["markers"][0]["evidence"]["char_range"]
    assert speech_text[cr_a[0] : cr_a[1]] == quote_a

    # Voice: aggregate + per-classification
    v = disc["voice"]
    assert v["dominant_voice"] in ("speaker_first_person", "quoted")
    assert set(v["voices_seen"]) == {"speaker_first_person", "quoted"}
    assert len(v["classifications"]) == 2
    cls0 = v["classifications"][0]
    assert cls0["marker_id"] == "m_0"
    assert cls0["voice_evidence"]["char_range"] is not None
    assert v["classifications"][1]["attributed_to"] == "Opoziție"

    # DQI: aggregate + per-marker (with value coerced)
    d = disc["dqi"]
    assert d["level_of_justification"] == 2
    assert d["framework_version"] == "steiner-bachtiger@2017"
    assert d["rationale"].startswith("Discursul")
    by_kind = {m["kind"]: m for m in d["markers"]}
    assert by_kind["level_of_justification"]["value"] == "2"
    assert by_kind["content_of_justification"]["value"] == "common_good"

    # V-Party: aggregate + empty markers
    vp = disc["vparty"]
    assert vp["score"] == 0
    assert vp["framework_version"].startswith("vparty@2020")
    assert vp["rationale"].startswith("Niciun")
    assert vp["marker_count"] == 0
    assert "markers" not in vp  # no markers projected when source list is empty
    assert "marker_kinds" not in vp

    # Mapping conformance — full doc walks the mapping tree.
    _assert_doc_matches_mapping("mo-speeches", docs[0])


def test_speech_discourse_smoke_via_indexer_pipeline(tmp_path):
    """Higher-level smoke: write a real `<basename>.discourse.flash-lite.v0_1.json`
    file alongside a real sidecar.json, run the enrichment loader (the
    actual code the indexer uses), then denormalise. This catches
    breakage in the loader → denormaliser handoff that synthetic-dict
    tests would miss.
    """
    import json as _json
    from monitorul_ii.elasticsearch import enrichments as enrichments_mod

    sidecar = _plenary_sidecar()
    sidecar_path = tmp_path / "2018-11-20_MO-PII-168-2018.extraction.json"
    sidecar_path.write_text(_json.dumps(sidecar), encoding="utf-8")

    discourse_path = (
        tmp_path / "2018-11-20_MO-PII-168-2018.discourse.flash-lite.v0_1.json"
    )
    speech_text = sidecar["body"]["agenda_items"][0]["activities"][0]["text"]
    quote = "intervenție substanțială"
    discourse_path.write_text(
        _json.dumps(
            {
                "mo://2018/II/168#agenda-1#act-1": {
                    "_meta": {
                        "namespace": "discourse",
                        "producer": "flash-lite",
                        "version": "0.1",
                        "source_sidecar_content_sha": sidecar["content_sha"],
                    },
                    "text_fingerprint": "abcdef",
                    "hawkins": {
                        "score": 1,
                        "framework_confidence": 0.85,
                        "framework_version": "hawkins@2018",
                        "rationale": "Top-level rationale.",
                        "markers": [
                            {
                                "kind": "people_vs_elite",
                                "marker_confidence": 0.9,
                                "rationale_short": "Per-marker rationale.",
                                "evidence": {"text": quote},
                            },
                        ],
                    },
                    "voice": {
                        "classifications": [
                            {
                                "marker_id": "m_0",
                                "voice": "speaker_first_person",
                                "voice_confidence": 0.92,
                                "rationale_short": "First person.",
                                "voice_evidence": {"text": quote},
                            }
                        ]
                    },
                    "dqi": {
                        "level_of_justification": 1,
                        "content_of_justification": "common_good",
                        "respect_for_groups": 1,
                        "respect_for_demands": 1,
                        "respect_for_counterarguments": 0,
                        "constructive_politics": "positional",
                        "framework_confidence": 0.7,
                        "rationale": "DQI rationale.",
                        "markers": [
                            {
                                "kind": "level_of_justification",
                                "value": 1,
                                "marker_confidence": 0.75,
                                "rationale_short": "Mid justification.",
                                "evidence": {"text": quote},
                            }
                        ],
                    },
                    "vparty": {
                        "score": 0,
                        "framework_confidence": 0.95,
                        "framework_version": "vparty@2020",
                        "rationale": "No anti-pluralist markers.",
                        "markers": [],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = enrichments_mod.load_enrichments(
        sidecar_path, sidecar_content_sha=sidecar["content_sha"]
    )
    docs = denormalize.to_speeches_docs(sidecar, enrichments=loaded)
    by_id = {d["_id"]: d["_source"] for d in docs}
    enrich = by_id["mo://2018/II/168#agenda-1#act-1"]["enrichments"]
    assert enrich["discourse_producer"] == "flash-lite"
    h = enrich["discourse"]["hawkins"]
    assert h["rationale"] == "Top-level rationale."
    assert h["framework_version"] == "hawkins@2018"
    assert h["markers"][0]["evidence"]["text"] == quote
    expected_start = speech_text.index(quote)
    assert h["markers"][0]["evidence"]["char_range"] == [
        expected_start,
        expected_start + len(quote),
    ]
    assert (
        enrich["discourse"]["voice"]["classifications"][0]["voice_evidence"][
            "char_range"
        ]
        is not None
    )
    assert enrich["discourse"]["dqi"]["markers"][0]["value"] == "1"
    _assert_doc_matches_mapping("mo-speeches", docs[0])
