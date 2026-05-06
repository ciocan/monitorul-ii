"""Activity dispatcher for plenary agenda items.

2-pass partitioner per Q6 design:

  Pass 1 — coarse partition by speaker headers (`## **NAME:**`)
  Pass 2 — fine refinement within each turn for embedded events
           (votes via votes.detect_votes, narrators, deferrals, procedurals)
  Pass 3 — sort by source_span.chars[0]; assert non-overlap

Output is a list of activity dicts, each matching one of the schema's
discriminated Activity variants:
  speech | vote | procedural | narrator | deferral
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import lines_for_range
from monitorul_ii.extraction.extractors.plenary import votes as votes_mod
from monitorul_ii.extraction.references import parse_mentioned_references
from monitorul_ii.extraction.speakers import (
    extract_delivery_mode,
    make_speaker,
    parse_honorific_speaker,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext


# Speech header: `## **NAME:**` — captures the inner string between **
_SPEECH_HEADER_RE = re.compile(
    r"^##\s+\*\*\s*(?P<inner>[^*\n]+?)\s*\*\*\s*$",
    re.MULTILINE,
)

# Italic block — matches both standalone-line italic paragraphs AND inline
# parenthetical italic markers like `_(Aplauze.)_` that appear mid-sentence.
# Standalone line: `^_<multi-line text>_$`; inline: `_(<...>)_`.
_ITALIC_BLOCK_RE = re.compile(
    r"^_(?P<text>[^_\n]+(?:\s*\n[^_\n]+)*)_\s*$"
    r"|_(?P<inline>\([^)]+\))_",
    re.MULTILINE,
)

# Narrator content discriminators (audience reaction, stage business)
_NARRATOR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bAplauze\b", re.IGNORECASE),
    re.compile(r"\bSe\s+intoneaz[ăa]\b", re.IGNORECASE),
    re.compile(r"\bMurmure\b", re.IGNORECASE),
    re.compile(r"\bVoci\s+din\s+sal[ăa]\b", re.IGNORECASE),
    re.compile(r"\bSe\s+prezint[ăa]\s+(?:materialul|video)", re.IGNORECASE),
    re.compile(r"\bCoboar[ăa]\s+la\s+tribun[ăa]\b", re.IGNORECASE),
    re.compile(r"\bUrc[ăa]\s+la\s+tribun[ăa]\b", re.IGNORECASE),
]

# Procedural content discriminators (session flow control)
_PROCEDURAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bPauz[ăa]\b", re.IGNORECASE),
    re.compile(
        r"\b(?:relu[ăa]m|continu[ăa]m)\s+(?:ședin[țt]a|lucr[ăa]rile)", re.IGNORECASE
    ),
    re.compile(
        r"\bSuspend(?:[ăa]m?|a[țt]i|am|au)?\s+(?:ședin[țt]a|lucr[ăa]rile)",
        re.IGNORECASE,
    ),
    re.compile(r"\bSe\s+ridic[ăa]\s+ședin[țt]a\b", re.IGNORECASE),
]

# Standalone deferral phrase (not associated with a vote-open)
_BARE_DEFERRAL_RE = re.compile(
    r"^[^\n]*?Aceasta\s+(?:r[ăa]m[âa]ne|se\s+va\s+da)[^\n]*?votul[uli]?\s+final[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _classify_italic_block(text: str) -> str:
    """Discriminate narrator vs procedural for an italic standalone block.

    Mutually exclusive content patterns; default to narrator (more common).
    """
    for pat in _PROCEDURAL_PATTERNS:
        if pat.search(text):
            return "procedural"
    for pat in _NARRATOR_PATTERNS:
        if pat.search(text):
            return "narrator"
    return "narrator"


def _make_source_span(
    chars: tuple[int, int], offsets: list[int], content_sha: str
) -> dict[str, Any]:
    lr = lines_for_range(chars, offsets)
    return {
        "chars": [chars[0], chars[1]],
        "lines": [lr[0], lr[1]],
        "content_sha": content_sha,
    }


def _make_extraction(
    confidence: float,
    span_chars: tuple[int, int],
    offsets: list[int],
    content_sha: str,
    extractor: str = "regex@plenary@0.1.0",
) -> dict[str, Any]:
    return {
        "extractor": extractor,
        "confidence": round(confidence, 4),
        "source_span": _make_source_span(span_chars, offsets, content_sha),
    }


def _build_speech_activity(
    *,
    speaker: dict[str, Any],
    delivery_mode: str | None,
    text: str,
    span_chars: tuple[int, int],
    offsets: list[int],
    content_sha: str,
) -> dict[str, Any]:
    # Extract references cited mid-speech. char_offsets are local to the
    # speech text fragment (consumers can rebase against source_span if
    # global coordinates are needed).
    references_mentioned = parse_mentioned_references(text) if text else []
    return {
        "type": "speech",
        "speaker": speaker,
        "delivery_mode": delivery_mode,
        "text": text,
        "references_mentioned": references_mentioned,
        "source_span": _make_source_span(span_chars, offsets, content_sha),
        "extraction": _make_extraction(0.85, span_chars, offsets, content_sha),
    }


def _build_narrator_activity(
    *,
    text: str,
    span_chars: tuple[int, int],
    offsets: list[int],
    content_sha: str,
) -> dict[str, Any]:
    return {
        "type": "narrator",
        "text": text,
        "source_span": _make_source_span(span_chars, offsets, content_sha),
        "extraction": _make_extraction(0.9, span_chars, offsets, content_sha),
    }


def _build_procedural_activity(
    *,
    text: str,
    actor: dict[str, Any] | None,
    span_chars: tuple[int, int],
    offsets: list[int],
    content_sha: str,
) -> dict[str, Any]:
    return {
        "type": "procedural",
        "actor": actor,
        "text": text,
        "source_span": _make_source_span(span_chars, offsets, content_sha),
        "extraction": _make_extraction(0.85, span_chars, offsets, content_sha),
    }


def _build_deferral_activity(
    *,
    text: str,
    span_chars: tuple[int, int],
    offsets: list[int],
    content_sha: str,
) -> dict[str, Any]:
    return {
        "type": "deferral",
        "text": text,
        "defers_to": None,
        "source_span": _make_source_span(span_chars, offsets, content_sha),
        "extraction": _make_extraction(0.8, span_chars, offsets, content_sha),
    }


def _build_vote_activity(
    vote_data: dict[str, Any],
    span_chars: tuple[int, int],
    offsets: list[int],
    content_sha: str,
) -> dict[str, Any]:
    """Wrap a vote_data dict from votes.detect_votes into a full activity."""
    activity = dict(vote_data)
    activity["source_span"] = _make_source_span(span_chars, offsets, content_sha)
    activity["extraction"] = _make_extraction(0.8, span_chars, offsets, content_sha)
    return activity


def _parse_speaker_from_inner(inner: str) -> tuple[dict[str, Any], str | None]:
    """Parse speech header inner text into (speaker_dict, delivery_mode)."""
    stripped, mode = extract_delivery_mode(inner)
    # Strip trailing colon if present (e.g., "Domnul X:" → "Domnul X")
    if stripped.endswith(":"):
        stripped = stripped[:-1].strip()
    speaker = parse_honorific_speaker(stripped)
    if not speaker.get("name"):
        # Fallback: use raw stripped string as raw, name as the stripped text
        speaker = make_speaker(raw=stripped, name=stripped or None)
    return speaker, mode


def extract_activities(
    body: str,
    span_start: int,
    span_end: int,
    ctx: "ExtractContext",
) -> list[dict[str, Any]]:
    """Build activities[] for an agenda item span [span_start, span_end).

    Returns a chronologically-sorted, non-overlapping list of activities.
    """
    offsets = ctx.line_offsets
    content_sha = ctx.content_sha
    span_text = body[span_start:span_end]

    # -- Pass 1: partition by speaker headers --------------------------------
    speaker_matches = list(_SPEECH_HEADER_RE.finditer(span_text))
    if not speaker_matches:
        # No speakers in this agenda item — wrap the entire span as an
        # implicit chair-narration speech, then refine for events.
        if span_text.strip():
            speaker = make_speaker(raw="<chair narration>", name=None)
            base_activities = [
                (
                    span_start,
                    span_end,
                    _build_speech_activity(
                        speaker=speaker,
                        delivery_mode=None,
                        text=span_text.strip(),
                        span_chars=(span_start, span_end),
                        offsets=offsets,
                        content_sha=content_sha,
                    ),
                )
            ]
        else:
            return []
    else:
        # Each speaker turn = [header_start, next_header_or_span_end)
        base_activities: list[tuple[int, int, dict[str, Any]]] = []
        for i, sm in enumerate(speaker_matches):
            turn_start = sm.start()
            turn_end = (
                speaker_matches[i + 1].start()
                if i + 1 < len(speaker_matches)
                else len(span_text)
            )
            inner = sm.group("inner")
            speaker, delivery_mode = _parse_speaker_from_inner(inner)
            content = span_text[sm.end() : turn_end].strip()
            global_start = span_start + turn_start
            global_end = span_start + turn_end
            base_activities.append(
                (
                    global_start,
                    global_end,
                    _build_speech_activity(
                        speaker=speaker,
                        delivery_mode=delivery_mode,
                        text=content,
                        span_chars=(global_start, global_end),
                        offsets=offsets,
                        content_sha=content_sha,
                    ),
                )
            )

    # -- Pass 2: fine refinement within each turn ----------------------------
    refined: list[tuple[int, int, dict[str, Any]]] = []
    for turn_start, turn_end, speech in base_activities:
        sub = _refine_turn(turn_start, turn_end, speech, body, ctx)
        refined.extend(sub)

    # -- Pass 3: sort + clip overlapping spans --------------------------------
    # Vote/event span heuristics can produce small overlaps on certain layouts
    # (mostly older docs without paragraph-break formatting). Clip the
    # previous activity's source_span end to the current activity's start so
    # the schema's monotonic-span invariant holds. Last-resort drop the
    # previous activity entirely if clipping would leave it empty.
    refined.sort(key=lambda t: t[0])
    refined = _clip_overlaps(refined)

    return [a for _, _, a in refined]


def _clip_overlaps(
    activities: list[tuple[int, int, dict[str, Any]]],
) -> list[tuple[int, int, dict[str, Any]]]:
    """Walk activities in order; clip prev_end to cur_start when they overlap."""
    if not activities:
        return activities
    out: list[tuple[int, int, dict[str, Any]]] = []
    for s, e, a in activities:
        if out:
            ps, pe, pa = out[-1]
            if s < pe:
                # Clip prev to s; drop prev entirely if that empties it
                if s <= ps:
                    out.pop()
                else:
                    pa["source_span"]["chars"] = [ps, s]
                    out[-1] = (ps, s, pa)
        out.append((s, e, a))
    return out


def _refine_turn(
    turn_start: int,
    turn_end: int,
    speech: dict[str, Any],
    body: str,
    ctx: "ExtractContext",
) -> list[tuple[int, int, dict[str, Any]]]:
    """Refine one speaker turn by splitting around embedded events.

    The speech activity passed in covers the full turn; this fn may
    return:
      - The original speech (unchanged) if no events found
      - Multiple speech fragments + interleaved event activities if events
        are detected
    """
    offsets = ctx.line_offsets
    content_sha = ctx.content_sha
    turn_text = body[turn_start:turn_end]

    # Find embedded events (votes, narrator italic blocks, deferral phrases)
    events: list[tuple[int, int, dict[str, Any]]] = []

    # Votes via state machine
    for vstart, vend, vote_data in votes_mod.detect_votes(turn_text, turn_start):
        events.append(
            (
                vstart,
                vend,
                _build_vote_activity(vote_data, (vstart, vend), offsets, content_sha),
            )
        )

    # Italic blocks (narrator/procedural) — both standalone-line and
    # inline parenthetical forms
    for im in _ITALIC_BLOCK_RE.finditer(turn_text):
        gs = turn_start + im.start()
        ge = turn_start + im.end()
        text = (im.group("text") or im.group("inline") or "").strip()
        if not text:
            continue
        kind = _classify_italic_block(text)
        if kind == "narrator":
            events.append(
                (
                    gs,
                    ge,
                    _build_narrator_activity(
                        text=text,
                        span_chars=(gs, ge),
                        offsets=offsets,
                        content_sha=content_sha,
                    ),
                )
            )
        else:
            events.append(
                (
                    gs,
                    ge,
                    _build_procedural_activity(
                        text=text,
                        actor=None,
                        span_chars=(gs, ge),
                        offsets=offsets,
                        content_sha=content_sha,
                    ),
                )
            )

    # Bare deferral phrases (not inside vote-open windows)
    vote_spans = [(s, e) for s, e, a in events if a.get("type") == "vote"]
    for dm in _BARE_DEFERRAL_RE.finditer(turn_text):
        gs = turn_start + dm.start()
        ge = turn_start + dm.end()
        if any(not (ge <= s or gs >= e) for s, e in vote_spans):
            continue
        events.append(
            (
                gs,
                ge,
                _build_deferral_activity(
                    text=dm.group(0).strip(),
                    span_chars=(gs, ge),
                    offsets=offsets,
                    content_sha=content_sha,
                ),
            )
        )

    if not events:
        # No splits — return the original speech as-is
        return [(turn_start, turn_end, speech)]

    # Sort events by start
    events.sort(key=lambda t: t[0])

    # Split the speech around events
    result: list[tuple[int, int, dict[str, Any]]] = []
    cursor = turn_start
    speaker = speech["speaker"]
    delivery_mode = speech["delivery_mode"]

    # First fragment: span before the first event becomes a speech sub-
    # activity. Holds for both header-present turns (the `## **NAME:**` line
    # is included in this fragment) and implicit-chair turns (no header).
    first_event_start = events[0][0]
    first_text = body[turn_start:first_event_start].strip()
    if first_text:
        result.append(
            (
                turn_start,
                first_event_start,
                _build_speech_activity(
                    speaker=speaker,
                    delivery_mode=delivery_mode,
                    text=first_text,
                    span_chars=(turn_start, first_event_start),
                    offsets=offsets,
                    content_sha=content_sha,
                ),
            )
        )
    cursor = first_event_start

    for i, (es, ee, ea) in enumerate(events):
        # Append the event itself
        result.append((es, ee, ea))
        cursor = ee
        # Speech fragment between this event and next event (or turn end)
        next_start = events[i + 1][0] if i + 1 < len(events) else turn_end
        if next_start > cursor:
            frag_text = body[cursor:next_start].strip()
            if frag_text:
                result.append(
                    (
                        cursor,
                        next_start,
                        _build_speech_activity(
                            speaker=speaker,
                            delivery_mode=delivery_mode,
                            text=frag_text,
                            span_chars=(cursor, next_start),
                            offsets=offsets,
                            content_sha=content_sha,
                        ),
                    )
                )
            cursor = next_start

    return result


__all__ = ["extract_activities"]
