"""Agenda extractor for plenary_stenogram.

28-category rule table with weighted resolution; SUMAR-driven enumeration
with body-scan fallback. Sub-field detectors run conditionally:

  - confidence_type      → when category=government_confidence
  - requested_by_group   → when category=government_hour
  - reexamination_reason → any category, when title contains 'reexaminare'

`agenda_items[].outcome` (9-value enum) is a separate body-driven detector
that scans the agenda item's body span for the vote-result line.

Per-item activities are populated by `activities.extract_activities`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    lines_for_range,
    make_record_claim,
)
from monitorul_ii.extraction.extractors.plenary import activities as activities_mod
from monitorul_ii.extraction.extractors.plenary.session import find_sumar_span
from monitorul_ii.extraction.references import parse_primary_references
from monitorul_ii.extraction.topics import make_topics

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext


# -- Category rule table ----------------------------------------------------


@dataclass(frozen=True)
class _CategoryRule:
    category: str
    pattern: re.Pattern[str]
    weight: float = 1.0  # 1.0 = specific, 0.5 = generic baseline


_CATEGORY_RULES: list[_CategoryRule] = [
    # Single-phrase specific discriminators (weight 1.0)
    _CategoryRule(
        "oath_taking", re.compile(r"Depunerea jur[ăa]m[âa]ntului", re.IGNORECASE)
    ),
    _CategoryRule(
        "government_hour", re.compile(r"\bOra\s+Guvernului\b", re.IGNORECASE)
    ),
    _CategoryRule(
        "government_confidence",
        re.compile(
            r"vot\s+de\s+încredere|mo[țt]iun(?:e|ea|ii|ile|ilor)\s+de\s+cenzur[ăa]|"
            r"angajare(?:a)?\s+r[ăa]spunderii|investitur(?:a|ii)?\s+Guvernului|"
            r"învestire\s+a\s+Guvernului",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "tacit_adoption",
        re.compile(
            r"adoptat[ăa]?\s+tacit|împlinirea\s+termenului\s+constitu[țt]ional",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "commemorative",
        re.compile(
            r"Alocu[țt]iune\s+cu\s+prilejul|Moment\s+de\s+reculegere", re.IGNORECASE
        ),
    ),
    _CategoryRule(
        "final_vote_batch",
        re.compile(r"Supunerea\s+la\s+votul\s+final", re.IGNORECASE),
    ),
    _CategoryRule(
        "political_declarations",
        re.compile(
            r"Declara[țt]ii\s+politice|interven[țt]ii\s+ale\s+deputa[țt]ilor",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "appointment",
        re.compile(
            r"Numirea\s+(?:unor\s+)?membri(?:lor)?|alegerea\s+(?:unor\s+)?membri",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "motion",
        re.compile(r"mo[țt]iun(?:e|ea)\s+(?:simpl[ăa]|de\s+cenzur[ăa])", re.IGNORECASE),
    ),
    _CategoryRule(
        "committee_report_presentation",
        re.compile(
            r"Prezentarea\s+Raportului\s+privind\s+activitatea\s+Comisiei",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "legislative_transmission",
        re.compile(
            r"Aprobarea\s+transmiterii\s+c[ăa]tre\s+Camera\s+Deputa[țt]ilor|"
            r"ca\s+prim[ăa]\s+Camer[ăa]\s+sesizat[ăa]",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "withdrawal",
        re.compile(
            r"solicitarea\s+de\s+retragere\s+din\s+procesul\s+legislativ",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "notification",
        re.compile(
            r"Not[ăa]\s+pentru\s+exercitarea|Informare\s+privind\s+depunerea|"
            r"Informare\s+(?:din\s+partea|privind)",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "regulation_amendment",
        re.compile(
            r"modificarea\s+Regulamentului\s+(?:Senatului|Camerei\s+Deputa[țt]ilor)",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "party_membership_change",
        re.compile(
            r"trecere(?:a)?\s+(?:de\s+)?la\s+Grupul|demisie\s+din\s+Grupul|"
            r"activarea\s+doamnei?\s+(?:deputat|senator)|activarea\s+domnului\s+(?:deputat|senator)",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "subsidiarity_check",
        re.compile(
            r"control\s+de\s+subsidiaritate|în\s+temeiul\s+Protocolului\s+nr\.?\s*2",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "foreign_address",
        re.compile(
            r"(?:Mesaj(?:ul)?|Discurs(?:ul)?|Alocu[țt]iune)\s+(?:al\s+)?Pre[șs]edintelui\s+(?!Rom[âa]niei)",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "seat_vacancy",
        re.compile(
            r"[Vv]acantarea\s+func[țt]iei|Validarea\s+func[țt]iei", re.IGNORECASE
        ),
    ),
    _CategoryRule(
        "parliamentary_declaration",
        re.compile(
            r"Declara[țt](?:ia|iei)?\s+Parlamentului\s+Rom[âa]niei", re.IGNORECASE
        ),
    ),
    _CategoryRule(
        "delegation_membership",
        re.compile(
            r"Componen[țt]a\s+nominal[ăa]\s+(?:[șs]i\s+a\s+)?(?:conducerii\s+)?Delega[țt]iei\s+permanente",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "mandate_validation",
        re.compile(r"Validarea\s+mandatelor", re.IGNORECASE),
    ),
    _CategoryRule(
        "chamber_officer",
        re.compile(
            r"alegerea\s+(?:vicepre[șs]edin[țt]ilor?|(?:unui|unor)\s+vicepre[șs]edinte)\s+(?:al(?:e)?\s+)?(?:Senatului|Camerei\s+Deputa[țt]ilor)",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "committee_membership",
        re.compile(
            r"constituirea\s+Comisiei\s+(?:speciale|permanente)|"
            r"componen[țt]a\s+(?:nominal[ăa]\s+)?Comisiei",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "eu_consultation",
        re.compile(
            r"opinie\s+referitoare\s+la\s+Comunicarea\s+Comisiei|"
            r"adoptarea\s+opiniei\s+referitoare\s+la",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "questions_interpellations",
        re.compile(
            r"[ÎI]ntreb[ăa]ri\s+orale\s+adresate\s+Guvernului|"
            r"R[ăa]spunsuri\s+(?:scrise\s+)?la\s+întreb[ăa]ri",
            re.IGNORECASE,
        ),
    ),
    _CategoryRule(
        "deadline_extension",
        re.compile(r"prelungirea\s+termenului", re.IGNORECASE),
    ),
    _CategoryRule(
        "procedural",
        re.compile(
            r"Aprobarea\s+ordinii\s+de\s+zi|programul(?:ui)?\s+de\s+lucru|"
            r"Discu[țt]ii\s+procedurale|modificarea\s+ordinii",
            re.IGNORECASE,
        ),
    ),
    # Generic baseline (weight 0.5) — loses ties to specifics
    _CategoryRule(
        "bill_debate",
        re.compile(
            r"(?:Dezbaterea\s+)?(?:Proiectul(?:ui)?\s+(?:de\s+lege|de\s+hot[ăa]r[âa]re)|Propunerii\s+legislative)",
            re.IGNORECASE,
        ),
        0.5,
    ),
]


def detect_category(title: str) -> tuple[str, float, list[str]]:
    """(top_category, confidence, runners_up_within_0.2_weight)."""
    matches: list[tuple[str, float]] = []
    for r in _CATEGORY_RULES:
        if r.pattern.search(title):
            matches.append((r.category, r.weight))
    if not matches:
        return ("other", 0.4, [])
    matches.sort(key=lambda x: (-x[1], x[0]))
    top_cat, top_w = matches[0]
    runners = [c for c, w in matches[1:] if top_w - w < 0.2]
    if len(matches) == 1:
        confidence = 0.95 if top_w >= 1.0 else 0.7
    else:
        confidence = 0.75
    return top_cat, confidence, runners


# -- Sub-field detectors ----------------------------------------------------


_CONFIDENCE_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("învestitură", re.compile(r"învestir|investitur", re.IGNORECASE)),
    (
        "cenzură",
        re.compile(r"mo[țt]iun(?:e|ea|ii|ile|ilor)\s+de\s+cenzur[ăa]", re.IGNORECASE),
    ),
    (
        "angajare_răspundere",
        re.compile(r"angajare(?:a)?\s+r[ăa]spunderii", re.IGNORECASE),
    ),
    ("demitere", re.compile(r"demitere", re.IGNORECASE)),
]


def detect_confidence_type(title: str) -> str | None:
    for label, pat in _CONFIDENCE_TYPE_RULES:
        if pat.search(title):
            return label
    return None


_REQUESTED_BY_GROUP_RE = re.compile(
    r"la\s+solicitarea\s+Grupului\s+parlamentar\s+al\s+"
    r"(?P<group>[A-ZȘȚÂÎĂ][\w\-+ăâîșțĂÂÎȘȚ]*"
    r"(?:\s+(?:și|şi)\s+[A-ZȘȚÂÎĂ][\w\-+ăâîșțĂÂÎȘȚ]*)?)",
    re.UNICODE,
)


def detect_requested_by_group(title: str) -> str | None:
    """Extract the parliamentary group name from a `government_hour` title.

    Group names are short acronyms (PNL, PSD, USR, AUR) or short conjunctive
    forms ("USR și Forța Dreptei"). The regex deliberately doesn't capture
    everything after the group name to avoid swallowing the trailing
    "cu privire la X" clause.
    """
    m = _REQUESTED_BY_GROUP_RE.search(title)
    if m:
        return m.group("group").strip()
    return None


_REEXAMINATION_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "presidential_request",
        re.compile(r"Pre[șs]edintelui\s+Rom[âa]niei", re.IGNORECASE),
    ),
    (
        "constitutional_court",
        re.compile(
            r"Cur[țt]ii\s+Constitu[țt]ionale|Deciziei?\s+Cur[țt]ii", re.IGNORECASE
        ),
    ),
]


def detect_reexamination_reason(title: str) -> str | None:
    if "reexaminare" not in title.lower():
        return None
    for label, pat in _REEXAMINATION_RULES:
        if pat.search(title):
            return label
    return "parliamentary_majority"


# -- Outcome detector (body-driven) -----------------------------------------


_OUTCOME_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("adoptat_tacit", re.compile(r"adoptat[ăa]?\s+tacit", re.IGNORECASE)),
    (
        "votul_final_deferred",
        re.compile(
            r"r[ăa]m[âa]ne\s+pentru\s+votul\s+final|r[ăa]mas\s+pentru\s+votul\s+final",
            re.IGNORECASE,
        ),
    ),
    (
        "vot_amânat",
        re.compile(
            r"votul\s+final\s+(?:se\s+va\s+da|asupra[^.]*?va\s+fi\s+dat).*?(?:într-o\s+[șs]edin[țt][ăa]\s+viitoare)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    ("retras", re.compile(r"a\s+fost\s+retras", re.IGNORECASE)),
    ("retrimis", re.compile(r"retrimitere\s+la\s+(?:Comisia|Comisii)", re.IGNORECASE)),
    ("respins", re.compile(r"a\s+fost\s+respin[șs]", re.IGNORECASE)),
    ("informare", re.compile(r"^Informare\s+", re.IGNORECASE)),
    ("adoptat", re.compile(r"a\s+fost\s+(?:adoptat|aprobat)", re.IGNORECASE)),
]


def detect_outcome_from_body(item_body: str, title: str) -> str | None:
    """Detect agenda_items[].outcome from the item's body span + title.

    Order matters: more specific phrases first.
    """
    for label, pat in _OUTCOME_RULES:
        if pat.search(item_body) or pat.search(title):
            return label
    return None


# -- SUMAR table parsing ----------------------------------------------------


# SUMAR table contains entries of the form (after PyMuPDF rendering):
#   |Nr.<br>1.<br>Title text...<br>2.<br>Other title...|Pagina|
# OR free-floating lines like "10. Dezbaterea..." after the table tabs out.
# We extract (ordinal, title, page_range) tuples by heuristic.

_SUMAR_ITEM_RE = re.compile(
    r"(?:<br>|^|\|)\s*(?P<ord>\d{1,3})\.\s*(?P<rest>(?:(?!<br>\d{1,3}\.).)+?)"
    r"(?=<br>\d{1,3}\.|<br>\||\|Pagina\||$)",
    re.DOTALL,
)


# Multi-table SUMAR variant — second-format rows render as
# `|N.|Title spans multiple<br>lines|page|`. Each table is a markdown
# table block; the data row starts with `|N.|` and content continues
# across cell boundaries until the next `|N.|` row or end of span.
_SUMAR_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<ord>\d{1,3})\.\s*\|(?P<body>(?:(?!^\|\s*\d{1,3}\.\s*\|).)+?)"
    r"(?=^\|\s*\d{1,3}\.\s*\||\Z)",
    re.MULTILINE | re.DOTALL,
)


# Plain-text body agenda marker — older docs sometimes have agenda items
# rendered as `N. Title` lines without table wrapping. Used as a fallback
# when neither SUMAR variant catches an entry.
_SUMAR_PLAIN_LINE_RE = re.compile(
    r"^(?P<ord>\d{1,3})\.\s+(?P<rest>[^\n]{20,400})\.{2,}\s*(?P<page>\d+(?:[–\-]\d+)?)?",
    re.MULTILINE,
)


@dataclass
class _SumarEntry:
    ordinal: int
    title: str
    pages: list[int] = field(default_factory=list)


_PAGE_RANGE_RE = re.compile(r"(?P<a>\d{1,4})(?:[–\-](?P<b>\d{1,4}))?")


def _parse_pages_chunk(s: str) -> list[int]:
    """Parse a "3-5; 12; 15-17" style chunk into a sorted list of unique
    page numbers."""
    out: set[int] = set()
    for m in _PAGE_RANGE_RE.finditer(s):
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else a
        if 0 < a <= 9999 and 0 < b <= 9999 and b - a <= 200:
            out.update(range(a, b + 1))
    return sorted(out)


def _clean_sumar_title(rest: str) -> tuple[str, list[int]]:
    """Strip dot-leaders, `<br>`, table separators; pull pages out of tail."""
    cleaned = re.sub(r"\.{3,}", "", rest)
    cleaned = cleaned.replace("<br>", " ")
    cleaned = cleaned.replace("|", " ")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    pages: list[int] = []
    # Trailing page-range chunk inside `|...|`
    page_match = re.search(r"\|\s*(?P<pages>[\d–\-;,\s]+)\s*\|?\s*$", rest)
    if page_match:
        pages = _parse_pages_chunk(page_match.group("pages"))
    if not pages:
        # Trailing "...100" or "...18-19; 23" pattern
        trail_m = re.search(
            r"(?P<pages>\b\d{1,4}(?:[–\-]\d{1,4})?(?:\s*;\s*\d{1,4})*\b)\s*$",
            cleaned,
        )
        if trail_m:
            pages = _parse_pages_chunk(trail_m.group("pages"))
            cleaned = cleaned[: trail_m.start()].strip()
    return cleaned, pages


def parse_sumar(body: str, sumar_span: tuple[int, int]) -> list[_SumarEntry]:
    """Best-effort SUMAR parser → list of (ordinal, title, pages).

    Three layouts coexist across the corpus:
      1. `<br>`-separated items inside one big table cell (modern docs)
      2. Per-item table rows: `|N.|title spans cells|page|` (after first
         table tabs out — common in long SUMARs)
      3. Plain-text `N. Title ........ page` lines (older docs, no tables)

    Run all three patterns in order, dedup by ordinal (first wins).
    """
    span_text = body[sumar_span[0] : sumar_span[1]]
    entries: list[_SumarEntry] = []
    seen_ordinals: set[int] = set()

    # Pass 1: `<br>`-separated items (covers the first SUMAR table)
    for m in _SUMAR_ITEM_RE.finditer(span_text):
        ord_n = int(m.group("ord"))
        if ord_n in seen_ordinals or ord_n < 1 or ord_n > 200:
            continue
        cleaned, pages = _clean_sumar_title(m.group("rest"))
        if cleaned:
            entries.append(_SumarEntry(ordinal=ord_n, title=cleaned, pages=pages))
            seen_ordinals.add(ord_n)

    # Pass 2: per-item table rows (covers second-table-onwards layout)
    for m in _SUMAR_TABLE_ROW_RE.finditer(span_text):
        ord_n = int(m.group("ord"))
        if ord_n in seen_ordinals or ord_n < 1 or ord_n > 200:
            continue
        cleaned, pages = _clean_sumar_title(m.group("body"))
        if cleaned:
            entries.append(_SumarEntry(ordinal=ord_n, title=cleaned, pages=pages))
            seen_ordinals.add(ord_n)

    # Pass 3: plain-text fallback (older docs without tables)
    for m in _SUMAR_PLAIN_LINE_RE.finditer(span_text):
        ord_n = int(m.group("ord"))
        if ord_n in seen_ordinals or ord_n < 1 or ord_n > 200:
            continue
        cleaned, pages = _clean_sumar_title(m.group("rest"))
        if cleaned:
            entries.append(_SumarEntry(ordinal=ord_n, title=cleaned, pages=pages))
            seen_ordinals.add(ord_n)

    # Sort by ordinal (passes may interleave finds)
    entries.sort(key=lambda e: e.ordinal)
    return entries


# -- Body-scan agenda fallback ----------------------------------------------


# Body-side ordinal headers (when SUMAR is missing or incomplete):
#   "## **1. Title text**"  /  "## 1. Title"  /  "1. Title text"
_BODY_AGENDA_ITEM_RE = re.compile(
    r"^##\s+\*\*\s*(?P<ord>\d{1,3})\.\s+(?P<title>[^*\n]+)\*\*\s*$",
    re.MULTILINE,
)


def _find_agenda_item_body_span(
    body: str,
    item_ordinal: int,
    item_title: str,
    agenda_end: int,
) -> tuple[int, int] | None:
    """Locate an agenda item's body span by searching for its ordinal +
    title approximate match in the body proper.

    Returns (start, end) or None if not located.
    """
    # Look for "ordinal." marker in the body proper after SUMAR
    # Strategy: find first occurrence of the title's first 30 chars after
    # the SUMAR end (rough), then walk to next ordinal marker.
    title_snippet = item_title[:40].lower()
    if not title_snippet:
        return None
    # Look for the item-N marker in the body proper
    marker_re = re.compile(
        r"^[^a-zA-Z\n]*?" + str(item_ordinal) + r"\.\s+",
        re.MULTILINE,
    )
    candidates: list[int] = []
    for mm in marker_re.finditer(body):
        # Must be in the body proper (not in SUMAR span — usually after
        # SUMAR end). The agenda walker bounds search by agenda_end.
        if mm.start() >= agenda_end:
            break
        candidates.append(mm.start())
    if not candidates:
        return None
    # Best heuristic: pick the candidate whose 200-char window after it
    # contains the most of title_snippet
    best = candidates[0]
    return (best, agenda_end)


# -- Public API -------------------------------------------------------------


def extract_agenda(
    body: str,
    agenda_end: int,
    ctx: "ExtractContext",
) -> tuple[list[dict[str, Any]], list[Claim]]:
    """Build agenda_items[] + emit record claims.

    `agenda_end` is the offset where the agenda block ends (typically
    where the interpellation block begins, or len(body)).
    """
    claims: list[Claim] = []
    items_out: list[dict[str, Any]] = []

    # Try SUMAR-driven enumeration first
    sumar_span = find_sumar_span(body)
    sumar_entries: list[_SumarEntry] = []
    if sumar_span is not None:
        sumar_entries = parse_sumar(body, sumar_span)

    if sumar_entries:
        # SUMAR-driven path
        items_out, item_claims = _build_items_from_sumar(
            body, sumar_entries, sumar_span, agenda_end, ctx
        )
        claims.extend(item_claims)
    else:
        # Body-scan fallback
        items_out, item_claims = _build_items_from_body_scan(body, agenda_end, ctx)
        claims.extend(item_claims)

    return items_out, claims


def _build_items_from_sumar(
    body: str,
    entries: list[_SumarEntry],
    sumar_span: tuple[int, int],
    agenda_end: int,
    ctx: "ExtractContext",
) -> tuple[list[dict[str, Any]], list[Claim]]:
    offsets = ctx.line_offsets
    content_sha = ctx.content_sha
    claims: list[Claim] = []
    out: list[dict[str, Any]] = []

    sumar_end = sumar_span[1]
    body_walk_start = sumar_end

    # Build per-item body spans by scanning forward for ordinal markers
    # and partitioning the body proper into per-item spans
    spans = _partition_body_into_item_spans(body, body_walk_start, agenda_end, entries)

    for entry, (item_start, item_end) in zip(entries, spans, strict=False):
        title = entry.title
        category, confidence, _runners = detect_category(title)
        primary_refs = parse_primary_references(title)
        item_topics = make_topics(title)
        item_body = body[item_start:item_end] if item_start is not None else ""

        # Activities
        if item_start is not None:
            item_activities = activities_mod.extract_activities(
                body, item_start, item_end, ctx
            )
        else:
            item_activities = []

        # Outcome from body span + title
        outcome = detect_outcome_from_body(item_body, title)

        # Sub-fields
        confidence_type = (
            detect_confidence_type(title)
            if category == "government_confidence"
            else None
        )
        requested_by_group = (
            detect_requested_by_group(title) if category == "government_hour" else None
        )
        reexamination_reason = detect_reexamination_reason(title)

        span_chars = (
            item_start if item_start is not None else sumar_end,
            item_end if item_end is not None else sumar_end + 1,
        )
        record = {
            "ordinal": entry.ordinal,
            "title": title,
            "primary_references": primary_refs,
            "category": category,
            "confidence_type": confidence_type,
            "requested_by_group": requested_by_group,
            "outcome": outcome,
            "reexamination_reason": reexamination_reason,
            "pages_in_pdf": entry.pages,
            "topics": item_topics,
            "activities": item_activities,
            "source_span": _make_source_span(span_chars, offsets, content_sha),
            "extraction": {
                "extractor": "regex@plenary@0.1.0",
                "confidence": round(confidence, 4),
                "source_span": _make_source_span(span_chars, offsets, content_sha),
            },
        }
        out.append(record)
        if item_start is not None:
            claims.append(make_record_claim(span_chars, offsets))

    return out, claims


def _partition_body_into_item_spans(
    body: str,
    walk_start: int,
    agenda_end: int,
    entries: list[_SumarEntry],
) -> list[tuple[int | None, int | None]]:
    """Partition the body proper into per-entry spans.

    Returns a list of (start, end) tuples in entry order; (None, None)
    when an entry's body span couldn't be located.

    Coverage policy: the FIRST entry always extends backward to walk_start
    (so chair narrative + intro chair-narration before the first body
    marker is covered by item 1's activities). Items without body markers
    get (None, None) — they still appear in agenda_items[] with their
    titles, just no per-item activities.
    """
    # Find ordinal markers in the body proper
    ord_re = re.compile(r"^\s*(?P<ord>\d{1,3})\.\s+", re.MULTILINE)
    body_marks: dict[int, int] = {}
    for m in ord_re.finditer(body, walk_start, agenda_end):
        try:
            ord_n = int(m.group("ord"))
        except ValueError:
            continue
        if 0 < ord_n <= 200 and ord_n not in body_marks:
            body_marks[ord_n] = m.start()

    spans: list[tuple[int | None, int | None]] = []
    if not body_marks:
        # No body markers — give first entry the entire post-SUMAR span.
        # All chair narration + speeches go into item 1's activities.
        for entry in entries:
            spans.append((None, None))
        if entries:
            spans[0] = (walk_start, agenda_end)
        return spans

    sorted_marks = sorted(body_marks.items(), key=lambda kv: kv[1])
    first_marked_pos = sorted_marks[0][1]
    first_marked_ord = sorted_marks[0][0]

    # Identify which SUMAR-entry-ordinal got the LAST positioned body mark.
    # Any body marks for ordinals NOT in entries (e.g., SUMAR parser missed
    # later items but body has their markers) should extend the previous
    # entry's coverage forward.
    sumar_ords = {e.ordinal for e in entries}
    last_sumar_marked_entry: int | None = None
    for ord_n, _pos in sorted_marks:
        if ord_n in sumar_ords:
            last_sumar_marked_entry = ord_n

    for entry in entries:
        start = body_marks.get(entry.ordinal)
        if start is None:
            spans.append((None, None))
            continue
        # First entry always extends backward to walk_start so the
        # pre-marker chair narration is covered.
        if start == first_marked_pos and entry.ordinal == first_marked_ord:
            start = walk_start
        # End: next ordinal-marked position (if it belongs to a *later*
        # SUMAR entry) OR agenda_end. Body marks for ordinals NOT in
        # SUMAR entries don't terminate this span — let it absorb their
        # content too.
        end = agenda_end
        for ord_n, pos in sorted_marks:
            if pos > start and ord_n in sumar_ords:
                end = pos
                break
        # Last SUMAR entry that got a body mark extends FORWARD to
        # agenda_end (covers tail content for SUMAR entries whose body
        # markers were missed).
        if entry.ordinal == last_sumar_marked_entry:
            end = agenda_end
        spans.append((start, end))
    return spans


def _build_items_from_body_scan(
    body: str,
    agenda_end: int,
    ctx: "ExtractContext",
) -> tuple[list[dict[str, Any]], list[Claim]]:
    """Fallback when SUMAR is missing — scan body for `## **N. Title**` headers."""
    offsets = ctx.line_offsets
    content_sha = ctx.content_sha
    claims: list[Claim] = []
    out: list[dict[str, Any]] = []

    matches = list(_BODY_AGENDA_ITEM_RE.finditer(body, 0, agenda_end))
    for i, m in enumerate(matches):
        ord_n = int(m.group("ord"))
        title = m.group("title").strip()
        item_start = m.start()
        item_end = matches[i + 1].start() if i + 1 < len(matches) else agenda_end

        category, confidence, _runners = detect_category(title)
        primary_refs = parse_primary_references(title)
        item_topics = make_topics(title)
        item_body = body[item_start:item_end]
        item_activities = activities_mod.extract_activities(
            body, item_start, item_end, ctx
        )
        outcome = detect_outcome_from_body(item_body, title)

        confidence_type = (
            detect_confidence_type(title)
            if category == "government_confidence"
            else None
        )
        requested_by_group = (
            detect_requested_by_group(title) if category == "government_hour" else None
        )
        reexamination_reason = detect_reexamination_reason(title)

        span_chars = (item_start, item_end)
        record = {
            "ordinal": ord_n,
            "title": title,
            "primary_references": primary_refs,
            "category": category,
            "confidence_type": confidence_type,
            "requested_by_group": requested_by_group,
            "outcome": outcome,
            "reexamination_reason": reexamination_reason,
            "pages_in_pdf": [],
            "topics": item_topics,
            "activities": item_activities,
            "source_span": _make_source_span(span_chars, offsets, content_sha),
            "extraction": {
                "extractor": "regex@plenary@0.1.0",
                "confidence": round(confidence, 4),
                "source_span": _make_source_span(span_chars, offsets, content_sha),
            },
        }
        out.append(record)
        claims.append(make_record_claim(span_chars, offsets))

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
    "extract_agenda",
    "detect_category",
    "detect_confidence_type",
    "detect_requested_by_group",
    "detect_reexamination_reason",
    "detect_outcome_from_body",
    "parse_sumar",
]
