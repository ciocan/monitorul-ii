"""Coverage diagnostics for the extract pipeline.

Coverage is the load-bearing diagnostic for the discovery loop: extract →
measure unaccounted spans → inspect → improve extractor → re-extract. A
`Claim` records a span the extractor accounted for (either by emitting a
real record over it, or by classifying it as known boilerplate). The
complement of all claims is the gap list — the unknown unknowns.

Coverage never gates writes (Q6 from the design discussion). It is a
read-out, not a validator.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, Literal

COVERAGE_VERSION = "0.1.0"

# Below this threshold, an unclaimed span is treated as whitespace / page-
# residue and dropped from `gaps`. Empirically chosen so the scaffolding
# doesn't drown in 1-3 char gaps from inter-paragraph whitespace differences.
DEFAULT_MIN_GAP_CHARS = 20

# Truncation length for `gap.preview` — long enough to recognise a pattern,
# short enough to avoid bloating sidecars when a single doc has dozens of
# gaps during early scaffolding.
PREVIEW_CHARS = 200


ClaimKind = Literal["record", "boilerplate"]


@dataclass(frozen=True)
class Claim:
    """A span the extractor accounted for — either a real record or a
    by-policy boilerplate skip.

    Coordinates are 0-indexed char offsets into the body text (half-open
    `[start, end)`) plus 1-indexed line numbers (inclusive `[a, b]`).
    """

    chars: tuple[int, int]
    lines: tuple[int, int]
    kind: ClaimKind
    reason: str | None = None  # required when kind == "boilerplate"


def line_offsets(text: str) -> list[int]:
    """Precomputed sorted list of `\\n` offsets — feed `line_for_offset`.

    Returned list is just the newline positions (i.e., line `n` ends at
    `offsets[n-1]`); see `line_for_offset` for the indexing convention.
    """
    return [i for i, c in enumerate(text) if c == "\n"]


def line_for_offset(char_offset: int, offsets: list[int]) -> int:
    """1-indexed line number containing the byte at `char_offset`.

    Lines are split at `\\n`; the first line is line 1. A `char_offset`
    pointing past EOF clamps to the last line.
    """
    if char_offset < 0:
        return 1
    return bisect.bisect_left(offsets, char_offset) + 1


def lines_for_range(chars: tuple[int, int], offsets: list[int]) -> tuple[int, int]:
    """1-indexed inclusive line range containing the half-open char span.

    `chars[1] - 1` is used for the upper bound because the half-open
    interval `[start, end)` ends on byte `end-1`; reporting `line_for_offset(end)`
    would over-count when `end` lands on the very next newline.
    """
    start, end = chars
    if end <= start:
        end = start + 1
    return (
        line_for_offset(start, offsets),
        line_for_offset(end - 1, offsets),
    )


def make_record_claim(chars: tuple[int, int], offsets: list[int]) -> Claim:
    return Claim(
        chars=chars,
        lines=lines_for_range(chars, offsets),
        kind="record",
    )


def make_boilerplate_claim(
    chars: tuple[int, int], reason: str, offsets: list[int]
) -> Claim:
    return Claim(
        chars=chars,
        lines=lines_for_range(chars, offsets),
        kind="boilerplate",
        reason=reason,
    )


def _merge_ranges(
    ranges: list[tuple[int, int]], body_chars: int
) -> list[tuple[int, int]]:
    """Sort + union half-open char ranges, clamping to `[0, body_chars)`."""
    clamped: list[tuple[int, int]] = []
    for start, end in ranges:
        s = max(0, start)
        e = min(body_chars, end)
        if e > s:
            clamped.append((s, e))
    clamped.sort()
    out: list[tuple[int, int]] = []
    for s, e in clamped:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _gaps_from_merged(
    merged: list[tuple[int, int]], body_chars: int
) -> list[tuple[int, int]]:
    cursor = 0
    gaps: list[tuple[int, int]] = []
    for s, e in merged:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = e
    if cursor < body_chars:
        gaps.append((cursor, body_chars))
    return gaps


def _is_whitespace_only(s: str) -> bool:
    return not s.strip()


def _preview(text: str, start: int, end: int) -> str:
    snippet = text[start:end].strip()
    snippet = " ".join(snippet.split())  # collapse whitespace runs
    if len(snippet) > PREVIEW_CHARS:
        snippet = snippet[: PREVIEW_CHARS - 1] + "…"
    return snippet


def compute_coverage(
    body_text: str,
    claims: list[Claim],
    *,
    min_gap_chars: int = DEFAULT_MIN_GAP_CHARS,
) -> dict[str, Any]:
    """Build the `coverage` envelope block for a sidecar.

    `claims` is the full list (records + boilerplate) emitted during
    extraction. Output shape matches the schema's `Coverage` definition.
    """
    body_chars = len(body_text)
    offsets = line_offsets(body_text)
    merged = _merge_ranges([c.chars for c in claims], body_chars)
    raw_gaps = _gaps_from_merged(merged, body_chars)

    gaps_out: list[dict[str, Any]] = []
    for s, e in raw_gaps:
        if e - s < min_gap_chars:
            continue
        if _is_whitespace_only(body_text[s:e]):
            continue
        lr = lines_for_range((s, e), offsets)
        gaps_out.append(
            {
                "chars": [s, e],
                "lines": [lr[0], lr[1]],
                "preview": _preview(body_text, s, e),
            }
        )

    by_policy_out: list[dict[str, Any]] = []
    for c in claims:
        if c.kind != "boilerplate":
            continue
        reason = c.reason or "unknown_boilerplate"
        by_policy_out.append(
            {
                "chars": [c.chars[0], c.chars[1]],
                "lines": [c.lines[0], c.lines[1]],
                "reason": reason,
            }
        )

    claimed_chars = sum(e - s for s, e in merged)
    pct = claimed_chars / body_chars if body_chars else 1.0

    return {
        "body_chars": body_chars,
        "claimed_chars": claimed_chars,
        "claimed_pct": round(pct, 6),
        "gaps": gaps_out,
        "claimed_by_policy": by_policy_out,
    }
