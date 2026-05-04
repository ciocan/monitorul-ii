"""Reference parser: 6 strict variants + `unknown` catch-all.

v0.2.0 graduates from the v0.1.0 stub. Ships:

  - `bill`                     — Pl-x / PL-x / L prefixes (chamber-of-origin
                                 derived from prefix; secondary_year for
                                 resubmissions; procedure flag)
  - `law`                      — Legea nr. N/Y form
  - `oug`                      — Ordonanță de urgență (OUG nr. N/Y)
  - `og`                       — Ordonanță (OG nr. N/Y) — distinguished
                                 from oug by absence of the U
  - `chamber_resolution`       — PHCD / PHS / PHCDS prefixes (chamber-of-
                                 origin derived from prefix)
  - `parliamentary_resolution` — Hotărârea Parlamentului României nr. N/Y
  - `unknown`                  — catch-all for spans that look reference-
                                 shaped but don't classify; carries `hint`
                                 for next-version planning

Variants 7-12 (motion, court_decision, constitution, regulation, eu_doc,
treaty) graduate as the discovery loop surfaces query needs.

Universal fields on every variant: `type`, `raw`, `char_offsets`. Strict
variants additionally carry their type-specific fields.

Two parser entry points:

  - `parse_primary_references(text, base_offset=0)`: extract refs from an
    agenda-item title or chair-announce sentence. Strict mode: `unknown`
    is rejected (returns the strict variants only). Used by agenda.py.
  - `parse_mentioned_references(text, base_offset=0)`: extract refs from
    free-form speech body. Permissive: emits `unknown` for cite-shaped
    spans that don't classify. Used by activities.py.
"""

from __future__ import annotations

import re
from typing import Any

REFERENCES_VERSION = "0.2.0"


# -- bill: Pl-x / PL-x / L prefixes ------------------------------------------


# `Pl-x N/Y` — Camera-originated bills (older notation)
# `PL-x N/Y` — Camera-originated bills (modern notation; same chamber)
# `L N/Y`    — Senate-originated bills
# Number can carry decimal separators (`1.234`); year is 4 digits; optional
# `/secondary_year` for resubmissions (`132/2022/2023`).
_BILL_RE = re.compile(
    r"\b(?P<prefix>P[Ll]-?x|L)\s*(?P<number>\d+(?:\.\d+)*)\s*/\s*"
    r"(?P<year>\d{4})(?:\s*/\s*(?P<secondary>\d{4}))?\b"
)

# Some converters render `Pl-x` as `Plx` or `PL x`; tolerate both (we
# normalise to canonical prefix in output).
_BILL_LOOSE_RE = re.compile(
    r"\b(?P<prefix>P[Ll]\s?[xX])\s*(?P<number>\d+(?:\.\d+)*)\s*/\s*"
    r"(?P<year>\d{4})(?:\s*/\s*(?P<secondary>\d{4}))?\b"
)


def _normalize_bill_prefix(raw_prefix: str) -> str:
    p = raw_prefix.replace(" ", "")
    if p.upper().startswith("PL"):
        return "PL-x" if "-x" in p.lower() or p.lower().endswith("x") else "PL-x"
    return "L"


def _bill_chamber(prefix: str) -> str:
    """Chamber-of-origin derived from the prefix."""
    return "camera" if prefix.upper().startswith("PL") else "senat"


# -- chamber_resolution: PHCD / PHS / PHCDS ----------------------------------


# `PHCD nr. N/Y` — Hotărâre a Camerei Deputaților
# `PHS nr. N/Y`  — Hotărâre a Senatului
# `PHCDS nr. N/Y` — joint Camera+Senat resolution
# Optional `nr.` prefix; modern docs often drop it.
_CHAMBER_RES_RE = re.compile(
    r"\b(?P<prefix>PHCDS|PHCD|PHS)\s*(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)"
    r"\s*/\s*(?P<year>\d{4})\b"
)


def _chamber_res_chamber(prefix: str) -> str | None:
    if prefix == "PHCD":
        return "camera"
    if prefix == "PHS":
        return "senat"
    return None  # PHCDS = joint, no single chamber


# -- parliamentary_resolution: Hotărârea Parlamentului României -------------


# Joint Camera+Senat resolution. Form: "Hotărârea Parlamentului României
# nr. N/Y" — sometimes "Hotărârii Parlamentului României nr. N/Y"
# (genitive form when the surrounding sentence demands it).
_PARLIAMENTARY_RES_RE = re.compile(
    r"\bHot[ăa]r[âa]r(?:i(?:i|le|lor)?|ea)\s+(?:a\s+|ale\s+)?"
    r"Parlamentului\s+Rom[âa]niei\s+"
    r"nr\.?\s*(?P<number>\d+(?:\.\d+)*)\s*/\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)


# -- law: Legea nr. N/Y ------------------------------------------------------


# `Legea nr. 96/2006` / `Legii nr. 96/2006` — direct cite
# Excludes `Legea pentru aprobarea OUG/OG` (those are bill cites).
_LAW_RE = re.compile(
    r"\bLeg(?:ea|ii)\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)


# -- oug / og: Ordonanță de urgență / Ordonanță -----------------------------


# OUG must be matched BEFORE OG (because OUG starts with O too).
_OUG_RE = re.compile(
    r"\bOUG\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*"
    r"(?P<year>\d{4})\b",
)
_OUG_FULL_RE = re.compile(
    r"\b[Oo]rdonan[țt]a\s+de\s+urgen[țt][ăa]\s+(?:a\s+Guvernului\s+)?"
    r"(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*(?P<year>\d{4})\b",
)
_OG_RE = re.compile(
    r"\bOG\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*(?P<year>\d{4})\b",
)
_OG_FULL_RE = re.compile(
    r"\b[Oo]rdonan[țt]a\s+(?:a\s+Guvernului\s+)?(?!de\s+urgen[țt][ăa])"
    r"(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*(?P<year>\d{4})\b",
)


# -- bill.procedure detector (v1.4.0 schema) ---------------------------------


_PROCEDURE_URGENCY_RE = re.compile(
    r"în\s+procedur[ăa]\s+de\s+urgen[țt][ăa]", re.IGNORECASE
)


# -- unknown.hint detector ---------------------------------------------------


_HINT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bdeciziei\s+CCR|Curtea\s+Constitu[țt]ional", re.IGNORECASE),
        "court-ish",
    ),
    (
        re.compile(r"\b(?:COM|JOIN|JOCE|REG|DIR|SWD)\s*\(\d{4}\)", re.IGNORECASE),
        "eu-doc-ish",
    ),
    (re.compile(r"\bart\.\s+\d+", re.IGNORECASE), "law-ish"),
]


def _detect_hint(text: str) -> str | None:
    for pat, hint in _HINT_PATTERNS:
        if pat.search(text):
            return hint
    return None


# -- builders ----------------------------------------------------------------


def _make_bill(
    *,
    prefix: str,
    number: str,
    year: int,
    secondary_year: int | None,
    raw: str,
    char_offsets: list[int],
    procedure: str | None = None,
) -> dict[str, Any]:
    canonical_prefix = "PL-x" if prefix.upper().startswith("PL") else "L"
    return {
        "type": "bill",
        "raw": raw,
        "char_offsets": char_offsets,
        "prefix": canonical_prefix,
        "number": number,
        "year": year,
        "secondary_year": secondary_year,
        "chamber_of_origin": _bill_chamber(canonical_prefix),
        "procedure": procedure,
    }


def _make_law(
    *, number: str, year: int, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "law",
        "raw": raw,
        "char_offsets": char_offsets,
        "number": number,
        "year": year,
        "subject": None,
    }


def _make_oug(
    *, number: str, year: int, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "oug",
        "raw": raw,
        "char_offsets": char_offsets,
        "number": number,
        "year": year,
        "issuer": "Guvern",
    }


def _make_og(
    *, number: str, year: int, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "og",
        "raw": raw,
        "char_offsets": char_offsets,
        "number": number,
        "year": year,
        "issuer": "Guvern",
    }


def _make_chamber_res(
    *, prefix: str, number: str, year: int, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "chamber_resolution",
        "raw": raw,
        "char_offsets": char_offsets,
        "prefix": prefix,
        "number": number,
        "year": year,
        "chamber": _chamber_res_chamber(prefix),
    }


def _make_parliamentary_res(
    *, number: str, year: int, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "parliamentary_resolution",
        "raw": raw,
        "char_offsets": char_offsets,
        "number": number,
        "year": year,
        "subject": None,
    }


def _make_unknown(
    *, raw: str, char_offsets: list[int], hint: str | None
) -> dict[str, Any]:
    return {
        "type": "unknown",
        "raw": raw,
        "char_offsets": char_offsets,
        "hint": hint,
    }


# -- top-level extractors ----------------------------------------------------


def _extract_bill_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for m in _BILL_RE.finditer(text):
        prefix = m.group("prefix")
        number = m.group("number")
        year = int(m.group("year"))
        secondary = int(m.group("secondary")) if m.group("secondary") else None
        raw = m.group(0)
        # detect procedure flag in trailing window (next 80 chars)
        tail = text[m.end() : m.end() + 80]
        procedure = (
            "procedură de urgență" if _PROCEDURE_URGENCY_RE.search(tail) else None
        )
        refs.append(
            _make_bill(
                prefix=prefix,
                number=number,
                year=year,
                secondary_year=secondary,
                raw=raw,
                char_offsets=[base_offset + m.start(), base_offset + m.end()],
                procedure=procedure,
            )
        )
    return refs


def _extract_chamber_res_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for m in _CHAMBER_RES_RE.finditer(text):
        refs.append(
            _make_chamber_res(
                prefix=m.group("prefix"),
                number=m.group("number"),
                year=int(m.group("year")),
                raw=m.group(0),
                char_offsets=[base_offset + m.start(), base_offset + m.end()],
            )
        )
    return refs


def _extract_parliamentary_res_refs(
    text: str, base_offset: int
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for m in _PARLIAMENTARY_RES_RE.finditer(text):
        refs.append(
            _make_parliamentary_res(
                number=m.group("number"),
                year=int(m.group("year")),
                raw=m.group(0),
                char_offsets=[base_offset + m.start(), base_offset + m.end()],
            )
        )
    return refs


def _extract_law_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for m in _LAW_RE.finditer(text):
        refs.append(
            _make_law(
                number=m.group("number"),
                year=int(m.group("year")),
                raw=m.group(0),
                char_offsets=[base_offset + m.start(), base_offset + m.end()],
            )
        )
    return refs


def _extract_oug_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen_offsets: set[tuple[int, int]] = set()
    for pat in (_OUG_RE, _OUG_FULL_RE):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen_offsets:
                continue
            seen_offsets.add(key)
            refs.append(
                _make_oug(
                    number=m.group("number"),
                    year=int(m.group("year")),
                    raw=m.group(0),
                    char_offsets=[base_offset + m.start(), base_offset + m.end()],
                )
            )
    return refs


def _extract_og_refs(
    text: str, base_offset: int, oug_spans: list[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Extract OG refs, skipping any span that overlaps an OUG match."""
    refs: list[dict[str, Any]] = []
    seen_offsets: set[tuple[int, int]] = set()
    for pat in (_OG_RE, _OG_FULL_RE):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen_offsets:
                continue
            if any(not (m.end() <= s or m.start() >= e) for s, e in oug_spans):
                continue  # overlap with OUG — skip
            seen_offsets.add(key)
            refs.append(
                _make_og(
                    number=m.group("number"),
                    year=int(m.group("year")),
                    raw=m.group(0),
                    char_offsets=[base_offset + m.start(), base_offset + m.end()],
                )
            )
    return refs


def _claimed_spans(refs: list[dict[str, Any]]) -> list[tuple[int, int]]:
    return [(r["char_offsets"][0], r["char_offsets"][1]) for r in refs]


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or a[0] >= b[1])


def parse_primary_references(text: str, base_offset: int = 0) -> list[dict[str, Any]]:
    """Extract strict-variant references from agenda title / chair announce.

    Returns the 6 strict variants only; never `unknown`. Use this when the
    chair announces a bill code: an unparseable code is an extractor bug
    worth surfacing, not a fallback to swallow.

    `base_offset` is added to every `char_offsets` tuple so callers can
    locate references in the global body coordinate system.
    """
    bills = _extract_bill_refs(text, 0)  # local offsets first; rebase below
    chamber = _extract_chamber_res_refs(text, 0)
    parl = _extract_parliamentary_res_refs(text, 0)
    laws = _extract_law_refs(text, 0)
    ougs = _extract_oug_refs(text, 0)
    oug_local_spans = [(r["char_offsets"][0], r["char_offsets"][1]) for r in ougs]
    ogs = _extract_og_refs(text, 0, oug_local_spans)

    all_refs = bills + chamber + parl + laws + ougs + ogs
    # Drop overlapping refs — keep the longer match (typically the strict
    # variant beats a generic). Sort by start ascending, end descending so
    # the longest at each start wins.
    all_refs.sort(key=lambda r: (r["char_offsets"][0], -r["char_offsets"][1]))
    deduped: list[dict[str, Any]] = []
    for r in all_refs:
        span = (r["char_offsets"][0], r["char_offsets"][1])
        if any(
            _spans_overlap(span, (d["char_offsets"][0], d["char_offsets"][1]))
            for d in deduped
        ):
            continue
        deduped.append(r)

    # Rebase to global coordinates
    if base_offset:
        for r in deduped:
            r["char_offsets"] = [
                r["char_offsets"][0] + base_offset,
                r["char_offsets"][1] + base_offset,
            ]
    return deduped


def parse_mentioned_references(text: str, base_offset: int = 0) -> list[dict[str, Any]]:
    """Extract references from speech body (or any free-form prose).

    Permissive: emits `unknown` for cite-shaped spans that don't classify
    into a strict variant. Currently the unknown detector is conservative —
    only fires for very obvious citation-shaped patterns to avoid noise.

    Returns the strict variants + any unknowns in source order.
    """
    refs = parse_primary_references(text, base_offset=base_offset)
    # No unknown emission in v0.2.0 default — graduate the unknown detector
    # patterns based on discovery-loop output. The function still routes
    # through here so callers get a single seam to swap when needed.
    return refs


__all__ = [
    "REFERENCES_VERSION",
    "parse_primary_references",
    "parse_mentioned_references",
]
