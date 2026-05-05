"""Vote detector — 5-stage state machine over the agenda-item span.

All canonical Romanian vote-protocol phrases live here (Q5 design lock):

  Stage 1 — Open:     chair triggers a vote
  Stage 2 — Result:   numeric counts line
  Stage 3 — Outcome:  qualifier ("Cu majoritate" / "Cu unanimitate" / "Mulțumesc")
  Stage 4 — Deferral: "rămâne pentru votul final" / cross-session
  Stage 5 — Quorum:   "Nu avem cvorum"

Plus orthogonal detectors on the chair's announce text:
  - _MOTION_TYPE_RULES → 8 enum values; system_check filters the chair's
    pre-vote hardware test
  - _VOTING_METHOD_RULES → 7 enum values; null when chair doesn't restate
"""

from __future__ import annotations

import re
from typing import Any

# -- Stage 1: vote-open phrases ---------------------------------------------


_VOTE_OPEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bSupun(?:em)?\s+votului\b", re.IGNORECASE),
    re.compile(
        r"\bV[ăa]\s+rog\s+s[ăa]\s+v[ăa]\s+preg[ăa]ti[țt]i\s+(?:de|pentru)\s+vot\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bS[ăa]\s+înceap[ăa]\s+votul\b", re.IGNORECASE),
    re.compile(r"\bTrecem\s+la\s+vot\b", re.IGNORECASE),
    re.compile(r"\bV[ăa]\s+rog\s+s[ăa]\s+vota[țt]i\b", re.IGNORECASE),
]


# -- Stage 2: result line ---------------------------------------------------


# Romanian convention: "X voturi pentru, Y împotrivă, Z abțineri[, N nu votează]"
# The fields are positional but flexible — sometimes against is missing
# (implicit 0), sometimes the order is for/abstain/not_voting (skipping
# against). We use a lenient sweep approach, picking off each field with
# its own regex anchored at the result-line start.
_RESULT_PATTERNS = {
    "for": re.compile(
        r"(?:Cu\s+)?(?P<n>\d+(?:\.\d+)*)\s+(?:de\s+)?voturi\s+pentru",
        re.IGNORECASE,
    ),
    "for_unanimous": re.compile(
        r"\bCu\s+unanimitate(?:\s+de\s+voturi)?", re.IGNORECASE
    ),
    "against": re.compile(
        r"(?P<n>\d+(?:\.\d+)*)\s+(?:de\s+)?(?:voturi\s+)?(?:împotrivă|contra)",
        re.IGNORECASE,
    ),
    "abstain": re.compile(
        r"(?P<n>\d+(?:\.\d+)*)\s+(?:de\s+)?ab[țt]iner[ie]+", re.IGNORECASE
    ),
    "not_voting": re.compile(
        r"(?P<n>\d+(?:\.\d+)*)\s*[–\-]?\s*[„\"']?nu\s+vot(?:e[az]|aser[ăa])",
        re.IGNORECASE,
    ),
}


# Outcome qualifier
_OUTCOME_APPROVED_RE = re.compile(
    r"\bCu\s+(?:majoritate|unanimitate)(?:\s+de\s+voturi)?[,\s]*"
    r"[^.]*?(?:a\s+fost\s+(?:aprobat[ăa]?|adoptat[ăa]?))",
    re.IGNORECASE,
)
_OUTCOME_REJECTED_RE = re.compile(r"a\s+fost\s+respin[șs]", re.IGNORECASE)
_UNANIMOUS_SHORTHAND_RE = re.compile(
    r"^\s*Mul[țt]umesc\.?\s*$", re.MULTILINE | re.IGNORECASE
)


# -- Stage 4: deferral ------------------------------------------------------


_DEFERRAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"r[ăa]m[âa]ne\s+pentru\s+votul\s+final", re.IGNORECASE),
    re.compile(
        r"votul\s+final\s+(?:se\s+va\s+da|asupra\s+acestui[^.]*?va\s+fi\s+dat)",
        re.IGNORECASE,
    ),
    re.compile(r"într-o\s+[șs]edin[țt][ăa]\s+viitoare", re.IGNORECASE),
    re.compile(r"se\s+va\s+supune\s+votului\s+final", re.IGNORECASE),
]


# -- Stage 5: quorum failure ------------------------------------------------


_QUORUM_FAIL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Nu\s+avem\s+cvorum", re.IGNORECASE),
    re.compile(r"cvorumul\s+nu\s+este\s+îndeplinit", re.IGNORECASE),
]


# -- Motion type detector ---------------------------------------------------


_MOTION_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "system_check",
        re.compile(
            r"verificare\s+a\s+sistemului\s+de\s+vot|test\s+al\s+sistemului\s+(?:electronic|de\s+vot)",
            re.IGNORECASE,
        ),
    ),
    (
        "agenda_approval",
        re.compile(
            r"Supun(?:em)?\s+votului\s+(?:dumneavoastr[ăa]\s+)?ordinea\s+de\s+zi"
            r"|Supun(?:em)?\s+votului\s+programul\s+de\s+lucru",
            re.IGNORECASE,
        ),
    ),
    (
        "urgency_procedure",
        re.compile(r"procedur[ăa]\s+de\s+urgen[țt][ăa]", re.IGNORECASE),
    ),
    (
        "amendment",
        re.compile(r"\bamendament(?:ul|elor)?\b", re.IGNORECASE),
    ),
    (
        "final",
        re.compile(
            r"votul\s+final\s+asupra|Supunerea\s+la\s+votul\s+final", re.IGNORECASE
        ),
    ),
    (
        "report_approval",
        re.compile(r"raportul\s+comisiei", re.IGNORECASE),
    ),
    (
        "item_adoption",
        re.compile(
            r"Supun(?:em)?\s+votului\s+(?:proiectul|hot[ăa]r[âa]rea|propunerea)",
            re.IGNORECASE,
        ),
    ),
]


def detect_motion_type(announce_text: str) -> str:
    for label, pat in _MOTION_TYPE_RULES:
        if pat.search(announce_text):
            return label
    return "procedural"


# -- Voting method detector -------------------------------------------------


_VOTING_METHOD_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "electronic_remote",
        re.compile(
            r"de\s+la\s+distan[țt][ăa]|prin\s+vot\s+(?:electronic\s+)?remote|"
            r"vot\s+online",
            re.IGNORECASE,
        ),
    ),
    (
        "electronic",
        re.compile(r"vot(?:ul)?\s+electronic|sistemul?\s+electronic", re.IGNORECASE),
    ),
    (
        "secret_ballot",
        re.compile(r"vot\s+secret(?:\s+cu\s+buletine\s+de\s+vot)?", re.IGNORECASE),
    ),
    (
        "nominal",
        re.compile(r"vot\s+nominal", re.IGNORECASE),
    ),
    (
        "show_of_hands",
        re.compile(r"prin\s+ridicare\s+de\s+m[âa]ini?", re.IGNORECASE),
    ),
    (
        "by_acclamation",
        re.compile(r"prin\s+aclama[țt]ii", re.IGNORECASE),
    ),
    (
        "telephone_roll_call",
        re.compile(r"apel\s+telefonic", re.IGNORECASE),
    ),
]


def detect_voting_method(announce_text: str) -> str | None:
    for label, pat in _VOTING_METHOD_RULES:
        if pat.search(announce_text):
            return label
    return None


# -- Result parsing ---------------------------------------------------------


def _parse_int(s: str) -> int | None:
    """Parse `1.234` / `123` to int."""
    s = s.replace(".", "")
    try:
        return int(s)
    except ValueError:
        return None


def parse_result_window(window: str) -> dict[str, Any] | None:
    """Parse a window of text following a vote-open for a result line.

    Returns a counts dict or None if no numeric result found.
    Counts shape:
      { "for": int|"unanimous"|null, "against": int|null,
        "abstain": int|null, "not_voting": int|null,
        "total_voting": int|null }
    """
    counts: dict[str, Any] = {
        "for": None,
        "against": None,
        "abstain": None,
        "not_voting": None,
        "total_voting": None,
    }
    found_any = False

    # for: numeric or unanimous
    fm = _RESULT_PATTERNS["for"].search(window)
    if fm:
        counts["for"] = _parse_int(fm.group("n"))
        found_any = True
    elif _RESULT_PATTERNS["for_unanimous"].search(window):
        counts["for"] = "unanimous"
        found_any = True

    am = _RESULT_PATTERNS["against"].search(window)
    if am:
        counts["against"] = _parse_int(am.group("n"))
        found_any = True

    bm = _RESULT_PATTERNS["abstain"].search(window)
    if bm:
        counts["abstain"] = _parse_int(bm.group("n"))
        found_any = True

    nv = _RESULT_PATTERNS["not_voting"].search(window)
    if nv:
        counts["not_voting"] = _parse_int(nv.group("n"))
        found_any = True

    if not found_any:
        return None

    # total_voting computable when for/against/abstain are numeric and
    # mutually consistent (chair often doesn't state it; we don't fabricate)
    return counts


def detect_outcome_from_window(window: str, counts: dict[str, Any]) -> str:
    """Map a result window + counts dict to the outcome enum."""
    # Explicit phrases win
    if _OUTCOME_REJECTED_RE.search(window):
        return "rejected"
    if _OUTCOME_APPROVED_RE.search(window):
        return "approved"
    # Numeric heuristic
    f = counts.get("for")
    a = counts.get("against")
    if f == "unanimous":
        return "approved"
    if isinstance(f, int) and isinstance(a, int):
        if f > a:
            return "approved"
        if a > f:
            return "rejected"
        return "tied"
    if isinstance(f, int) and a is None:
        return "approved"  # no opposition stated → approved by default
    return "approved"


# -- Top-level state machine ------------------------------------------------


def _find_next_vote_open(body: str, start: int) -> re.Match[str] | None:
    """Find the next vote-open match in body starting at `start`."""
    best: re.Match[str] | None = None
    for pat in _VOTE_OPEN_PATTERNS:
        m = pat.search(body, start)
        if m and (best is None or m.start() < best.start()):
            best = m
    return best


def _next_speaker_boundary(body: str, start: int) -> int:
    """Find the next `## **NAME:**` speaker header (vote window upper bound).

    Vote-open phrases are NOT used as boundaries — chair sequences like
    `Supun votului... Să înceapă votul!... result` use multiple open-style
    phrases as part of one vote event. Bounding by next-open would cut the
    window before the result line. Speaker change is the unambiguous
    boundary.
    """
    speaker_re = re.compile(r"^##\s+\*\*", re.MULTILINE)
    sm = speaker_re.search(body, start)
    return sm.start() if sm else len(body)


def _extract_announce_text(body: str, vote_open_start: int) -> str:
    """Walk back from vote-open start to find the chair's announce sentence.

    Heuristic: take the last 500 chars before vote-open, drop everything
    before the last `## **` header to limit to current speaker.
    """
    look_back_start = max(0, vote_open_start - 500)
    chunk = body[look_back_start:vote_open_start]
    # Trim to text after last `## **` header in the chunk
    header_idx = chunk.rfind("## **")
    if header_idx != -1:
        # Skip past the header line
        nl = chunk.find("\n", header_idx)
        if nl != -1:
            chunk = chunk[nl + 1 :]
    return chunk.strip()


def detect_votes(
    span_text: str, span_start_offset: int
) -> list[tuple[int, int, dict[str, Any]]]:
    """Walk an agenda-item span looking for vote events.

    Returns list of `(global_start, global_end, vote_data)` tuples where
    vote_data is a partial vote activity dict (without source_span and
    extraction provenance — those get added by the caller).

    The vote spans the text from the announce sentence start to the result
    line end (or deferral phrase end). Multiple votes per agenda item are
    detected sequentially.
    """
    out: list[tuple[int, int, dict[str, Any]]] = []
    cursor = 0
    while cursor < len(span_text):
        open_match = _find_next_vote_open(span_text, cursor)
        if open_match is None:
            break

        # Look-ahead window: until next speaker header, OR the next vote-open
        # phrase that follows a result-line / paragraph break (so back-to-back
        # votes stay separate), OR a 2000-char cap.
        speaker_boundary = _next_speaker_boundary(span_text, open_match.end())
        window_end = min(speaker_boundary, open_match.end() + 2000)
        window = span_text[open_match.end() : window_end]
        # If we find a result line followed by another vote-open, end the
        # window after the result so the next vote is detected separately.
        for _key, pat in _RESULT_PATTERNS.items():
            rm = pat.search(window)
            if rm:
                next_open = _find_next_vote_open(span_text, open_match.end() + rm.end())
                if next_open and next_open.start() < window_end:
                    window_end = min(window_end, next_open.start())
                    window = span_text[open_match.end() : window_end]
                break

        # Announce text (chair's sentence preceding the open)
        announce_text = _extract_announce_text(span_text, open_match.start())
        # Build motion_text from the open phrase + the *rest* of the chair's
        # motion sentence — extends from open_match.end through the next
        # sentence-ending punctuation (`.` / `!` / `?`) or 300 chars.
        sentence_end_re = re.compile(r"[.!?]")
        sm = sentence_end_re.search(span_text, open_match.end())
        sentence_end = (
            sm.end()
            if sm and sm.start() - open_match.end() < 300
            else (open_match.end() + 300)
        )
        sentence_end = min(sentence_end, len(span_text))
        motion_text_chunk = span_text[
            max(0, open_match.start() - 200) : sentence_end
        ].strip()

        # Resolution priority: numeric result > deferral > quorum failure
        counts = parse_result_window(window)
        deferral_match = None
        for pat in _DEFERRAL_PATTERNS:
            m = pat.search(window)
            if m and (deferral_match is None or m.start() < deferral_match.start()):
                deferral_match = m
        quorum_match = None
        for pat in _QUORUM_FAIL_PATTERNS:
            m = pat.search(window)
            if m and (quorum_match is None or m.start() < quorum_match.start()):
                quorum_match = m

        if counts is not None:
            # Live vote with numeric result
            outcome = detect_outcome_from_window(window, counts)
            timing = "live"
            # Find actual result-line end (last sub-match within window)
            result_end = open_match.end()
            for key, pat in _RESULT_PATTERNS.items():
                m = pat.search(window)
                if m:
                    result_end = max(result_end, open_match.end() + m.end())
            # Extend through any trailing outcome qualifier ("Cu majoritate
            # de voturi, ... a fost aprobat") on the SAME line; cap at next
            # newline to avoid swallowing the next paragraph (italic
            # narrator, next chair narration, etc.)
            tail_end = span_text.find("\n", result_end)
            if tail_end == -1:
                tail_end = window_end
            actual_end = min(tail_end, window_end)
        elif deferral_match is not None:
            counts = {
                "for": None,
                "against": None,
                "abstain": None,
                "not_voting": None,
                "total_voting": None,
            }
            outcome = "deferred"
            timing = "deferred"
            actual_end = open_match.end() + deferral_match.end()
        elif quorum_match is not None:
            counts = {
                "for": None,
                "against": None,
                "abstain": None,
                "not_voting": None,
                "total_voting": None,
            }
            outcome = "no_quorum"
            timing = "live"
            actual_end = open_match.end() + quorum_match.end()
        else:
            # Implicit deferral: vote opened but no resolution within window
            counts = {
                "for": None,
                "against": None,
                "abstain": None,
                "not_voting": None,
                "total_voting": None,
            }
            outcome = "deferred"
            timing = "deferred"
            actual_end = window_end

        vote_data = {
            "type": "vote",
            "motion_text": motion_text_chunk[:300] if motion_text_chunk else "",
            "motion_type": detect_motion_type(announce_text + " " + motion_text_chunk),
            "voting_method": detect_voting_method(
                announce_text + " " + motion_text_chunk
            ),
            "timing": timing,
            "counts": counts,
            "outcome": outcome,
            "quorum_announced": None,
            "proposed_by": None,
            "nominal_breakdown": None,
            # Cross-document linker slots (populated by linker.py v0.2.0+;
            # always null/[] at extract time).
            "defers_to": None,
            "resolves": [],
        }
        out.append(
            (
                span_start_offset + open_match.start(),
                span_start_offset + actual_end,
                vote_data,
            )
        )
        cursor = actual_end

    return out


__all__ = [
    "detect_votes",
    "detect_motion_type",
    "detect_voting_method",
    "parse_result_window",
    "detect_outcome_from_window",
]
