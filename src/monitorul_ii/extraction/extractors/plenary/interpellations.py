"""Interpellation block extractor for plenary_stenogram.

Boundary detection by chair's canonical transition phrase. Block ends at EOF.
Per-interpellation parser extracts questioner / addressed_to / topic /
interpellation_number / response_deferred / question_text.

`question_text` (v0.2.0): the policy-substance body of the interpellation,
extracted from the questioner's turn by stripping opening pleasantries,
topic-naming lead-ins, trailing signatures/closures, and capping at
section-break markers (e.g., when the same speaker continues into a
political declaration in the same turn). Returns null when the recovered
body is too short to be meaningful.
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


INTERPELLATIONS_VERSION = "0.2.0"
INTERPELLATIONS_LABEL = f"regex@plenary_interpellations@{INTERPELLATIONS_VERSION}"


# -- Transition phrases (block boundary detector) ---------------------------
#
# Survey of 5551 production MDs (2026-05) showed only 1 doc matched the
# v0.1 pattern set. v0.2 widens coverage with the chair-declares-the-block
# phrases observed across 2009-2025: "Declar deschisă sesiunea/ședința de
# întrebări/interpelări" (most common modern form), "Începem sesiunea de
# întrebări și interpelări" (2009-2017), "Urmează prezentarea/sesiunea de
# interpelări" (2009-era), "Răspunsuri la interpelări." (terse).
#
# Patterns are intentionally chair-declarative — they fire on the chair
# OPENING the session, not on incidental mentions of interpelări in
# regular speech. Anchored line-or-substring as appropriate.


_TRANSITION_PHRASES: list[re.Pattern[str]] = [
    # "trecem la primirea răspunsurilor la interpelări"
    re.compile(
        r"trecem\s+la\s+primirea\s+r[ăa]spunsurilor\s+la\s+interpel[ăa]ri",
        re.IGNORECASE,
    ),
    # "Începem sesiunea/ora de întrebări/interpelări..." (chair opens)
    re.compile(
        r"începem\s+(?:sesiunea|ora)\s+(?:de\s+)?(?:întreb[ăa]r(?:i|ilor)?\s+)?"
        r"(?:[șs]i\s+)?interpel[ăa]r(?:i|ilor)?",
        re.IGNORECASE,
    ),
    # "(vom) intra(m) în ora întrebărilor/interpelărilor"
    re.compile(
        r"(?:vom\s+)?intr[ăa]m?\s+în\s+ora\s+(?:întreb[ăa]rilor|interpel[ăa]rilor)",
        re.IGNORECASE,
    ),
    # "trecem la prezentarea interpelărilor noi"
    re.compile(r"trecem\s+la\s+prezentarea\s+interpel[ăa]rilor\s+noi", re.IGNORECASE),
    # "Declar deschisă sesiunea/ședința [consacrată/de] ... interpelări/întrebări"
    # (chair's canonical opener; 136 hits across the corpus)
    re.compile(
        r"declar\s+deschis[ăa]\s+(?:[șs]edin[țt]a|sesiunea)\s+"
        r"(?:consacrat[ăa]\s+|de\s+|pentru\s+)?[^\n]{0,80}"
        r"(?:interpel[ăa]r|întreb[ăa]r)",
        re.IGNORECASE,
    ),
    # "Urmează prezentarea/sesiunea ... de interpelări/întrebări"
    re.compile(
        r"urmeaz[ăa]\s+(?:prezentarea|sesiunea)\s+"
        r"(?:pe\s+scurt\s+)?(?:de\s+|a\s+)?[^\n]{0,80}"
        r"interpel[ăa]r",
        re.IGNORECASE,
    ),
    # "Deschidem ședința consacrată răspunsurilor orale ..."
    re.compile(
        r"deschidem\s+[șs]edin[țt]a\s+consacrat[ăa]\s+r[ăa]spunsurilor",
        re.IGNORECASE,
    ),
    # "Răspunsuri la interpelări." — chair-anchored line. Allows trailing
    # courtesy phrases ("ale domnilor deputați...") but not table cells
    # (excluded by negative-lookbehind for `|`, common in SUMAR rows).
    re.compile(
        r"(?<![|])^\s*R[ăa]spunsuri\s+la\s+interpel[ăa]ri[ie]?[^\n|]{0,160}\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "## **Întrebări orale ...**" header
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


# -- question_text extraction (v0.2.0) --------------------------------------


# Lines that are pure pleasantries or salutations — drop from the start of
# question_text. Anchored line-only (full match) so substantive sentences
# that *contain* these phrases are preserved.
_PLEASANTRY_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:v[ăa]\s+)?mul[țt]umesc(?:[\s,!.][^\n]*)?|"
    r"bun[ăa]\s+(?:diminea[țt]a|seara|ziua)(?:[\s,!.][^\n]*)?|"
    r"doamn[ăa]\s+pre[șs]edint[eă][^\n]*|"
    r"domnule\s+pre[șs]edint[eă][^\n]*|"
    r"stima[țt]i\s+colegi[^\n]*|"
    r"stimate\s+colege[^\n]*|"
    r"stima[țt]i\s+(?:domni\s+(?:senatori|deputa[țt]i)|senatori|deputa[țt]i)[^\n]*|"
    r"dragi\s+români[^\n]*|"
    r"doamnelor\s+[șs]i\s+domnilor[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)

# Topic-naming preamble lines — the formal "I shall now read out..." opening
# that names the addressed minister + subject. Also covers the older
# "Interpelarea este adresată..." / "Interpelarea se adresează..." form.
# Drop these from the start because the topic + addressee are captured
# separately as their own fields.
_TOPIC_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"voi\s+(?:da\s+curs|prezenta|citi)[^.\n]*"
    r"(?:întreb[ăa]rii|interpel[ăa]rii|întreb[ăa]ri|interpel[ăa]ri)[^\n]*|"
    r"interpelarea\s+(?:este\s+adresat[ăa]|se\s+adreseaz[ăa])[^\n]*|"
    r"întrebarea\s+(?:este\s+adresat[ăa]|se\s+adreseaz[ăa])[^\n]*|"
    r"obiectul\s+interpel[ăa]rii\s*[:.]?\s*[^\n]*|"
    r"obiectul\s+întreb[ăa]rii\s*[:.]?\s*[^\n]*|"
    r"adresez(?:[ăa])?\s+(?:aceast[ăa]\s+)?(?:întrebare|interpelare)[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)

# Closing patterns — cap question_text at the start of the earliest match.
# These signal the speaker is wrapping up (request for written response,
# courtesy close, signature). Anchored loosely so the trailing text
# ("Solicit răspuns în scris, în termen de 15 zile...") is excluded too.
_CLOSING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bSolicit\s+r[ăa]spuns\b", re.IGNORECASE),
    re.compile(r"\bA[șs]tept(?:[ăa]m)?\s+r[ăa]spuns(?:ul)?\b", re.IGNORECASE),
    re.compile(r"\bDoresc\s+un\s+r[ăa]spuns\b", re.IGNORECASE),
    re.compile(r"^\s*Cu\s+stim[ăa]\s*[,.]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Cu\s+respect\s*[,.]", re.IGNORECASE | re.MULTILINE),
]

# Section-break patterns — same speaker continues into a different topic
# (typically a political declaration after the interpellation). Cap
# question_text at the start of the earliest match.
_SECTION_BREAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\bvoi\s+(?:citi|prezenta|continua\s+cu)\s+(?:[șs]i\s+)?"
        r"declara[țt]ia\s+politic[ăa]\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdeclara[țt]ia\s+politic[ăa]\s+(?:cu\s+titlul|cu\s+tema|"
        r"pe\s+care\s+(?:vreau|doresc))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:trec(?:em)?|voi\s+trece)\s+(?:acum\s+)?la\s+"
        r"(?:a\s+doua|cea\s+de-a\s+doua|cealalt[ăa])\s+"
        r"(?:întrebare|interpelare)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bvoi\s+citi\s+[șs]i\s+declara[țt]ia\b", re.IGNORECASE),
]


_MIN_QUESTION_TEXT_CHARS = 40


def _extract_question_text(turn_body: str) -> str | None:
    """Return the policy-substance text of an interpellation turn, or None.

    `turn_body` is the post-header content (everything after the
    `## **NAME:**` line). Strategy:
      1. Cap at the earliest section-break (next political declaration,
         next interpellation in same turn).
      2. Cap at the earliest closing pattern ("Solicit răspuns", "Cu stimă,"...).
      3. Drop leading pleasantry / topic-preamble lines.
      4. Trim and reject if shorter than `_MIN_QUESTION_TEXT_CHARS`
         (very short residue = boilerplate stripped, no real content).

    Paragraph breaks inside the body are preserved (collapsed to single
    blank lines). Returns None when no meaningful body remains.
    """
    text = turn_body

    cap = len(text)
    for pat in _SECTION_BREAK_PATTERNS:
        m = pat.search(text)
        if m and m.start() < cap:
            cap = m.start()
    for pat in _CLOSING_PATTERNS:
        m = pat.search(text, 0, cap)
        if m and m.start() < cap:
            cap = m.start()
    text = text[:cap]

    lines = text.splitlines()
    while lines:
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        if (
            _PLEASANTRY_LINE_RE.match(line)
            or _TOPIC_PREAMBLE_RE.match(line)
            or line.startswith("#")
            or line.startswith("_")
        ):
            lines.pop(0)
            continue
        break

    while lines and not lines[-1].strip():
        lines.pop()

    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    if len(cleaned) < _MIN_QUESTION_TEXT_CHARS:
        return None
    return cleaned


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

    # question_text — body content of the questioner's turn (post-header),
    # with pleasantries / topic-preamble / closing / section-breaks removed.
    turn_body = "\n".join(body_lines)
    question_text = _extract_question_text(turn_body)

    # response: null in v0.1 (response detection is downstream work)
    response = None

    return {
        "genre": genre,
        "questioner": questioner,
        "addressed_to": addressed_to,
        "addressed_to_normalized": None,
        "interpellation_number": interpellation_number,
        "topic": topic,
        "question_text": question_text,
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
        confidence = 0.85 if record_dict.get("question_text") else 0.7
        record_dict["extraction"] = {
            "extractor": INTERPELLATIONS_LABEL,
            "confidence": confidence,
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


__all__ = [
    "INTERPELLATIONS_VERSION",
    "INTERPELLATIONS_LABEL",
    "find_interpellation_block",
    "extract_interpellations",
]
