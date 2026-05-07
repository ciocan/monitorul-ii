"""Tests for tools.verify_playback — the playback fidelity verifier.

Synthetic-input tests: each builds a tiny in-memory MD body + matching
sidecar dict, calls `verify_doc(...)` with `check_es=False` (offline
mode), and asserts the issue set.

The exemptions encoded in the verifier (continuation, SUMAR title, chair
narration) get explicit "0 issues" tests so a future regression that
chases a false alarm gets caught immediately.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.verify_playback import (
    Issue,
    _has_header_at,
    _name_overlap_ratio,
    _peel_name,
    check_md_coverage,
    check_speeches,
    find_sumar_span,
    iter_speaker_headers,
    verify_doc,
)


# ---- helpers -----------------------------------------------------------


def _write_pair(tmp_path: Path, body: str, sidecar: dict) -> Path:
    """Write `body` and `sidecar` to disk as a pair sharing a basename."""
    md = tmp_path / "test_doc.md"
    md.write_text(body, encoding="utf-8")
    (tmp_path / "test_doc.extraction.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    return md


def _plenary_sidecar(
    body_text: str,
    *,
    document_id: str = "mo://2025/II/1",
    activities: list[dict] | None = None,
    interpellations: list[dict] | None = None,
    agenda_title: str = "Test agenda",
) -> dict:
    """Build a minimal plenary_stenogram sidecar around `activities`."""
    activities = activities or []
    interpellations = interpellations or []
    return {
        "schema_version": "1.13.0",
        "document_id": document_id,
        "content_sha": "0123456789ab",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": "1",
            "year": 2025,
            "part": "II",
            "published": "2025-01-01",
            "session_date": "2025-01-01",
            "chamber": "Camera Deputaților",
        },
        "extraction": {
            "extractor_versions": {"plenary_stenogram": "0.2.9"},
            "identity": {"record_id": document_id},
        },
        "coverage": {"claimed_pct": 1.0, "body_chars": len(body_text)},
        "body": {
            "session": {"chair": [], "secretaries": []},
            "agenda_items": [
                {
                    "id": f"{document_id}#agenda-1",
                    "ordinal": 1,
                    "title": agenda_title,
                    "primary_references": [],
                    "category": "other",
                    "outcome": None,
                    "topics": {"primary": [], "secondary": []},
                    "activities": activities,
                    "source_span": {"chars": [0, len(body_text)], "lines": [1, 1]},
                }
            ],
            "interpellations": interpellations,
        },
    }


def _speech(rid: str, name: str, position: int, end: int, text: str) -> dict:
    return {
        "id": rid,
        "type": "speech",
        "speaker": {
            "raw": name,
            "name": _peel_name(name),
            "title": None,
            "role": None,
            "party_group": None,
            "person_id": None,
        },
        "delivery_mode": None,
        "text": text,
        "references_mentioned": [],
        "source_span": {"chars": [position, end], "lines": [1, 1]},
    }


def _narrator(rid: str, position: int, end: int, text: str = "(Aplauze.)") -> dict:
    return {
        "id": rid,
        "type": "narrator",
        "text": text,
        "source_span": {"chars": [position, end], "lines": [1, 1]},
    }


def _vote(rid: str, position: int, end: int) -> dict:
    return {
        "id": rid,
        "type": "vote",
        "motion_type": None,
        "voting_method": None,
        "outcome": None,
        "counts": {},
        "source_span": {"chars": [position, end], "lines": [1, 1]},
    }


# ---- name normalization helpers ---------------------------------------


def test_peel_name_strips_honorific():
    assert _peel_name("Domnul Emil Boc") == "Emil Boc"
    assert _peel_name("Doamna Roberta Alma Anastase") == "Roberta Alma Anastase"


def test_peel_name_strips_role_clause():
    assert _peel_name("Domnul Emil Boc, prim-ministru") == "Emil Boc"


def test_name_overlap_ratio_match():
    # Diacritic-insensitive token-set Jaccard.
    assert _name_overlap_ratio("Emil Boc", "Domnul Emil Boc") >= 0.5
    assert _name_overlap_ratio("Văcăroiu", "Vacaroiu Nicolae") >= 0.5


def test_name_overlap_ratio_mismatch():
    assert _name_overlap_ratio("Emil Boc", "Roberta Anastase") < 0.5


# ---- header detection -------------------------------------------------


def test_iter_speaker_headers_finds_hash_plain():
    body = "## **Domnul Emil Boc:**\n\nText.\n"
    headers = list(iter_speaker_headers(body))
    assert len(headers) == 1
    assert "Emil Boc" in headers[0][2]


def test_iter_speaker_headers_finds_no_hash_role():
    """Round 1 bug: pattern is `**NAME** – _role_ **:**` — NO `## ` prefix."""
    body = "**Domnul Emil Boc** – _prim-ministrul Guvernului României_ **:**\n\nText.\n"
    headers = list(iter_speaker_headers(body))
    assert len(headers) == 1
    assert "Emil Boc" in headers[0][2]


def test_iter_speaker_headers_finds_hash_with_role():
    body = "## **Domnul Test Person** – _role_ **:**\n\nText.\n"
    headers = list(iter_speaker_headers(body))
    assert len(headers) == 1
    assert "Test Person" in headers[0][2]


def test_has_header_at_position():
    body = (
        "Some prefix text.\n\n"
        "**Domnul Emil Boc** – _prim-ministrul Guvernului României_ **:**\n\n"
        "Speech body."
    )
    pos = body.index("**Domnul")
    res = _has_header_at(body, pos)
    assert res is not None
    name, _ = res
    assert "Emil Boc" in name


def test_has_header_at_position_negative():
    body = "Some prose without a speaker header at all."
    assert _has_header_at(body, 0) is None


# ---- SUMAR detection --------------------------------------------------


def test_find_sumar_span_basic():
    body = (
        "Header line\n\n"
        "## SUMAR\n\n"
        "Item 1: Title A. Pages 1-3\n"
        "Item 2: Title B. Pages 4-7\n\n"
        "## **Doamna Chair:**\n\n"
        "Speech text."
    )
    span = find_sumar_span(body)
    assert span is not None
    start, end = span
    assert "SUMAR" in body[start:end]
    assert "Doamna Chair" not in body[start:end]


def test_find_sumar_span_none():
    body = "No sumar here.\n\n## **Doamna Chair:**\n\nSpeech."
    assert find_sumar_span(body) is None


# ---- end-to-end verify_doc -------------------------------------------


def test_verify_doc_known_good(tmp_path):
    """Two speakers with hash-prefix headers; both attributed correctly."""
    body = (
        "## SUMAR\n\n"
        "1. Test agenda. Pages 1-3\n\n"
        "## **Domnul Speaker One:**\n\n"
        "Hello. " + ("Body of speech one. " * 30) + "\n\n"
        "## **Doamna Speaker Two:**\n\n"
        "Reply. " + ("Body of speech two. " * 30) + "\n"
    )
    pos1 = body.index("## **Domnul Speaker One")
    pos2 = body.index("## **Doamna Speaker Two")
    activities = [
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "Domnul Speaker One",
            pos1,
            pos2,
            body[pos1:pos2].strip(),
        ),
        _speech(
            "mo://2025/II/1#agenda-1#act-2",
            "Doamna Speaker Two",
            pos2,
            len(body),
            body[pos2:].strip(),
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities, agenda_title="Test agenda")
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    assert result.error is None, result.error
    # No issues expected.
    kinds = [i.kind for i in result.issues]
    assert kinds == [], f"Unexpected issues: {kinds}"


def test_verify_doc_injected_misattribution(tmp_path):
    """Speech 2 is misattributed to Speaker One — speaker_mismatch fires."""
    body = (
        "## **Domnul Speaker One:**\n\n"
        "First speech body.\n\n"
        "**Domnul Real Two** – _ministru_ **:**\n\n"
        "Reply text body.\n"
    )
    pos1 = body.index("## **Domnul Speaker One")
    pos2 = body.index("**Domnul Real Two")
    activities = [
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "Domnul Speaker One",
            pos1,
            pos2,
            body[pos1:pos2].strip(),
        ),
        # Wrong speaker attribution!
        _speech(
            "mo://2025/II/1#agenda-1#act-2",
            "Domnul Speaker One",
            pos2,
            len(body),
            body[pos2:].strip(),
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    misattr = [i for i in result.issues if i.kind == "speaker_mismatch"]
    assert len(misattr) == 1
    assert "Real Two" in misattr[0].detail.get("in_body", "")


def test_verify_doc_dropped_turn(tmp_path):
    """MD has a header that the sidecar doesn't claim — dropped_turn fires.

    Bodies are padded so Speaker Three's header sits > 100 chars from any
    sidecar activity start; real-corpus speeches are easily 200+ chars.
    """
    pad = (" ".join(["Filler text content here."] * 20)) + "\n\n"
    body = (
        "## **Domnul Speaker One:**\n\n"
        + pad
        + "## **Doamna Speaker Two:**\n\n"
        + pad
        + "## **Domnul Speaker Three:**\n\n"
        + pad
    )
    pos1 = body.index("## **Domnul Speaker One")
    pos2 = body.index("## **Doamna Speaker Two")
    # Sidecar has only Speakers 1 and 2 — Speaker Three is dropped.
    activities = [
        _speech(
            "mo://2025/II/1#agenda-1#act-1", "Domnul Speaker One", pos1, pos2, "first"
        ),
        _speech(
            "mo://2025/II/1#agenda-1#act-2",
            "Doamna Speaker Two",
            pos2,
            len(body),
            "second",
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    dropped = [i for i in result.issues if i.kind == "dropped_turn"]
    assert len(dropped) == 1
    assert "Three" in dropped[0].detail.get("name", "")


def test_verify_doc_continuation_after_narrator_is_exempt(tmp_path):
    """A speech right after a narrator block is a continuation — no header
    expected, must not flag.
    """
    body = (
        "## **Domnul Speaker:**\n\n"
        "First fragment of speech. Need to fill chars to pass any min "
        "threshold here. " + ("More text. " * 8) + "\n\n"
        "_(Aplauze.)_\n\n"
        "Continuation of the same speaker after applause. "
        + ("And more text. " * 8)
        + "\n"
    )
    pos1 = body.index("## **Domnul Speaker")
    narr_pos = body.index("_(Aplauze.)_")
    narr_end = body.index("\n\n", narr_pos) + 2
    cont_start = narr_end
    activities = [
        # First speech fragment ends before narrator.
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "Domnul Speaker",
            pos1,
            narr_pos,
            body[pos1:narr_pos].strip(),
        ),
        # Narrator block.
        _narrator("mo://2025/II/1#agenda-1#act-2", narr_pos, narr_end, "(Aplauze.)"),
        # Continuation — has the same speaker but no header at its start.
        _speech(
            "mo://2025/II/1#agenda-1#act-3",
            "Domnul Speaker",
            cont_start,
            len(body),
            body[cont_start:].strip(),
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    # No speaker_mismatch and no invented_or_misplaced_speech for the
    # continuation fragment.
    bad_kinds = {"speaker_mismatch", "invented_or_misplaced_speech"}
    bad = [i for i in result.issues if i.kind in bad_kinds]
    assert bad == [], f"Continuation falsely flagged: {bad}"


def test_verify_doc_chair_narration_literal_is_exempt(tmp_path):
    """`<chair narration>` literal speaker name — no header search."""
    body = (
        "_The chair narrates the opening of the session. The italic block "
        "is bounded by underscores at the start and end._\n\n"
        "Subsequent prose.\n"
    )
    activities = [
        # Chair-narration speech wraps the whole italic block; speaker is
        # the literal `<chair narration>`.
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "<chair narration>",
            0,
            len(body),
            body.strip(),
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    # Override the parsed name so it's the literal sentinel (not peeled).
    sc["body"]["agenda_items"][0]["activities"][0]["speaker"]["name"] = None
    sc["body"]["agenda_items"][0]["activities"][0]["speaker"]["raw"] = (
        "<chair narration>"
    )
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    # No speaker_mismatch / invented_or_misplaced_speech.
    bad_kinds = {"speaker_mismatch", "invented_or_misplaced_speech"}
    bad = [i for i in result.issues if i.kind in bad_kinds]
    assert bad == [], f"Chair narration falsely flagged: {bad}"


def test_verify_doc_agenda_title_in_sumar_is_exempt(tmp_path):
    """Agenda position is at the discussion start, not the SUMAR row.
    The verifier must not flag this as a mismatch — it must just check
    that the title appears anywhere reachable in the body.
    """
    body = (
        "## SUMAR\n\n"
        "1. Test agenda title. Pages 1-3\n\n"
        "## **Doamna Chair:**\n\n"
        "Speech opening discussion of the agenda.\n"
    )
    pos1 = body.index("## **Doamna Chair")
    activities = [
        _speech(
            "mo://2025/II/1#agenda-1#act-1", "Doamna Chair", pos1, len(body), "speech"
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities, agenda_title="Test agenda title")
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    # No agenda_title_not_in_body issue.
    bad = [i for i in result.issues if i.kind == "agenda_title_not_in_body"]
    assert bad == [], f"Agenda title in SUMAR falsely flagged: {bad}"


def test_check_votes_passes_when_phrase_present(tmp_path):
    body = (
        "## **Doamna Chair:**\n\n"
        "Now we vote. Supun votului proiectul. Cine este pentru?\n"
        "100 voturi pentru, adoptat.\n"
    )
    speech_pos = body.index("## **Doamna Chair")
    vote_pos = body.index("Supun votului")
    activities = [
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "Doamna Chair",
            speech_pos,
            vote_pos,
            "speech",
        ),
        _vote("mo://2025/II/1#agenda-1#vote-1", vote_pos, len(body)),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    bad = [i for i in result.issues if i.kind == "vote_open_missing"]
    assert bad == []


def test_check_votes_flags_when_phrase_absent(tmp_path):
    body = (
        "## **Doamna Chair:**\n\n"
        "Some discussion that doesn't open a vote at all.\n"
        "Continuing without any vote phrase here.\n"
    )
    speech_pos = body.index("## **Doamna Chair")
    fake_vote_pos = body.index("Continuing")
    activities = [
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "Doamna Chair",
            speech_pos,
            fake_vote_pos,
            "speech",
        ),
        _vote("mo://2025/II/1#agenda-1#vote-1", fake_vote_pos, len(body)),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    bad = [i for i in result.issues if i.kind == "vote_open_missing"]
    assert bad, "Expected vote_open_missing when no opening phrase found"


def test_check_md_coverage_dropped_with_real_pattern(tmp_path):
    """The corpus-real bug: `**Domnul Emil Boc** – _role_ **:**` line in
    body, but sidecar attributes the speech under it to the prior speaker.
    The verifier flags this as `dropped_turn` (no activity within 100 chars
    of the no-hash header).
    """
    pad = (" ".join(["Padding text. " * 4] * 5)) + "\n\n"
    body = (
        "## **Doamna Roberta Alma Anastase:**\n\n"
        "Mulțumesc. Are cuvântul prim-ministrul Guvernului României. "
        + pad
        + "**Domnul Emil Boc** – _prim-ministrul Guvernului României_ **:**\n\n"
        "Doamnă președinte al Camerei Deputaților, " + ("Stimați colegi. " * 10) + "\n"
    )
    pos_anastase = body.index("## **Doamna Roberta")
    activities = [
        # Bug: speech wraps Anastase + Boc; only one activity, attributed
        # to Anastase. Boc's no-hash header is unclaimed.
        _speech(
            "mo://2025/II/1#agenda-1#act-1",
            "Doamna Roberta Alma Anastase",
            pos_anastase,
            len(body),
            body[pos_anastase:].strip(),
        ),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    md = _write_pair(tmp_path, body, sc)
    result = verify_doc(md, check_es=False)
    dropped = [i for i in result.issues if i.kind == "dropped_turn"]
    assert dropped, "Expected dropped_turn for unclaimed Boc header"
    assert any("Boc" in d.detail.get("name", "") for d in dropped)


def test_verify_doc_handles_missing_sidecar(tmp_path):
    md = tmp_path / "missing.md"
    md.write_text("body", encoding="utf-8")
    result = verify_doc(md, check_es=False)
    assert result.error is not None


# ---- direct check_md_coverage / check_speeches signatures -----------


def test_check_md_coverage_passes_when_all_headers_claimed():
    body = (
        "## SUMAR\n\nNothing here.\n\n"
        "## **Domnul Speaker A:**\n\nSpeech A.\n\n"
        "## **Doamna Speaker B:**\n\nSpeech B.\n"
    )
    posA = body.index("## **Domnul Speaker A")
    posB = body.index("## **Doamna Speaker B")
    activities = [
        _speech("a", "Domnul Speaker A", posA, posB, "Speech A."),
        _speech("b", "Doamna Speaker B", posB, len(body), "Speech B."),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    issues = check_md_coverage(sc, body)
    assert issues == []


def test_check_md_coverage_returns_empty_for_qr():
    """Question-register docs use the same header shapes; we don't run
    md-coverage on them.
    """
    body = "**Domnul Test Person** – _ministru_ **:**\n\nReply.\n"
    sc = {
        "schema_version": "1.13.0",
        "document_id": "mo://2025/II/2",
        "content_sha": "0",
        "document_type": "question_register",
        "metadata": {},
        "extraction": {"identity": {"record_id": "mo://2025/II/2"}},
        "coverage": {"claimed_pct": 1.0},
        "body": {"questions": []},
    }
    issues = check_md_coverage(sc, body)
    assert issues == []


def test_check_speeches_flags_oor_position():
    body = "## **Test Speaker:**\n\nSpeech.\n"
    activities = [
        _speech("a", "Test Speaker", 99999, 99999 + 10, "Speech."),
    ]
    sc = _plenary_sidecar(body, activities=activities)
    issues = check_speeches(sc, body)
    oor = [i for i in issues if i.kind == "position_oor_sidecar"]
    assert oor


def test_issue_to_dict_round_trip():
    iss = Issue(kind="speaker_mismatch", rid="x", detail={"a": 1})
    d = iss.to_dict()
    assert d["kind"] == "speaker_mismatch"
    assert d["rid"] == "x"
    assert d["a"] == 1
