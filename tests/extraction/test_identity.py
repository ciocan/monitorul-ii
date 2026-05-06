"""Tests for the identity producer.

Covers `mint_record_id` per grain, `compute_content_fingerprint`
determinism, `compute_short_id` stability, `mint_slug` ASCII-fold +
truncation + edge-case behaviour, `assign_identity`'s slug-once contract
and per-grain id minting, plus end-to-end identity persistence across a
re-extract and `--identity-only` backfill.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from monitorul_ii.extraction import extract
from monitorul_ii.extraction.identity import (
    IDENTITY_VERSION,
    assign_identity,
    compute_content_fingerprint,
    compute_short_id,
    mint_record_id,
    mint_slug,
)
from monitorul_ii.extraction.schema import SchemaError, validate


# -- mint_record_id --------------------------------------------------------


def test_mint_record_id_document_grain_returns_doc_id():
    assert mint_record_id("mo://2026/II/29", "document") == "mo://2026/II/29"


def test_mint_record_id_report_grain_returns_doc_id():
    """The report grain mirrors the document_id (one report per R-MO)."""
    assert mint_record_id("mo://2014/II/1R", "report") == "mo://2014/II/1R"


def test_mint_record_id_agenda_item_appends_ordinal():
    assert (
        mint_record_id("mo://2026/II/29", "agenda_item", ordinal=3)
        == "mo://2026/II/29#agenda-3"
    )


def test_mint_record_id_activity_appends_act_seq():
    assert (
        mint_record_id("mo://2026/II/29", "activity", ordinal=3, seq=12)
        == "mo://2026/II/29#agenda-3#act-12"
    )


def test_mint_record_id_vote_appends_vote_seq():
    """Votes get their own seq counter (separate from non-vote activities)."""
    assert (
        mint_record_id("mo://2026/II/29", "vote", ordinal=3, seq=2)
        == "mo://2026/II/29#agenda-3#vote-2"
    )


def test_mint_record_id_interpellation_uses_natural_key():
    assert (
        mint_record_id(
            "mo://2026/II/29", "interpellation", natural_key="123/2024-09-15"
        )
        == "mo://2026/II/29#interp-123/2024-09-15"
    )


def test_mint_record_id_interpellation_seq_when_no_natural_key():
    assert (
        mint_record_id("mo://2026/II/29", "interpellation_seq", seq=4)
        == "mo://2026/II/29#interp-seq-4"
    )


def test_mint_record_id_question_uses_regnum():
    assert (
        mint_record_id("mo://2026/II/29", "question", natural_key="2.303A/2024-09-15")
        == "mo://2026/II/29#q-2.303A/2024-09-15"
    )


def test_mint_record_id_committee_meeting():
    assert (
        mint_record_id(
            "mo://2026/II/29c", "committee_meeting", natural_key="comisia-juridica-1"
        )
        == "mo://2026/II/29c#cmt-comisia-juridica-1"
    )


def test_mint_record_id_committee_agenda_item():
    assert (
        mint_record_id(
            "mo://2026/II/29c",
            "committee_agenda_item",
            natural_key="comisia-juridica-1",
            ordinal=2,
        )
        == "mo://2026/II/29c#cmt-comisia-juridica-1#item-2"
    )


def test_mint_record_id_rejects_unknown_grain():
    with pytest.raises(ValueError, match="unknown grain"):
        mint_record_id("mo://2026/II/29", "bogus")


def test_mint_record_id_requires_required_args():
    with pytest.raises(ValueError, match="agenda_item requires ordinal"):
        mint_record_id("mo://2026/II/29", "agenda_item")
    with pytest.raises(ValueError, match="activity requires ordinal"):
        mint_record_id("mo://2026/II/29", "activity", ordinal=1)


def test_mint_record_id_natural_key_punctuation_normalized():
    """Special characters in natural keys are replaced with `-` so the
    resulting record_id matches the schema's RecordId pattern."""
    rid = mint_record_id(
        "mo://2026/II/29", "interpellation", natural_key="abc; def & xyz"
    )
    # `;`, `&` collapse into `-`
    assert rid == "mo://2026/II/29#interp-abc-def-xyz"


# -- compute_content_fingerprint ------------------------------------------


def test_compute_content_fingerprint_returns_12_hex_chars():
    fp = compute_content_fingerprint("hello world")
    assert len(fp) == 12
    assert all(c in "0123456789abcdef" for c in fp)


def test_compute_content_fingerprint_deterministic():
    a = compute_content_fingerprint("Hello, world!")
    b = compute_content_fingerprint("Hello, world!")
    assert a == b


def test_compute_content_fingerprint_normalizes_whitespace():
    """Whitespace differences shouldn't perturb the fingerprint — newlines
    and double spaces collapse to single spaces."""
    a = compute_content_fingerprint("hello\nworld")
    b = compute_content_fingerprint("hello world")
    c = compute_content_fingerprint("  hello   world  ")
    assert a == b == c


def test_compute_content_fingerprint_changes_with_content():
    a = compute_content_fingerprint("Apple")
    b = compute_content_fingerprint("Banana")
    assert a != b


def test_compute_content_fingerprint_handles_empty_input():
    fp = compute_content_fingerprint("")
    assert len(fp) == 12  # sha256 of "" still hashes


# -- compute_short_id ------------------------------------------------------


def test_compute_short_id_deterministic():
    a = compute_short_id(2026, "29", ordinal=3, seq=12)
    b = compute_short_id(2026, "29", ordinal=3, seq=12)
    assert a == b


def test_compute_short_id_different_inputs_different_outputs():
    """Distinct (year, issue, ordinal, seq) produce distinct short_ids."""
    base = compute_short_id(2026, "29", ordinal=1, seq=1)
    assert base != compute_short_id(2026, "29", ordinal=2, seq=1)
    assert base != compute_short_id(2026, "29", ordinal=1, seq=2)
    assert base != compute_short_id(2026, "30", ordinal=1, seq=1)
    assert base != compute_short_id(2025, "29", ordinal=1, seq=1)


def test_compute_short_id_is_lowercase_base32():
    sid = compute_short_id(2026, "29", ordinal=1, seq=1)
    # 6 raw bytes → 10 chars after stripping `=` padding
    assert len(sid) == 10
    assert all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in sid)


def test_compute_short_id_stable_without_optional_args():
    """Calling without ordinal/seq still yields a stable short_id."""
    a = compute_short_id(2026, "29")
    b = compute_short_id(2026, "29")
    assert a == b
    assert len(a) == 10


# -- mint_slug -------------------------------------------------------------


def test_mint_slug_strips_diacritics():
    """Romanian diacritics (ă, â, î, ș, ț) fold to ASCII."""
    short = "abc1234567"
    slug = mint_slug("Lege privind protecția mediului", short)
    assert slug == f"lege-privind-protectia-mediului-{short}"


def test_mint_slug_lowercases_uppercase():
    short = "abc1234567"
    slug = mint_slug("HEALTH POLICY", short)
    assert slug == f"health-policy-{short}"


def test_mint_slug_falls_back_to_short_id_on_empty_title():
    assert mint_slug("", "abc1234567") == "abc1234567"


def test_mint_slug_falls_back_to_short_id_on_all_punctuation():
    """A title made entirely of non-alphanumeric chars produces no slug
    keywords; the short_id alone is the canonical URL tail."""
    assert mint_slug("!!! ??? ...", "abc1234567") == "abc1234567"


def test_mint_slug_truncates_to_8_tokens():
    short = "abc1234567"
    long_title = "one two three four five six seven eight nine ten eleven"
    slug = mint_slug(long_title, short)
    # Only first 8 tokens kept, then dash + short_id
    assert slug == f"one-two-three-four-five-six-seven-eight-{short}"


def test_mint_slug_respects_max_keyword_chars():
    """A long single-word title gets capped at max_keyword_chars."""
    short = "abc1234567"
    title = "supercalifragilisticexpialidocious " * 5
    slug = mint_slug(title, short, max_keyword_chars=20)
    # The keyword portion is at most 20 chars long
    keyword = slug[: -len(short) - 1]  # strip trailing `-<short>`
    assert len(keyword) <= 20


def test_mint_slug_preserves_numerics():
    short = "abc1234567"
    slug = mint_slug("Legea 47/1992", short)
    # `/` becomes `-`, numbers preserved
    assert slug == f"legea-47-1992-{short}"


# -- assign_identity --------------------------------------------------------


def _make_qr_body(question_count: int = 2) -> dict:
    return {
        "session_label": None,
        "chamber": "Camera Deputaților",
        "questions": [
            {
                "ordinal": i,
                "addressee": {
                    "ministry": "X",
                    "ministry_normalized": None,
                    "name": None,
                    "role": None,
                },
                "questioner": {
                    "raw": "Speaker",
                    "name": None,
                    "title": None,
                    "role": None,
                    "party_group": None,
                    "person_id": None,
                },
                "registration_number": f"reg-{i}/2025",
                "registration_date": None,
                "topic": f"Topic about education {i}",
                "question_text": f"Body of question {i}",
                "source_span": {
                    "chars": [0, 1],
                    "lines": [1, 1],
                    "content_sha": "0123456789ab",
                },
                "extraction": {
                    "extractor": "x",
                    "confidence": 0.9,
                    "source_span": {
                        "chars": [0, 1],
                        "lines": [1, 1],
                        "content_sha": "0123456789ab",
                    },
                },
            }
            for i in range(1, question_count + 1)
        ],
    }


def test_assign_identity_returns_envelope_record_id():
    body = _make_qr_body()
    identity = assign_identity(
        body,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    assert identity == {"record_id": "mo://2025/II/1"}


def test_assign_identity_stamps_questions_in_place():
    body = _make_qr_body(question_count=2)
    assign_identity(
        body,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    questions = body["questions"]
    assert questions[0]["id"] == "mo://2025/II/1#q-reg-1/2025"
    assert questions[1]["id"] == "mo://2025/II/1#q-reg-2/2025"
    assert all(len(q["content_fingerprint"]) == 12 for q in questions)
    # Slugs include the topic-derived keyword and a short_id tail
    for q in questions:
        assert "topic-about-education" in q["slug"]


def test_assign_identity_preserves_slugs_when_record_id_matches():
    """The slug-once contract: when prior_sidecar carries the same id, the
    prior slug wins even if the title evolves."""
    body_v1 = _make_qr_body(question_count=1)
    assign_identity(
        body_v1,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    prior_slug = body_v1["questions"][0]["slug"]

    # Imagine v2 of the extractor reformats the title slightly. The prior
    # sidecar still has the v1 slug — feed it as prior_sidecar and the
    # slug must survive verbatim.
    body_v2 = _make_qr_body(question_count=1)
    body_v2["questions"][0]["topic"] = "Completely reworded topic"
    assign_identity(
        body_v2,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
        prior_sidecar={"body": body_v1},
    )
    assert body_v2["questions"][0]["slug"] == prior_slug


def test_assign_identity_regenerates_slug_when_record_id_changes():
    """A new record (e.g., new question added) doesn't inherit any slug —
    the slug-once contract only protects records whose id matches."""
    body_v1 = _make_qr_body(question_count=1)
    assign_identity(
        body_v1,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )

    body_v2 = _make_qr_body(question_count=2)  # extra question
    assign_identity(
        body_v2,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
        prior_sidecar={"body": body_v1},
    )
    # First question's slug preserved
    assert body_v2["questions"][0]["slug"] == body_v1["questions"][0]["slug"]
    # Second question gets a freshly-minted slug — non-empty, contains
    # the topic-keyword + short_id tail
    assert "topic-about-education" in body_v2["questions"][1]["slug"]


def test_assign_identity_changes_fingerprint_when_text_changes():
    """The forensic property: same id, different content → different
    fingerprint. Migration scripts use this to detect changed records."""
    body_v1 = _make_qr_body(question_count=1)
    assign_identity(
        body_v1,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    fp_before = body_v1["questions"][0]["content_fingerprint"]

    body_v2 = _make_qr_body(question_count=1)
    body_v2["questions"][0]["question_text"] = "Different body now"
    assign_identity(
        body_v2,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    assert fp_before != body_v2["questions"][0]["content_fingerprint"]


def test_assign_identity_handles_committee_synthesis_meetings_and_agenda():
    body = {
        "period": {"start": "2025-01-01", "end": "2025-01-07"},
        "committees": [
            {
                "name": "Comisia juridică",
                "kind": "permanent",
                "chair": None,
                "secretary": None,
                "meetings": [
                    {
                        "dates": ["2025-01-03"],
                        "time_windows": [],
                        "format": None,
                        "purpose": None,
                        "joint_with": [],
                        "roster": [],
                        "agenda": [
                            {
                                "ordinal": 1,
                                "title": "Proiectul de lege X",
                                "primary_references": [],
                                "co_committees": [],
                                "committee_role": None,
                                "output_type": None,
                                "for_committees": [],
                                "outcome_text": None,
                                "vote_summary": None,
                                "source_span": {
                                    "chars": [0, 1],
                                    "lines": [1, 1],
                                    "content_sha": "0123456789ab",
                                },
                                "extraction": {
                                    "extractor": "x",
                                    "confidence": 0.9,
                                    "source_span": {
                                        "chars": [0, 1],
                                        "lines": [1, 1],
                                        "content_sha": "0123456789ab",
                                    },
                                },
                            }
                        ],
                        "source_span": {
                            "chars": [0, 100],
                            "lines": [1, 5],
                            "content_sha": "0123456789ab",
                        },
                        "extraction": {
                            "extractor": "x",
                            "confidence": 0.9,
                            "source_span": {
                                "chars": [0, 100],
                                "lines": [1, 5],
                                "content_sha": "0123456789ab",
                            },
                        },
                    }
                ],
                "source_span": {
                    "chars": [0, 200],
                    "lines": [1, 10],
                    "content_sha": "0123456789ab",
                },
                "extraction": {
                    "extractor": "x",
                    "confidence": 0.9,
                    "source_span": {
                        "chars": [0, 200],
                        "lines": [1, 10],
                        "content_sha": "0123456789ab",
                    },
                },
            }
        ],
    }
    assign_identity(
        body,
        doc_type="committee_synthesis",
        doc_id="mo://2025/II/1c",
        year=2025,
        issue="1c",
    )
    meeting = body["committees"][0]["meetings"][0]
    assert meeting["id"].startswith("mo://2025/II/1c#cmt-")
    assert "#cmt-" in meeting["id"]
    agenda_item = meeting["agenda"][0]
    assert agenda_item["id"].startswith(meeting["id"])
    assert "#item-1" in agenda_item["id"]


def test_assign_identity_stamps_report_grain():
    body = {
        "report": {
            "title": "Raportul X în anul 2010",
            "issuing_body": "X",
            "issuing_body_normalized": None,
            "reporting_period": {"start": "2010-01-01", "end": "2010-12-31"},
            "received_at": {
                "session_kind": "joint",
                "session_date": "2013-12-04",
                "received_in_document": None,
            },
        },
        "headings": [],
        "raw_markdown_excerpt": "",
    }
    identity = assign_identity(
        body,
        doc_type="report_facsimile",
        doc_id="mo://2014/II/1R",
        year=2014,
        issue="1R",
    )
    assert identity == {"record_id": "mo://2014/II/1R"}
    assert body["report"]["id"] == "mo://2014/II/1R"
    assert len(body["report"]["content_fingerprint"]) == 12
    # Slug is title-derived
    assert "raportul-x" in body["report"]["slug"]


def test_assign_identity_handles_plenary_with_speech_and_vote():
    """Speech and vote activities use independent seq counters within an
    agenda item — votes get `vote-N`, others get `act-N`."""
    body = {
        "session": {
            "chair": [],
            "chair_segments": [],
            "secretaries": [],
            "attendance": {"registered": None, "total_seats": None},
            "quorum_met": None,
            "opened_at": None,
            "closed_at": None,
            "format": None,
            "outcome": None,
            "special_procedure": None,
        },
        "agenda_items": [
            {
                "ordinal": 1,
                "title": "Test",
                "primary_references": [],
                "category": "bill_debate",
                "confidence_type": None,
                "requested_by_group": None,
                "outcome": None,
                "reexamination_reason": None,
                "pages_in_pdf": [],
                "topics": {"primary": [], "secondary": []},
                "activities": [
                    {
                        "type": "speech",
                        "speaker": {
                            "raw": "X",
                            "name": None,
                            "title": None,
                            "role": None,
                            "party_group": None,
                            "person_id": None,
                        },
                        "delivery_mode": None,
                        "text": "speech 1",
                        "references_mentioned": [],
                        "source_span": {
                            "chars": [0, 10],
                            "lines": [1, 1],
                            "content_sha": "0123456789ab",
                        },
                        "extraction": {
                            "extractor": "x",
                            "confidence": 0.9,
                            "source_span": {
                                "chars": [0, 10],
                                "lines": [1, 1],
                                "content_sha": "0123456789ab",
                            },
                        },
                    },
                    {
                        "type": "vote",
                        "motion_text": "vote 1",
                        "motion_type": "final",
                        "voting_method": "electronic",
                        "timing": "live",
                        "counts": {
                            "for": 100,
                            "against": 0,
                            "abstain": 0,
                            "not_voting": 0,
                            "total_voting": 100,
                        },
                        "outcome": "approved",
                        "quorum_announced": None,
                        "proposed_by": None,
                        "nominal_breakdown": None,
                        "defers_to": None,
                        "resolves": [],
                        "source_span": {
                            "chars": [10, 20],
                            "lines": [2, 2],
                            "content_sha": "0123456789ab",
                        },
                        "extraction": {
                            "extractor": "x",
                            "confidence": 0.9,
                            "source_span": {
                                "chars": [10, 20],
                                "lines": [2, 2],
                                "content_sha": "0123456789ab",
                            },
                        },
                    },
                    {
                        "type": "speech",
                        "speaker": {
                            "raw": "Y",
                            "name": None,
                            "title": None,
                            "role": None,
                            "party_group": None,
                            "person_id": None,
                        },
                        "delivery_mode": None,
                        "text": "speech 2",
                        "references_mentioned": [],
                        "source_span": {
                            "chars": [20, 30],
                            "lines": [3, 3],
                            "content_sha": "0123456789ab",
                        },
                        "extraction": {
                            "extractor": "x",
                            "confidence": 0.9,
                            "source_span": {
                                "chars": [20, 30],
                                "lines": [3, 3],
                                "content_sha": "0123456789ab",
                            },
                        },
                    },
                ],
                "source_span": {
                    "chars": [0, 30],
                    "lines": [1, 3],
                    "content_sha": "0123456789ab",
                },
                "extraction": {
                    "extractor": "x",
                    "confidence": 0.9,
                    "source_span": {
                        "chars": [0, 30],
                        "lines": [1, 3],
                        "content_sha": "0123456789ab",
                    },
                },
            }
        ],
        "interpellations": [],
    }
    assign_identity(
        body,
        doc_type="plenary_stenogram",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    acts = body["agenda_items"][0]["activities"]
    assert acts[0]["id"] == "mo://2025/II/1#agenda-1#act-1"  # first speech
    assert acts[1]["id"] == "mo://2025/II/1#agenda-1#vote-1"  # vote-seq=1
    assert acts[2]["id"] == "mo://2025/II/1#agenda-1#act-2"  # second speech


# -- end-to-end via the extract pipeline -----------------------------------


def test_extract_writes_identity_block_in_envelope(tmp_path: Path):
    """Drive a real extract; the resulting sidecar must carry an envelope
    `extraction.identity.record_id` matching `document_id`."""
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)

    r = extract(md, write=False)
    assert r.status == "extract"
    sc = r.sidecar
    assert sc["extraction"]["identity"] == {"record_id": sc["document_id"]}
    assert sc["extraction"]["extractor_versions"]["identity"] == IDENTITY_VERSION


def test_extract_stamps_per_record_identity_on_qr_questions(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)

    r = extract(md, write=False)
    assert r.status == "extract"
    questions = r.sidecar["body"]["questions"]
    assert questions, "fixture should produce at least one question"
    for q in questions:
        assert q["id"].startswith(r.sidecar["document_id"] + "#q-")
        assert len(q["content_fingerprint"]) == 12
        assert q["slug"]


def test_extract_preserves_slug_across_force_re_extract(tmp_path: Path):
    """Slug-once: a second extract (--force) on the same MD must reuse
    the slug from the first pass."""
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)

    r1 = extract(md, force=False, write=True)
    assert r1.status == "extract"
    sc1 = json.loads(r1.sidecar_path.read_text(encoding="utf-8"))
    slug_before = sc1["body"]["questions"][0]["slug"]

    r2 = extract(md, force=True, write=True)
    assert r2.status == "extract"
    sc2 = json.loads(r2.sidecar_path.read_text(encoding="utf-8"))
    slug_after = sc2["body"]["questions"][0]["slug"]

    assert slug_before == slug_after, "slug must survive a force re-extract"


def test_identity_only_backfill_skips_when_already_current(tmp_path: Path):
    """First pass writes the sidecar with current identity; second
    --identity-only pass skips."""
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)

    extract(md, force=False, write=True)
    r = extract(md, identity_only=True, write=True)
    assert r.status == "skip"
    assert "identity current" in (r.reason or "")


def test_identity_only_backfill_re_runs_when_force(tmp_path: Path):
    """`--force --identity-only` re-runs even when identity is current."""
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)

    extract(md, force=False, write=True)
    r = extract(md, identity_only=True, force=True, write=True)
    assert r.status == "extract"


def test_identity_only_errors_when_sidecar_missing(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)
    # No sidecar yet — --identity-only requires one
    r = extract(md, identity_only=True, write=True)
    assert r.status == "error"
    assert "missing sidecar" in (r.reason or "")


def test_identity_only_upgrades_legacy_sidecar(tmp_path: Path):
    """Simulate a legacy sidecar (1.12.0-shaped, no identity) on disk.
    `--identity-only` must upgrade it to 1.13.0 with identity stamped onto
    every record. This is the corpus-backfill path."""
    fixture = Path(__file__).parent / "fixtures" / "qr_2026-03-25_29.md"
    md = tmp_path / fixture.name
    shutil.copy(fixture, md)

    # First do a normal extract to produce a current sidecar
    r1 = extract(md, force=False, write=True)
    sc = json.loads(r1.sidecar_path.read_text(encoding="utf-8"))

    # Strip the identity layer to simulate a legacy sidecar
    sc["schema_version"] = "1.12.0"
    sc["extraction"].pop("identity", None)
    sc["extraction"]["extractor_versions"].pop("identity", None)
    for q in sc["body"]["questions"]:
        q.pop("id", None)
        q.pop("content_fingerprint", None)
        q.pop("slug", None)
    r1.sidecar_path.write_text(
        json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Identity-only backfill
    r2 = extract(md, identity_only=True, write=True)
    assert r2.status == "extract", r2.reason
    after = json.loads(r1.sidecar_path.read_text(encoding="utf-8"))
    assert after["schema_version"] == "1.13.0"
    assert after["extraction"]["identity"]["record_id"] == after["document_id"]
    assert after["extraction"]["extractor_versions"]["identity"] == IDENTITY_VERSION
    for q in after["body"]["questions"]:
        assert q["id"]
        assert len(q["content_fingerprint"]) == 12
        assert q["slug"]


# -- schema validation -----------------------------------------------------


def test_validation_rejects_sidecar_missing_identity_block():
    """The schema requires `extraction.identity` — sidecars without one
    are rejected."""
    sc = {
        "schema_version": "1.13.0",
        "document_id": "mo://2025/II/1",
        "content_sha": "0123456789ab",
        "document_type": "question_register",
        "metadata": {
            "issue": "1",
            "year": 2025,
            "part": "II",
            "published": "2025-04-01",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": None,
            "legislature": None,
        },
        "raw_markdown_path": "x.md",
        "raw_pdf_path": "x.pdf",
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2025-04-01T12:00:00Z",
            "extractor_versions": {"identity": IDENTITY_VERSION},
            "confidence": 0.9,
            # identity intentionally missing
        },
        "coverage": {
            "body_chars": 1,
            "claimed_chars": 1,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
        "body": {
            "session_label": None,
            "chamber": "Camera Deputaților",
            "questions": [],
        },
    }
    with pytest.raises(SchemaError):
        validate(sc)


def test_validation_rejects_record_id_with_invalid_pattern():
    """The schema's RecordId pattern enforces `mo://YYYY/PART/ISSUE`
    prefix; bare strings should fail."""
    body = _make_qr_body(question_count=1)
    assign_identity(
        body,
        doc_type="question_register",
        doc_id="mo://2025/II/1",
        year=2025,
        issue="1",
    )
    body["questions"][0]["id"] = "not-a-mo-uri"
    sc = {
        "schema_version": "1.13.0",
        "document_id": "mo://2025/II/1",
        "content_sha": "0123456789ab",
        "document_type": "question_register",
        "metadata": {
            "issue": "1",
            "year": 2025,
            "part": "II",
            "published": "2025-04-01",
            "chamber": "Camera Deputaților",
            "session": None,
            "session_type": None,
            "session_date": None,
            "legislature": None,
        },
        "raw_markdown_path": "x.md",
        "raw_pdf_path": "x.pdf",
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2025-04-01T12:00:00Z",
            "extractor_versions": {"identity": IDENTITY_VERSION},
            "confidence": 0.9,
            "identity": {"record_id": "mo://2025/II/1"},
        },
        "coverage": {
            "body_chars": 1,
            "claimed_chars": 1,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
        "body": body,
    }
    with pytest.raises(SchemaError):
        validate(sc)
