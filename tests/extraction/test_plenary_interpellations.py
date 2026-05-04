"""Unit tests for the plenary interpellation extractor."""

from __future__ import annotations

from datetime import date

from monitorul_ii.extraction.envelope import EnvelopeMeta
from monitorul_ii.extraction.extractors.plenary.interpellations import (
    extract_interpellations,
    find_interpellation_block,
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


# -- boundary detection ---------------------------------------------------


def test_find_block_with_canonical_transition():
    body = (
        "## **Domnul X:**\n\n"
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Doamna Y:**\nInterpellation content.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None
    assert span[0] < span[1]
    assert span[1] == len(body)


def test_find_block_no_transition_returns_none():
    body = "## **Domnul X:**\n\nRegular debate without interpellation block.\n"
    assert find_interpellation_block(body) is None


def test_find_block_intrebari_orale_header():
    body = "Plenary content.\n## **Întrebări orale adresate Guvernului**\nText.\n"
    span = find_interpellation_block(body)
    assert span is not None


def test_find_block_picks_earliest_transition():
    """When multiple transition phrases appear, earliest wins."""
    body = (
        "Pre-content.\n"
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "Some intermediate text.\n"
        "Începem ora interpelărilor.\n"
    )
    span = find_interpellation_block(body)
    assert span is not None
    # Earliest = "Trecem la primirea..."
    assert "primirea" in body[span[0] : span[0] + 100]


# -- per-interpellation parsing ------------------------------------------


def test_extract_interpellations_basic():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Test Senator:**\n\n"
        "Adresez această interpelare Ministerului Educației.\n"
        "Solicit răspuns în scris.\n"
        "Nr. 1.234A\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 1
    interp = interps[0]
    assert interp["genre"] in ("interpelare", "întrebare")
    assert interp["questioner"]["name"] == "Test Senator"
    assert interp["interpellation_number"] == "1.234A"
    assert interp["response_deferred"] is True


def test_extract_interpellations_default_genre_interpelare():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Speaker One:**\n\n"
        "Interpelare adresată ministrului.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["genre"] == "interpelare"


def test_extract_interpellations_intrebare_genre():
    """When 'întrebare' phrasing dominates, genre flips to întrebare."""
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Speaker One:**\n\n"
        "Adresez o întrebare ministrului. Întrebarea mea este simplă.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["genre"] == "întrebare"


def test_extract_interpellations_response_deferred_in_scris():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul Test (în scris):**\n\n"
        "Body of interpellation.\n"
    )
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps[0]["response_deferred"] is True


def test_extract_interpellations_emits_record_claims():
    body = (
        "Trecem la primirea răspunsurilor la interpelări.\n"
        "## **Domnul A:**\n\nInterpellation A.\n"
        "## **Doamna B:**\n\nInterpellation B.\n"
    )
    span = find_interpellation_block(body)
    interps, claims = extract_interpellations(body, span, _ctx(body))
    assert len(interps) == 2
    record_claims = [c for c in claims if c.kind == "record"]
    assert len(record_claims) == 2


def test_extract_interpellations_empty_block():
    """Block with transition but no interpellation headers returns []."""
    body = "Trecem la primirea răspunsurilor la interpelări.\nNo headers here.\n"
    span = find_interpellation_block(body)
    interps, _ = extract_interpellations(body, span, _ctx(body))
    assert interps == []
