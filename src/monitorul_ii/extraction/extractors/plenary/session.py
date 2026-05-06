"""Session envelope extractor for plenary_stenogram.

Owns the body's pre-first-speaker span and body-suffix span. Detects:

  - opened_at      — italic `Ședința a început la ora HH:MM` (also `HH.MM`)
  - chair_segments — italic chair-narrative paragraphs (single or multi-
                     segment via "în prima parte" / "Ultima parte" markers)
  - chair[]        — union of chair_segments[].chair
  - secretaries[]  — union of chair_segments[].secretaries
  - attendance     — chair's first announce: "din totalul de N..., M"
  - quorum_met     — derived: registered >= ceil(total_seats / 2)
  - format         — body markers `prin mijloace electronice` / `format mixt`
                     / `în format fizic`; default `in_person` for pre-2020
                     when no marker (pandemic-era added the marker)
  - closed_at      — italic `Ședința s-a încheiat la ora HH:MM` near body end
  - outcome        — closing phrase mapping (Declar închisă → completed,
                     Suspend ședința pentru lipsa cvorumului → suspended_no_quorum,
                     etc.); default `completed` if closed_at parsed but no
                     phrase matches
  - special_procedure — two-pass: header-marker detection + post-agenda
                     derivation (deferred to orchestrator since it needs
                     agenda_items[] in hand)

Plenary-specific boilerplate (SUMAR table, STENOGRAMA marker, "Ședința din
ziua de" header) is claimed here as `plenary_stenogram.<thing>` boilerplate.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    make_boilerplate_claim,
    make_record_claim,
)
from monitorul_ii.extraction.speakers import (
    PARLIAMENTARY_TITLE_RE,
    make_speaker,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext


# -- Time + opened_at / closed_at -------------------------------------------


# Time separator: `:`, `.`, or `,` — pre-2008 docs use `13,25` style; modern
# docs use `13.25` or `13:25`. The body word `Ședin[țt]a` carries Romanian
# diacritics in modern docs, but older PDFs sometimes ship with cedilla
# (`Ședinţa`) or mojibake — v0.2.9 covers the full PostScript/Latin-1
# substitution set we observed in 2000–2007 era PDFs:
#   Ș → Ș Ş ª ™     (uppercase mojibake — modern, cedilla, OEM, PostScript)
#   ș → ș ş º ∫     (lowercase — last is integral-sign mojibake from 2004+)
#   ț → ț ţ þ ˛     (lowercase — last is combining ogonek from 2004+)
#   î → î Ó Œ Ñ     (mojibake of `î` and uppercase `Î` — both render as Ó)
#   ă → ă ã „        (last is double-low-9 quote — only added when bounded
#                     by Romanian-letter context to avoid quote false-pos)
_TIME_SEP = r"[.,:]"
_SEDINTA_VARIANTS = r"(?:[ŞȘª™S]edin[țtţþ˛]a|[Ss]edinta)"
# `început` / `încheiat` mojibake forms — `î` and `Î` both render as `Ó`/`Œ`/`Ñ`
# in 2004+ PostScript docs.
_INCEPUT = r"[îÓŒÑ]nceput"
_INCHEIAT = r"[îÓŒÑ]ncheiat"
# Present-tense forms used in 2000-era docs: "Ședința începe la ora ..."
# (vs. modern perfect "a început") and "se încheie" (vs. "s-a încheiat").
_OPEN_VERB = rf"(?:a\s+{_INCEPUT}|[îÓŒÑ]ncepe)"
_CLOSE_VERB = rf"(?:s-a\s+{_INCHEIAT}|se\s+[îÓŒÑ]ncheie)"

_OPENED_AT_RE = re.compile(
    rf"_?\s*[Ss]edinta\s+{_OPEN_VERB}\s+la\s+ora\s+"
    rf"(?P<h>\d{{1,2}}){_TIME_SEP}(?P<m>\d{{2}})",
    re.IGNORECASE,
)
_OPENED_AT_DIACRITICS_RE = re.compile(
    rf"_?\s*{_SEDINTA_VARIANTS}\s+{_OPEN_VERB}\s+la\s+ora\s+"
    rf"(?P<h>\d{{1,2}}){_TIME_SEP}(?P<m>\d{{2}})",
    re.IGNORECASE,
)
_CLOSED_AT_RE = re.compile(
    rf"{_SEDINTA_VARIANTS}\s+{_CLOSE_VERB}\s+la\s+ora\s+"
    rf"(?P<h>\d{{1,2}}){_TIME_SEP}(?P<m>\d{{2}})",
    re.IGNORECASE,
)


def _parse_time_match(m: re.Match[str]) -> str:
    h = int(m.group("h"))
    mn = int(m.group("m"))
    return f"{h:02d}:{mn:02d}"


# -- Chair-narrative italic block --------------------------------------------


# Find the chair-narrative paragraph block. Italic markers on each line; the
# block typically starts with "Lucrările au fost conduse..." Multi-segment
# blocks include multiple paragraphs separated by `_..._` boundaries.
#
# Variants observed across the corpus:
#   - Modern (2015+):           "Lucrările au fost conduse de domnul …"
#   - 2008-era:                 "Lucrările ședinței au fost conduse de …"
#   - Some 2008+ joint docs:    "Ședința a fost condusă, în prima parte, de …"
#   - Mojibake (pre-2008):      "Lucrãrile au fost conduse" (ã not ă),
#                               "ªedinţa a fost condusã"
_CHAIR_BLOCK_OPENING_RE = re.compile(
    r"_\s*(?:"
    r"Lucr[ăaãâ„]rile\s+(?:[șsşº∫]edin[țtţþ˛]ei\s+)?(?:au\s+fost\s+conduse|sunt\s+conduse)"
    r"|[ŞȘª™S]edin[țtţþ˛]a\s+a\s+fost\s+condus[ăaã„]"
    r")",
    re.IGNORECASE,
)

# Markers that split the chair-narrative into segments
_SEGMENT_MARKERS = [
    (re.compile(r"\b[îÓŒÑ]n\s+prima\s+parte\b", re.IGNORECASE), "prima parte"),
    (
        re.compile(r"\b[îÓŒÑ]n\s+(?:cea\s+de-)?a\s+doua\s+parte\b", re.IGNORECASE),
        "a doua parte",
    ),
    (
        re.compile(r"\b[Aa]\s+doua\s+parte\s+a\s+[șsşº∫]edin[țtţþ˛]ei", re.IGNORECASE),
        "a doua parte",
    ),
    (
        re.compile(r"\b[Uu]ltima\s+parte\s+a\s+[șsşº∫]edin[țtţþ˛]ei", re.IGNORECASE),
        "ultima parte",
    ),
]

# Regex to find each "domnul deputat NAME, role" / "doamna senator NAME, role"
# inside a chair-narrative paragraph. Stops at common terminators (next
# coordinating conjunction, next honorific, end of clause). Rank
# (deputat/senator) is OPTIONAL — pre-2010 docs and joint sessions
# routinely use the bare honorific form ("domnul Nicolae Văcăroiu,
# președintele Senatului").
_CHAIR_PERSON_RE = re.compile(
    r"\b(?P<honorific>domnul|doamna)\s+"
    r"(?:(?P<rank>deputat|senator)\s+)?"
    r"(?P<name>[A-ZȘȚÂÎĂ][\w\-]+(?:\s+[A-ZȘȚÂÎĂ][\w\-]+){0,4})"
    r"(?:\s*,\s*(?P<role>[^,_;]+?(?:Camerei|Senatului|Deputa[țtţþ˛]ilor)[^,_;]*?))?",
    re.UNICODE,
)


def _find_chair_block(body: str) -> tuple[int, int] | None:
    """Locate the chair-narrative italic block.

    Block runs from the opening "_Lucrările au fost conduse..." line until
    just before the first `## **NAME:**` speaker header (the chair's first
    speech turn), bounded to a max ~3000 char window so we don't accidentally
    swallow agenda content.
    """
    m = _CHAIR_BLOCK_OPENING_RE.search(body)
    if m is None:
        return None
    start = m.start()
    # End: before first speaker header or 3000 chars later
    speaker_re = re.compile(r"^##\s+\*\*", re.MULTILINE)
    speaker_match = speaker_re.search(body, m.end())
    if speaker_match:
        end = speaker_match.start()
    else:
        end = min(len(body), m.end() + 3000)
    return start, end


def _split_into_segments(block_text: str) -> list[tuple[str, str | None]]:
    """Split a chair-narrative block into (segment_text, segment_label) pairs.

    If no segment markers are found, returns one (block_text, None) tuple
    representing a single-segment session.
    """
    # Find all marker positions
    matches: list[tuple[int, str]] = []
    for pat, label in _SEGMENT_MARKERS:
        for m in pat.finditer(block_text):
            matches.append((m.start(), label))
    matches.sort()

    if not matches:
        return [(block_text, None)]

    # Split at marker positions; each segment owns text from previous
    # marker to next marker
    segments: list[tuple[str, str | None]] = []
    # Walk paragraphs (rough proxy: split on double newline)
    paragraphs = re.split(r"\n\s*\n", block_text)
    for para in paragraphs:
        if not para.strip():
            continue
        label = None
        for pat, lbl in _SEGMENT_MARKERS:
            if pat.search(para):
                label = lbl
                break
        segments.append((para, label))
    if not segments:
        return [(block_text, None)]
    return segments


def _parse_chair_persons(
    text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse a chair-narrative paragraph into (chairs, secretaries).

    The paragraph follows the pattern:
        "Lucrările au fost conduse [, în prima parte,] de
         domnul deputat NAME1, ROLE1, [și de] domnul deputat NAME2, ROLE2,
         asistat[ăți] de doamna deputat NAME3 [, ...]"

    Heuristic: find the "asistat[ăți] de" boundary; persons before it are
    chairs, persons after it are secretaries.
    """
    boundary_re = re.compile(r"\basista[țt][i]?\s+de\b", re.IGNORECASE)
    bm = boundary_re.search(text)
    if bm:
        chair_text = text[: bm.start()]
        secretary_text = text[bm.end() :]
    else:
        chair_text = text
        secretary_text = ""

    def _persons(s: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for m in _CHAIR_PERSON_RE.finditer(s):
            rank = m.group("rank")
            role = m.group("role")
            # Without an explicit rank, only treat the match as a chair-person
            # when a chamber-bearing role suffix is present — otherwise plain
            # `domnul X` mentions in the chair-narrative span over-fire.
            if rank is None and not role:
                continue
            name = m.group("name").strip()
            if name in seen:
                continue
            seen.add(name)
            role_clean = re.sub(r"\s+", " ", role.strip()) if role else None
            raw_segment = re.sub(r"\s+", " ", m.group(0).strip())
            # Title: explicit rank when present; otherwise infer from role
            # suffix (`președintele Senatului` → senator).
            if rank is not None:
                title = rank.lower()
            elif role_clean and "Senatului" in role_clean:
                title = "senator"
            elif role_clean and ("Camerei" in role_clean or "Deputa" in role_clean):
                title = "deputat"
            else:
                title = None
            out.append(
                make_speaker(
                    raw=raw_segment,
                    name=name,
                    title=title,
                    role=role_clean,
                )
            )
        return out

    return _persons(chair_text), _persons(secretary_text)


def _detect_started_at(segment_text: str) -> str | None:
    """Look for `la ora HH:MM` inside the segment text (mid-session swaps
    sometimes carry their own start time)."""
    m = re.search(
        r"la\s+ora\s+(?P<h>\d{1,2})[.:](?P<m>\d{2})",
        segment_text,
        re.IGNORECASE,
    )
    if m:
        return _parse_time_match(m)
    return None


def _build_chair_segments(block_text: str) -> list[dict[str, Any]]:
    segments = _split_into_segments(block_text)
    out: list[dict[str, Any]] = []
    for seg_text, label in segments:
        chairs, secretaries = _parse_chair_persons(seg_text)
        for chair in chairs:
            out.append(
                {
                    "chair": chair,
                    "secretaries": list(secretaries),
                    "segment_label": label,
                    "started_at": _detect_started_at(seg_text),
                }
            )
    return out


# -- Attendance --------------------------------------------------------------


# "din totalul de N deputați și senatori, ... și-au înregistrat prezența M"
# "din totalul celor N de deputați, ... până în acest moment, M"
# "din totalul de N senatori, ... M"
# Mojibake variants: deputaþi (þ→ț), prezenþa (þ→ț), ºi (º→ș), si-au înregistrat
_ATTENDANCE_RE = re.compile(
    r"din\s+totalul\s+(?:celor\s+)?(?:de\s+)?(?P<total>\d+)\s+(?:de\s+)?"
    r"(?:deputa[țtţþ˛]i|senatori|deputa[țtţþ˛]i\s+[șsşº∫]i\s+senatori)[^.]*?"
    r"(?:[șs∫]i-au\s+[îÓŒÑ]nregistrat\s+prezen[țtţþ˛]a|"
    r"[îÓŒÑ]n\s+acest\s+moment[,]?\s*[șs∫]i-au\s+[îÓŒÑ]nregistrat\s+prezen[țtţþ˛]a|"
    r"prezen[țtţþ˛]a)[^.\n]*?"
    r"(?P<registered>\d+)",
    re.IGNORECASE | re.DOTALL,
)


def _detect_attendance(body: str) -> tuple[dict[str, int | None], int | None]:
    """Return (attendance_dict, end_offset).

    end_offset is the body position past the matched announcement so the
    extractor can credit a record claim over that span.
    """
    m = _ATTENDANCE_RE.search(body)
    if not m:
        return {"registered": None, "total_seats": None}, None
    total = int(m.group("total"))
    registered = int(m.group("registered"))
    return ({"registered": registered, "total_seats": total}, m.end())


# -- Format -----------------------------------------------------------------


_FORMAT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bformat\s+mixt(?:\s+de\s+prezen[țt][ăa])?", re.IGNORECASE), "mixed"),
    (re.compile(r"\bprin\s+mijloace\s+electronice\b", re.IGNORECASE), "mixed"),
    (re.compile(r"\bonline\s+(?:[șs]i\s+)?fizic\b", re.IGNORECASE), "mixed"),
    (re.compile(r"\bnumai\s+online\b|\bdoar\s+online\b", re.IGNORECASE), "online"),
    (
        re.compile(
            r"\bîn\s+format\s+fizic\b|\ben\s+prezen[țt]a\s+fizic[ăa]\b", re.IGNORECASE
        ),
        "in_person",
    ),
]


def _detect_format(body: str, year: int | None) -> str | None:
    for pat, fmt in _FORMAT_PATTERNS:
        if pat.search(body):
            return fmt
    # Pre-2020 default: in_person (pandemic-era added the marker universally)
    if year is not None and year < 2020:
        return "in_person"
    return None


# -- Closing / outcome ------------------------------------------------------


_CLOSED_PHRASE_RE = re.compile(
    r"Declar\s+[îÓŒÑ]nchis[ăaã„]\s+[șsşº∫]edin[țtţþ˛]a", re.IGNORECASE
)
_SUSPEND_NO_QUORUM_RE = re.compile(
    r"[Ss]uspend\s+[șsşº∫]edin[țtţþ˛]a\s+pentru\s+lipsa\s+cvorumului",
    re.IGNORECASE,
)
_SUSPEND_OTHER_RE = re.compile(r"[Ss]uspend\s+[șsşº∫]edin[țtţþ˛]a\b", re.IGNORECASE)
_ADJOURNED_RE = re.compile(
    r"[Șșª™]edin[țtţþ˛]a\s+continu[ăaã„]\s+m[âaã]ine|"
    r"se\s+va\s+relua\s+m[âaã]ine",
    re.IGNORECASE,
)


def _detect_outcome(body: str, closed_at: str | None) -> str | None:
    if _SUSPEND_NO_QUORUM_RE.search(body):
        return "suspended_no_quorum"
    if _CLOSED_PHRASE_RE.search(body):
        return "completed"
    if _ADJOURNED_RE.search(body):
        return "adjourned"
    if _SUSPEND_OTHER_RE.search(body):
        return "suspended_other"
    if closed_at is not None:
        return "completed"
    return None


# -- Special procedure ------------------------------------------------------


_SPECIAL_PROCEDURE_HEADER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"[Șș]edin[țt][ăa]\s+solemn[ăa]\s+(?:comun[ăa]\s+)?consacrat[ăa]",
            re.IGNORECASE,
        ),
        "sedinta_solemna",
    ),
    (
        re.compile(
            r"[Dd]eschiderea\s+sesiunii\s+(?:ordinare|extraordinare)",
            re.IGNORECASE,
        ),
        "deschiderea_sesiunii",
    ),
    (
        re.compile(r"[Dd]eschiderea\s+legislaturii", re.IGNORECASE),
        "deschiderea_legislaturii",
    ),
    (
        re.compile(
            r"[Mm]esaj(?:ul)?\s+al?\s+Pre[șs]edintelui\s+Rom[âa]niei|"
            r"[Dd]iscurs(?:ul)?\s+al?\s+Pre[șs]edintelui\s+Rom[âa]niei",
            re.IGNORECASE,
        ),
        "mesaj_prezidential",
    ),
]


def detect_special_procedure_from_header(body: str) -> str | None:
    """First pass: scan body header for an explicit special-procedure marker.

    Returns the schema enum value or None.
    """
    for pat, value in _SPECIAL_PROCEDURE_HEADER_PATTERNS:
        if pat.search(body[:5000]):  # only header
            return value
    return None


# -- SUMAR boundary ---------------------------------------------------------


# SUMAR keyword variants: bare `SUMAR`, markdown-prefixed `## SUMAR` (modern
# layouts where PyMuPDF promotes it to a heading), and pipe-prefixed
# `|SUMAR<br>...` (some 2007-era docs render the keyword inside the table cell).
_SUMAR_OPENING_RE = re.compile(
    r"^(?:##?\s+)?SUMAR\s*$|^\|SUMAR(?=<br>|\|)",
    re.MULTILINE,
)


def find_sumar_span(body: str) -> tuple[int, int] | None:
    """Locate the SUMAR table — start of the SUMAR keyword line through the
    end of the markdown table (next `##` header or `_Ședința` italic).

    Returns (start, end) or None.
    """
    m = _SUMAR_OPENING_RE.search(body)
    if m is None:
        return None
    start = m.start()
    # End: first `_Ședința a început` italic (incl. mojibake/cedilla variants)
    # or first `##` header after start
    end_re = re.compile(
        rf"_\s*{_SEDINTA_VARIANTS}\s+a\s+{_INCEPUT}|^##\s",
        re.MULTILINE,
    )
    end_match = end_re.search(body, m.end())
    end = end_match.start() if end_match else min(len(body), m.end() + 50000)
    return start, end


# -- Compute quorum ---------------------------------------------------------


def _compute_quorum_met(registered: int | None, total: int | None) -> bool | None:
    if registered is None or total is None or total == 0:
        return None
    return registered >= math.ceil(total / 2)


# -- Public API -------------------------------------------------------------


def extract_session(
    body: str, ctx: "ExtractContext"
) -> tuple[dict[str, Any], list[Claim]]:
    """Build the session dict + emit boilerplate/record claims.

    `body` is the full body string (post-frontmatter); offsets are global
    to body.
    """
    offsets = ctx.line_offsets
    claims: list[Claim] = []

    # opened_at — first match wins
    opened_at: str | None = None
    for pat in (_OPENED_AT_DIACRITICS_RE, _OPENED_AT_RE):
        m = pat.search(body)
        if m:
            opened_at = _parse_time_match(m)
            claims.append(make_record_claim((m.start(), m.end()), offsets))
            break

    # chair narrative block
    chair_segments: list[dict[str, Any]] = []
    chairs: list[dict[str, Any]] = []
    secretaries: list[dict[str, Any]] = []
    cb = _find_chair_block(body)
    if cb is not None:
        block_text = body[cb[0] : cb[1]]
        segs = _build_chair_segments(block_text)
        chair_segments = segs
        # Union of chairs and secretaries across segments
        seen_chair_names: set[str] = set()
        for s in segs:
            cn = s["chair"].get("name")
            if cn and cn not in seen_chair_names:
                seen_chair_names.add(cn)
                chairs.append(s["chair"])
            for sec in s["secretaries"]:
                sn = sec.get("name")
                if sn and sn not in seen_chair_names:
                    # secretaries listed separately; dedupe by name
                    if not any(x.get("name") == sn for x in secretaries):
                        secretaries.append(sec)
        claims.append(make_record_claim((cb[0], cb[1]), offsets))

    # attendance
    attendance, att_end = _detect_attendance(body)
    if att_end is not None:
        # claim the announce span (rough — from chair-block end onward to
        # the announce match end)
        att_match = _ATTENDANCE_RE.search(body)
        if att_match is not None:
            claims.append(
                make_record_claim((att_match.start(), att_match.end()), offsets)
            )

    quorum_met = _compute_quorum_met(
        attendance["registered"], attendance["total_seats"]
    )

    # format
    year = ctx.meta.published.year
    fmt = _detect_format(body, year)

    # closed_at — search body suffix for the italic close marker
    closed_at: str | None = None
    cm = _CLOSED_AT_RE.search(body)
    if cm:
        closed_at = _parse_time_match(cm)
        claims.append(make_record_claim((cm.start(), cm.end()), offsets))

    # outcome
    outcome = _detect_outcome(body, closed_at)

    # special_procedure (header pass; orchestrator may augment via agenda)
    special_procedure = detect_special_procedure_from_header(body)

    # SUMAR boilerplate claim (the table content is non-substantive
    # boilerplate — agenda items are extracted via the body proper).
    # NOTE: the agenda extractor parses the SUMAR for ordinal+title+pages;
    # we still claim the SUMAR span as boilerplate here so coverage credits
    # it. The agenda extractor's record claims override this transparently
    # via the claim-merge step.
    sumar_span = find_sumar_span(body)
    if sumar_span is not None:
        claims.append(
            make_boilerplate_claim(
                sumar_span,
                "plenary_stenogram.sumar_table",
                offsets,
            )
        )

    # STENOGRAMA marker line claim
    sm = re.search(r"^\(STENOGRAMA\)\s*$", body, re.MULTILINE)
    if sm:
        claims.append(
            make_boilerplate_claim(
                (sm.start(), sm.end()),
                "plenary_stenogram.stenograma_marker",
                offsets,
            )
        )

    # "Ședința din ziua de DD luna YYYY" header line — accept cedilla &
    # mojibake variants (`Ședinţa`, `Ședinþa`, `ªedinþa`).
    header_re = re.compile(
        rf"^##\s+\*\*{_SEDINTA_VARIANTS}\s+din\s+ziua\s+de[^\n]+\*\*\s*$",
        re.MULTILINE,
    )
    hm = header_re.search(body)
    if hm:
        claims.append(
            make_boilerplate_claim(
                (hm.start(), hm.end()),
                "plenary_stenogram.session_date_header",
                offsets,
            )
        )

    session_dict: dict[str, Any] = {
        "chair": chairs,
        "chair_segments": chair_segments,
        "secretaries": secretaries,
        "attendance": attendance,
        "quorum_met": quorum_met,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "format": fmt,
        "outcome": outcome,
        "special_procedure": special_procedure,
    }
    return session_dict, claims


# Re-export for tests
__all__ = [
    "extract_session",
    "find_sumar_span",
    "detect_special_procedure_from_header",
    "_OPENED_AT_RE",
    "_CLOSED_AT_RE",
    "_ATTENDANCE_RE",
    "_CHAIR_BLOCK_OPENING_RE",
    "PARLIAMENTARY_TITLE_RE",
]
