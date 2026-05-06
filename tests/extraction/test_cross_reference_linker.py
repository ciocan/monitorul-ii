"""Tests for the cross-reference (intra-doc) linker.

Synthetic plenary stenogram sidecars (built in-test) cover:
  - Single anchor in same list → resolved
  - Multiple anchors → most-recent-preceding wins
  - No anchor in list → unresolved (resolved_to stays unset)
  - Idempotent re-link (already-resolved skip)
  - --force overwrites
  - --force clears stale resolved_to when no anchor
  - Schema validation guard rejects bad shapes
  - Code-anchor priority tie-break (when both law and code end at the
    same offset)
  - Document-type filter (committee_synthesis is skipped)
  - Cross-list isolation (anchor in one list doesn't resolve unknown in
    a different list)
  - Schema version bump on write
"""

from __future__ import annotations

import json
from pathlib import Path

from monitorul_ii.extraction.cross_reference_linker import (
    XREF_LINKER_VERSION,
    XrefLinkResult,
    link_all_xrefs,
    link_xrefs,
)


# -- helpers --------------------------------------------------------------


def _envelope(*, doc_id: str, doc_type: str, schema_version: str = "1.13.0") -> dict:
    return {
        "schema_version": schema_version,
        "document_id": doc_id,
        "content_sha": "0123456789ab",
        "document_type": doc_type,
        "metadata": {
            "issue": doc_id.split("/")[-1],
            "year": 2025,
            "part": "II",
            "published": "2025-04-01",
            "chamber": (
                "Camera Deputaților" if doc_type == "plenary_stenogram" else None
            ),
            "session": None,
            "session_type": None,
            "session_date": "2025-04-01",
            "legislature": None,
        },
        "raw_markdown_path": "x.md",
        "raw_pdf_path": "x.pdf",
        "extraction": {
            "extractor": "regex@1",
            "extracted_at": "2025-04-01T12:00:00Z",
            "extractor_versions": {"boilerplate": "0.1.0"},
            "confidence": 0.9,
            "identity": {"record_id": doc_id},
        },
        "coverage": {
            "body_chars": 200,
            "claimed_chars": 200,
            "claimed_pct": 1.0,
            "gaps": [],
            "claimed_by_policy": [],
        },
    }


def _law_ref(*, raw: str, start: int, end: int) -> dict:
    return {
        "type": "law",
        "raw": raw,
        "char_offsets": [start, end],
        "number": "47",
        "year": 1992,
        "subject": None,
    }


def _code_ref(*, raw: str, start: int, end: int, kind: str = "muncii") -> dict:
    return {
        "type": "code",
        "raw": raw,
        "char_offsets": [start, end],
        "code_kind": kind,
        "article": None,
    }


def _bill_ref(*, raw: str, start: int, end: int) -> dict:
    return {
        "type": "bill",
        "raw": raw,
        "char_offsets": [start, end],
        "prefix": "PL-x",
        "number": "100",
        "year": 2025,
        "secondary_year": None,
        "chamber_of_origin": "camera",
        "procedure": None,
        "subject": None,
    }


def _unknown_ref(*, raw: str, start: int, end: int) -> dict:
    return {
        "type": "unknown",
        "raw": raw,
        "char_offsets": [start, end],
        "hint": "law-ish",
    }


def _empty_session() -> dict:
    return {
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
    }


def _per_section_extraction(span: tuple[int, int]) -> dict:
    return {
        "extractor": "regex@1",
        "confidence": 0.9,
        "source_span": {
            "chars": list(span),
            "lines": [1, 2],
            "content_sha": "0123456789ab",
        },
    }


def _agenda_item(
    *,
    primary_refs: list[dict],
    activity: dict | None = None,
    span: tuple[int, int] = (0, 200),
) -> dict:
    return {
        "id": "mo://2025/II/1#agenda-1",
        "content_fingerprint": "0123456789ab",
        "slug": "test-agenda-item-abcdef12",
        "ordinal": 1,
        "title": "Test agenda item",
        "primary_references": primary_refs,
        "category": "bill_debate",
        "confidence_type": None,
        "requested_by_group": None,
        "outcome": None,
        "reexamination_reason": None,
        "pages_in_pdf": [],
        "topics": {"primary": [], "secondary": []},
        "activities": [activity] if activity else [],
        "source_span": {
            "chars": list(span),
            "lines": [1, 5],
            "content_sha": "0123456789ab",
        },
        "extraction": _per_section_extraction(span),
    }


def _speech_activity(
    *,
    refs_mentioned: list[dict],
    span: tuple[int, int],
) -> dict:
    return {
        "id": "mo://2025/II/1#agenda-1#act-1",
        "content_fingerprint": "0123456789ab",
        "slug": "speech-text-abcdef12",
        "type": "speech",
        "speaker": {
            "raw": "Speaker",
            "name": "Speaker",
            "title": None,
            "role": None,
            "party_group": None,
            "person_id": None,
        },
        "delivery_mode": None,
        "text": "speech text",
        "references_mentioned": refs_mentioned,
        "source_span": {
            "chars": list(span),
            "lines": [1, 3],
            "content_sha": "0123456789ab",
        },
        "extraction": _per_section_extraction(span),
    }


def _stenogram_with_agendas(
    *, doc_id: str, agendas: list[dict], schema_version: str = "1.12.0"
) -> dict:
    sc = _envelope(
        doc_id=doc_id,
        doc_type="plenary_stenogram",
        schema_version=schema_version,
    )
    sc["body"] = {
        "session": _empty_session(),
        "agenda_items": agendas,
        "interpellations": [],
    }
    return sc


def _committee_sidecar(*, doc_id: str) -> dict:
    sc = _envelope(doc_id=doc_id, doc_type="committee_synthesis")
    sc["body"] = {
        "period": {"start": "2025-04-01", "end": "2025-04-07"},
        "committees": [],
    }
    return sc


def _write_sidecar(tmp_path: Path, name: str, sidecar: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# -- happy path ------------------------------------------------------------


def test_resolves_unknown_to_anchor_in_same_list(tmp_path: Path):
    """Anchor and unknown in the same primary_references list → resolved."""
    refs = [
        _law_ref(raw="Legii nr. 47/1992", start=0, end=20),
        _unknown_ref(raw="art. 25", start=22, end=29),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/1",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "linked", result.reason
    assert result.resolved == 1
    assert result.unresolved == 0
    after = json.loads(sp.read_text(encoding="utf-8"))
    unknown_ref = after["body"]["agenda_items"][0]["primary_references"][1]
    assert unknown_ref["resolved_to"] == {"char_offsets": [0, 20]}


def test_unresolved_when_only_unknowns_in_list(tmp_path: Path):
    """No non-unknown refs in the list → unknown stays unresolved."""
    refs = [_unknown_ref(raw="art. 25", start=0, end=7)]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/2",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "skip"
    assert "no resolutions" in (result.reason or "")
    assert result.unresolved == 1
    after = json.loads(sp.read_text(encoding="utf-8"))
    unknown_after = after["body"]["agenda_items"][0]["primary_references"][0]
    assert "resolved_to" not in unknown_after


# -- anchor priority --------------------------------------------------------


def test_most_recent_preceding_anchor_wins(tmp_path: Path):
    """When multiple anchors precede the unknown, the closest one wins."""
    refs = [
        _law_ref(raw="Legii nr. 47/1992", start=0, end=20),
        _code_ref(raw="Codul muncii", start=22, end=34),
        _unknown_ref(raw="art. 25", start=40, end=47),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/3",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "linked", result.reason
    after = json.loads(sp.read_text(encoding="utf-8"))
    unknown_ref = after["body"]["agenda_items"][0]["primary_references"][2]
    # Code wins because it's the most-recent preceding anchor
    assert unknown_ref["resolved_to"] == {"char_offsets": [22, 34]}


def test_code_beats_law_at_same_end_offset(tmp_path: Path):
    """Tie-break: when two anchors end at the same offset, code wins."""
    refs = [
        _law_ref(raw="Legii nr. 47/1992", start=0, end=20),
        _code_ref(raw="Codul muncii", start=2, end=20, kind="muncii"),
        _unknown_ref(raw="art. 25", start=22, end=29),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/4",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "linked", result.reason
    after = json.loads(sp.read_text(encoding="utf-8"))
    unknown_ref = after["body"]["agenda_items"][0]["primary_references"][2]
    assert unknown_ref["resolved_to"] == {"char_offsets": [2, 20]}


def test_only_preceding_anchors_count(tmp_path: Path):
    """An anchor AFTER the unknown is not eligible."""
    refs = [
        _unknown_ref(raw="art. 25", start=0, end=7),
        _law_ref(raw="Legii nr. 47/1992", start=10, end=30),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/AFTER",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "skip"
    assert result.unresolved == 1


# -- idempotency ------------------------------------------------------------


def test_idempotent_re_link_skips_already_resolved(tmp_path: Path):
    refs = [
        _law_ref(raw="Legea nr. 47/1992", start=0, end=18),
        _unknown_ref(raw="art. 25", start=20, end=27),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/5",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    r1 = link_xrefs(sp)
    assert r1.status == "linked"
    assert r1.resolved == 1
    r2 = link_xrefs(sp)
    assert r2.status == "skip"
    assert r2.skipped_already == 1
    assert r2.resolved == 0


def test_force_overwrites_existing_resolved_to(tmp_path: Path):
    unknown = _unknown_ref(raw="art. 25", start=20, end=27)
    unknown["resolved_to"] = {"char_offsets": [9999, 99999]}
    refs = [
        _law_ref(raw="Legea nr. 47/1992", start=0, end=18),
        unknown,
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/6",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)

    # Without force: stale stays
    r1 = link_xrefs(sp, force=False)
    assert r1.status == "skip"
    assert r1.skipped_already == 1
    after_no_force = json.loads(sp.read_text(encoding="utf-8"))
    assert after_no_force["body"]["agenda_items"][0]["primary_references"][1][
        "resolved_to"
    ] == {"char_offsets": [9999, 99999]}
    # With force: rewrites
    r2 = link_xrefs(sp, force=True)
    assert r2.status == "linked"
    assert r2.resolved == 1
    after_force = json.loads(sp.read_text(encoding="utf-8"))
    assert after_force["body"]["agenda_items"][0]["primary_references"][1][
        "resolved_to"
    ] == {"char_offsets": [0, 18]}


def test_force_clears_stale_resolved_to_when_no_anchor(tmp_path: Path):
    """Tighter rules can make a previously-resolved entry no longer
    resolvable. force=True must clear the stale value."""
    unknown = _unknown_ref(raw="art. 25", start=0, end=7)
    unknown["resolved_to"] = {"char_offsets": [42, 60]}  # stale
    refs = [unknown]  # no anchor
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/STALE",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp, force=True)
    assert result.status == "linked"  # we wrote (cleared)
    assert result.resolved == 0
    assert result.unresolved == 1
    assert result.cleared_stale == 1
    after = json.loads(sp.read_text(encoding="utf-8"))
    assert (
        after["body"]["agenda_items"][0]["primary_references"][0]["resolved_to"] is None
    )


def test_dry_run_preserves_disk(tmp_path: Path):
    refs = [
        _law_ref(raw="Legea nr. 47/1992", start=0, end=18),
        _unknown_ref(raw="art. 25", start=20, end=27),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/7",
        agendas=[_agenda_item(primary_refs=refs)],
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    before = sp.read_text(encoding="utf-8")
    result = link_xrefs(sp, write=False)
    assert result.status == "linked"
    assert result.resolved == 1
    assert sp.read_text(encoding="utf-8") == before


# -- schema-version bump on write ------------------------------------------


def test_link_bumps_schema_version_on_write(tmp_path: Path):
    """A 1.11.0 sidecar that gets a successful link must come out at
    the runtime schema version (1.12.0+).  The new schema is fully
    backwards-compatible so the bump is honest."""
    refs = [
        _law_ref(raw="Legea nr. 47/1992", start=0, end=18),
        _unknown_ref(raw="art. 25", start=20, end=27),
    ]
    sc = _stenogram_with_agendas(
        doc_id="mo://2025/II/BUMP",
        agendas=[_agenda_item(primary_refs=refs)],
        schema_version="1.11.0",
    )
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "linked", result.reason
    after = json.loads(sp.read_text(encoding="utf-8"))
    # Schema bumped to 1.12.0 (or whatever SCHEMA_VERSION is now)
    from monitorul_ii.extraction.pipeline import SCHEMA_VERSION

    assert after["schema_version"] == SCHEMA_VERSION


# -- doc-type filter --------------------------------------------------------


def test_skips_committee_synthesis(tmp_path: Path):
    sc = _committee_sidecar(doc_id="mo://2025/II/8")
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "skip"
    assert "not a linkable doc type" in (result.reason or "")


# -- error paths ------------------------------------------------------------


def test_unreadable_sidecar_returns_error(tmp_path: Path):
    p = tmp_path / "missing.extraction.json"
    result = link_xrefs(p)
    assert result.status == "error"
    assert "failed to read" in (result.reason or "")


# -- batch entry point ------------------------------------------------------


def test_link_all_xrefs_yields_one_result_per_linkable(tmp_path: Path):
    refs_a = [
        _law_ref(raw="Legea nr. 47/1992", start=0, end=18),
        _unknown_ref(raw="art. 25", start=20, end=27),
    ]
    refs_b = [_unknown_ref(raw="art. 25", start=0, end=7)]
    sc_a = _stenogram_with_agendas(
        doc_id="mo://2025/II/A", agendas=[_agenda_item(primary_refs=refs_a)]
    )
    sc_b = _stenogram_with_agendas(
        doc_id="mo://2025/II/B", agendas=[_agenda_item(primary_refs=refs_b)]
    )
    sc_c = _committee_sidecar(doc_id="mo://2025/II/C")
    pa = _write_sidecar(tmp_path, "a.extraction.json", sc_a)
    pb = _write_sidecar(tmp_path, "b.extraction.json", sc_b)
    pc = _write_sidecar(tmp_path, "c.extraction.json", sc_c)
    results = list(link_all_xrefs([pa, pb, pc]))
    # Only A and B yield results; C (committee) is filtered out
    assert len(results) == 2
    statuses = {r.status for r in results}
    assert statuses == {"linked", "skip"}


# -- cross-list isolation ---------------------------------------------------


def test_cross_list_anchor_is_NOT_used(tmp_path: Path):
    """An anchor in one list does NOT resolve an unknown in another list.

    Per the v0.1.0 same-list scoping rule (see module docstring §2),
    refs offsets are local to their parent string; cross-list resolution
    would need body-global coordinates which the linker doesn't compute.
    """
    # Agenda's primary_references has the law anchor
    primary_refs = [_law_ref(raw="Legea nr. 47/1992", start=0, end=18)]
    # Speech inside this agenda has the unknown — different list
    speech_refs = [_unknown_ref(raw="art. 25", start=0, end=7)]
    speech = _speech_activity(refs_mentioned=speech_refs, span=(0, 200))
    agenda = _agenda_item(primary_refs=primary_refs, activity=speech, span=(0, 200))
    sc = _stenogram_with_agendas(doc_id="mo://2025/II/ISO", agendas=[agenda])
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "skip"
    assert result.unresolved == 1
    after = json.loads(sp.read_text(encoding="utf-8"))
    speech_after = after["body"]["agenda_items"][0]["activities"][0]
    assert "resolved_to" not in speech_after["references_mentioned"][0]


def test_each_list_resolved_independently(tmp_path: Path):
    """Two lists each carrying their own anchor + unknown both resolve."""
    primary_refs = [
        _law_ref(raw="Legea nr. 47/1992", start=0, end=18),
        _unknown_ref(raw="art. 25", start=20, end=27),
    ]
    speech_refs = [
        _code_ref(raw="Codul muncii", start=0, end=12),
        _unknown_ref(raw="art. 30", start=15, end=22),
    ]
    speech = _speech_activity(refs_mentioned=speech_refs, span=(0, 200))
    agenda = _agenda_item(primary_refs=primary_refs, activity=speech, span=(0, 200))
    sc = _stenogram_with_agendas(doc_id="mo://2025/II/EACH", agendas=[agenda])
    sp = _write_sidecar(tmp_path, "doc.extraction.json", sc)
    result = link_xrefs(sp)
    assert result.status == "linked"
    assert result.resolved == 2
    after = json.loads(sp.read_text(encoding="utf-8"))
    assert after["body"]["agenda_items"][0]["primary_references"][1]["resolved_to"] == {
        "char_offsets": [0, 18]
    }
    assert after["body"]["agenda_items"][0]["activities"][0]["references_mentioned"][1][
        "resolved_to"
    ] == {"char_offsets": [0, 12]}


# -- versioning -------------------------------------------------------------


def test_version_format():
    parts = XREF_LINKER_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


# -- schema validation guard ------------------------------------------------


def test_schema_validation_rejects_bad_resolved_to_shape():
    """Pre-write validation guards against malformed resolved_to."""
    from jsonschema import Draft202012Validator

    from monitorul_ii.extraction.schema import schema_dict

    schema = schema_dict()
    v = Draft202012Validator(
        {**schema["$defs"]["UnknownReference"], "$defs": schema["$defs"]}
    )
    bad = {
        "type": "unknown",
        "raw": "art. 25",
        "char_offsets": [0, 7],
        "hint": "law-ish",
        "resolved_to": "not-an-object",
    }
    assert list(v.iter_errors(bad))


# -- result type ------------------------------------------------------------


def test_xref_link_result_dataclass_shape():
    r = XrefLinkResult(sidecar_path=Path("x"), status="linked", resolved=3)
    assert r.resolved == 3
    assert r.unresolved == 0
    assert r.skipped_already == 0
    assert r.cleared_stale == 0
    assert r.reason is None
