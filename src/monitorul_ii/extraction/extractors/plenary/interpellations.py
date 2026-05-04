"""Interpellation block extractor for plenary_stenogram.

Boundary detection by chair's canonical transition phrase. Block ends at EOF.
Per-interpellation parser extracts questioner / addressed_to / topic /
interpellation_number / response_deferred.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    lines_for_range,
    make_boilerplate_claim,
    make_record_claim,
)
from monitorul_ii.extraction.speakers import (
    extract_delivery_mode,
    parse_honorific_speaker,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext


# -- Transition phrases (block boundary detector) ---------------------------


_TRANSITION_PHRASES: list[re.Pattern[str]] = [
    re.compile(
        r"trecem\s+la\s+primirea\s+r[ăa]spunsurilor\s+la\s+interpel[ăa]ri",
        re.IGNORECASE,
    ),
    re.compile(
        r"începem\s+ora\s+(?:întreb[ăa]rilor\s+)?(?:[șs]i\s+)?interpel[ăa]rilor",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:vom\s+)?intr[ăa]m?\s+în\s+ora\s+(?:întreb[ăa]rilor|interpel[ăa]rilor)",
        re.IGNORECASE,
    ),
    re.compile(r"trecem\s+la\s+prezentarea\s+interpel[ăa]rilor\s+noi", re.IGNORECASE),
    re.compile(
        r"^\s*R[ăa]spunsuri\s+la\s+interpel[ăa]ri\s*[:.]?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"^##?\s+\*\*?\s*Întreb[ăa]ri\s+orale", re.IGNORECASE | re.MULTILINE),
]


def find_interpellation_block(body: str) -> tuple[int, int] | None:
    """Return (block_start, block_end=len(body)) or None if no transition found."""
    earliest: int | None = None
    for pat in _TRANSITION_PHRASES:
        m = pat.search(body)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None:
        return None
    return (earliest, len(body))


# -- Per-interpellation parser ----------------------------------------------


# Interpellation header — `## **<inner>:**` shape; same regex as activities
_INTERP_HEADER_RE = re.compile(
    r"^##\s+\*\*\s*(?P<inner>[^*\n]+?)\s*\*\*\s*$",
    re.MULTILINE,
)


# Genre detection — `interpelare` and `întrebare` heuristics
_INTERPELARE_HINTS = re.compile(r"interpel[ăa]r", re.IGNORECASE)
_INTREBARE_HINTS = re.compile(r"întreb[ăa]r", re.IGNORECASE)


# Addressed_to detection — first sentence of questioner block
_ADDRESSED_TO_RE = re.compile(
    r"adresat[ăa]?\s+(?:doamnei|domnului)\s+(?P<role>[^.,\n]+?)|"
    r"adresez(?:[ăa])?\s+(?:aceast[ăa]\s+)?(?:întrebare|interpelare)\s+"
    r"(?:doamnei|domnului)\s+(?P<role2>[^.,\n]+?)|"
    r"Ministerului\s+(?P<ministry>[^.,\n]+)",
    re.IGNORECASE,
)

# Interpellation number — same regex as qr's
_INTERPELLATION_NUMBER_RE = re.compile(
    r"\bNr\.\s*(?P<num>\d+(?:\.\d+)*[A-Za-z]?)", re.IGNORECASE
)

# Response_deferred markers
_RESPONSE_DEFERRED_RE = re.compile(
    r"\(\s*în\s+scris\s*\)|răspuns(?:ul)?\s+în\s+scris", re.IGNORECASE
)


def _parse_interpellation(
    block_text: str,
    block_start: int,
    inner: str,
    interp_start: int,
    interp_end: int,
    next_responder_start: int | None,
) -> dict[str, Any]:
    """Parse one interpellation block into the schema shape."""
    questioner_text = block_text[interp_start:interp_end]
    inner_text, _ = extract_delivery_mode(inner)
    if inner_text.endswith(":"):
        inner_text = inner_text[:-1].strip()
    questioner = parse_honorific_speaker(inner_text)

    # Genre — default interpelare
    genre = "interpelare"
    if _INTREBARE_HINTS.search(questioner_text) and not _INTERPELARE_HINTS.search(
        questioner_text
    ):
        genre = "întrebare"

    # addressed_to
    addressed_to: str | None = None
    am = _ADDRESSED_TO_RE.search(questioner_text)
    if am:
        addressed_to = am.group("role") or am.group("role2") or am.group("ministry")
        if addressed_to:
            addressed_to = addressed_to.strip()
            if not addressed_to.lower().startswith("ministerul") and am.group(
                "ministry"
            ):
                addressed_to = f"Ministerul {addressed_to}"

    # interpellation_number — last match wins (matching qr pattern)
    nm_matches = list(_INTERPELLATION_NUMBER_RE.finditer(questioner_text))
    interpellation_number = nm_matches[-1].group("num") if nm_matches else None

    # response_deferred
    response_deferred = bool(_RESPONSE_DEFERRED_RE.search(questioner_text))

    # topic — first non-empty line of body after questioner header
    body_lines = questioner_text.splitlines()[1:]
    topic: str | None = None
    for line in body_lines:
        line = line.strip()
        if line and not line.startswith("_") and not line.startswith("#"):
            topic = line[:300]
            break

    # response: null in v0.1 (response detection is downstream work)
    response = None

    return {
        "genre": genre,
        "questioner": questioner,
        "addressed_to": addressed_to,
        "addressed_to_normalized": None,
        "interpellation_number": interpellation_number,
        "topic": topic,
        "question_text": None,
        "response": response,
        "response_deferred": response_deferred,
    }


def extract_interpellations(
    body: str,
    block_span: tuple[int, int],
    ctx: "ExtractContext",
) -> tuple[list[dict[str, Any]], list[Claim]]:
    """Build interpellations[] + emit record/boilerplate claims."""
    offsets = ctx.line_offsets
    content_sha = ctx.content_sha
    block_start, block_end = block_span
    block_text = body[block_start:block_end]
    out: list[dict[str, Any]] = []
    claims: list[Claim] = []

    # Find earliest transition phrase line and emit boilerplate claim for it
    for pat in _TRANSITION_PHRASES:
        m = pat.search(block_text)
        if m:
            line_start = block_start + m.start()
            line_end = block_start + m.end()
            # Extend to end of line
            nl = body.find("\n", line_end)
            if nl != -1 and nl > line_end:
                line_end = nl
            claims.append(
                make_boilerplate_claim(
                    (line_start, line_end),
                    "plenary_stenogram.interpellation_transition",
                    offsets,
                )
            )
            break

    # Find each `## **<inner>:**` header inside the block
    headers = list(_INTERP_HEADER_RE.finditer(block_text))
    if not headers:
        return out, claims

    for i, h in enumerate(headers):
        interp_start = h.start()
        interp_end = headers[i + 1].start() if i + 1 < len(headers) else len(block_text)
        inner = h.group("inner")
        record_dict = _parse_interpellation(
            block_text,
            block_start,
            inner,
            interp_start,
            interp_end,
            None,
        )
        global_start = block_start + interp_start
        global_end = block_start + interp_end
        record_dict["source_span"] = _make_source_span(
            (global_start, global_end), offsets, content_sha
        )
        record_dict["extraction"] = {
            "extractor": "regex@plenary@0.1.0",
            "confidence": 0.7,
            "source_span": _make_source_span(
                (global_start, global_end), offsets, content_sha
            ),
        }
        out.append(record_dict)
        claims.append(make_record_claim((global_start, global_end), offsets))

    return out, claims


def _make_source_span(
    chars: tuple[int, int], offsets: list[int], content_sha: str
) -> dict[str, Any]:
    lr = lines_for_range(chars, offsets)
    return {
        "chars": [chars[0], chars[1]],
        "lines": [lr[0], lr[1]],
        "content_sha": content_sha,
    }


__all__ = ["find_interpellation_block", "extract_interpellations"]
