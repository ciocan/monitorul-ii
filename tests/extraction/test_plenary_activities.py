"""Unit tests for the plenary activity dispatcher (2-pass partitioner)."""

from __future__ import annotations

from datetime import date

from monitorul_ii.extraction.envelope import EnvelopeMeta
from monitorul_ii.extraction.extractors.plenary.activities import (
    _classify_italic_block,
    extract_activities,
)
from monitorul_ii.extraction.pipeline import ExtractContext


def _ctx(body: str) -> ExtractContext:
    from monitorul_ii.extraction.coverage import line_offsets

    meta = EnvelopeMeta(issue="1", year=2025, part="II", published=date(2025, 1, 1))
    return ExtractContext(
        body_text=body,
        line_offsets=line_offsets(body),
        content_sha="0123456789ab",
        meta=meta,
        frontmatter={},
    )


# -- italic block classifier ----------------------------------------------


def test_classify_aplauze_is_narrator():
    assert _classify_italic_block("(Aplauze.)") == "narrator"


def test_classify_intoneaza_is_narrator():
    assert _classify_italic_block("Se intonează Imnul național") == "narrator"


def test_classify_pauza_is_procedural():
    assert _classify_italic_block("Pauză de masă") == "procedural"


def test_classify_suspend_is_procedural():
    assert _classify_italic_block("Suspendăm ședința") == "procedural"


def test_classify_default_to_narrator():
    """Unknown italic content defaults to narrator (more common)."""
    assert _classify_italic_block("Some unknown italic text") == "narrator"


# -- end-to-end activity extraction ---------------------------------------


def test_extract_activities_single_speaker():
    body = (
        "## **Domnul Test Person:**\n\n"
        "Mulțumesc, domnule președinte. Vorbesc despre proiectul X.\n"
    )
    acts = extract_activities(body, 0, len(body), _ctx(body))
    assert len(acts) == 1
    assert acts[0]["type"] == "speech"
    assert acts[0]["speaker"]["name"] == "Test Person"


def test_extract_activities_two_speakers():
    body = (
        "## **Domnul Speaker One:**\n\n"
        "First speech.\n\n"
        "## **Doamna Speaker Two:**\n\n"
        "Second speech.\n"
    )
    acts = extract_activities(body, 0, len(body), _ctx(body))
    speeches = [a for a in acts if a["type"] == "speech"]
    assert len(speeches) == 2
    assert speeches[0]["speaker"]["name"] == "Speaker One"
    assert speeches[1]["speaker"]["name"] == "Speaker Two"


def test_extract_activities_speech_with_narrator():
    body = (
        "## **Domnul Test:**\n\n"
        "Ne pregătim pentru vot. _(Aplauze.)_\n\n"
        "Continuăm dezbaterea.\n"
    )
    acts = extract_activities(body, 0, len(body), _ctx(body))
    types = [a["type"] for a in acts]
    assert "narrator" in types


def test_extract_activities_speech_with_vote():
    body = (
        "## **Domnul Test:**\n\n"
        "Supun votului proiectul. Să înceapă votul!\n"
        "100 voturi pentru, 5 împotrivă. Adoptat.\n"
    )
    acts = extract_activities(body, 0, len(body), _ctx(body))
    types = [a["type"] for a in acts]
    assert "vote" in types
    # Speech fragments should appear before and/or after the vote
    assert "speech" in types


def test_extract_activities_no_speakers_implicit_chair():
    """final_vote_batch items have no speaker headers — implicit chair wrap."""
    body = (
        "Supunerea la votul final.\n\n"
        "Supun votului proiectul. Să înceapă votul!\n"
        "200 voturi pentru, 10 împotrivă.\n"
    )
    acts = extract_activities(body, 0, len(body), _ctx(body))
    # Should produce at least one speech (implicit chair) + one vote
    types = [a["type"] for a in acts]
    assert "vote" in types
    assert "speech" in types


def test_extract_activities_empty_span():
    acts = extract_activities("", 0, 0, _ctx(""))
    assert acts == []


def test_extract_activities_chronological_order():
    body = (
        "## **Domnul A:**\n\nFirst.\n\n"
        "## **Domnul B:**\n\nSecond.\n\n"
        "## **Doamna C:**\n\nThird.\n"
    )
    acts = extract_activities(body, 0, len(body), _ctx(body))
    starts = [a["source_span"]["chars"][0] for a in acts]
    assert starts == sorted(starts)


def test_extract_activities_non_overlap_invariant():
    """Activities must not overlap — Pass 3 clips small overlaps that escape
    the per-event span heuristics on edge-case layouts. The output guarantees
    monotonic non-overlapping spans regardless of input.
    """
    body = (
        "## **Domnul A:**\n"
        "Supun votului. Să înceapă votul! 100 pentru, 5 împotrivă. Adoptat.\n"
        "_(Aplauze.)_\n"
        "## **Doamna B:**\n"
        "Mulțumesc.\n"
    )
    # Should not raise (clip pass guarantees no overlap)
    acts = extract_activities(body, 0, len(body), _ctx(body))
    for i in range(1, len(acts)):
        prev_end = acts[i - 1]["source_span"]["chars"][1]
        cur_start = acts[i]["source_span"]["chars"][0]
        assert cur_start >= prev_end, (
            f"overlap: prev ends at {prev_end}, cur starts at {cur_start}"
        )


def test_extract_activities_speech_with_references():
    """Speech activities should populate references_mentioned[]."""
    body = "## **Domnul Test:**\n\nDiscutăm Pl-x 100/2025 și OUG nr. 50/2024.\n"
    acts = extract_activities(body, 0, len(body), _ctx(body))
    speeches = [a for a in acts if a["type"] == "speech"]
    refs = speeches[0]["references_mentioned"]
    types = {r["type"] for r in refs}
    assert "bill" in types
    assert "oug" in types


def test_extract_activities_delivery_mode_parsed():
    body = "## **Domnul Test (din sală):**\n\nIntervenție din sală.\n"
    acts = extract_activities(body, 0, len(body), _ctx(body))
    speeches = [a for a in acts if a["type"] == "speech"]
    assert speeches[0]["delivery_mode"] == "from_floor"
