"""Reference parser: 12 strict variants + `unknown` catch-all.

v0.4.0 enables the `unknown` emitter in `parse_mentioned_references` so
cite-shaped spans that don't classify into a strict variant surface in
the body's `references_mentioned[]` array. v0.3.0 had the variants but
left `unknown` empty.

Strict variants:

  - `bill`                     — Pl-x / PL-x / L prefixes
  - `law`                      — Legea nr. N/Y
  - `oug`                      — Ordonanță de urgență (OUG nr. N/Y)
  - `og`                       — Ordonanță (OG nr. N/Y)
  - `chamber_resolution`       — PHCD / PHS / PHCDS prefixes
  - `parliamentary_resolution` — Hotărârea Parlamentului României nr. N/Y
  - `motion`                   — Moțiunea simplă / Moțiunea de cenzură   (v0.3.0)
  - `court_decision`           — Decizia CCR / Curții Constituționale     (v0.3.0)
  - `constitution`             — `art. N (alin. M) din Constituție`      (v0.3.0)
  - `regulation`               — Regulamentul Camerei/Senatului art. N   (v0.3.0)
  - `eu_doc`                   — COM(YYYY)NNN / JOIN / Regulamentul (UE) (v0.3.0)
  - `treaty`                   — Tratatul/Convenția de la X              (v0.3.0)
  - `unknown`                  — catch-all for spans that look reference-
                                 shaped but don't classify; carries `hint`

Universal fields on every variant: `type`, `raw`, `char_offsets`. Strict
variants additionally carry their type-specific fields.

Two parser entry points:

  - `parse_primary_references(text, base_offset=0)`: extract refs from an
    agenda-item title or chair-announce sentence. Strict mode: `unknown`
    is rejected (returns the strict variants only). Used by agenda.py.
  - `parse_mentioned_references(text, base_offset=0)`: extract refs from
    free-form speech body. Permissive: returns strict variants PLUS
    `unknown` for cite-shaped spans that don't classify. Used by
    activities.py for `references_mentioned[]`.

# Future graduation candidates (TODO — discovery-loop output)

A v0.3.0 cite-shape probe over a 1001-doc sample surfaced ~69K unclassified
cite-shaped spans (≈69 per doc). The breakdown points to three follow-on
graduations:

1. **`code` variant** (~2,100 hits — clean candidate). Romanian named
   codes: Codul muncii / penal / fiscal / civil / silvic / vamal /
   comercial / administrativ / aerian / rutier / navigației. Each has
   a stable canonical name; one regex with an enum field would graduate
   them out of `unknown`.

2. **Broaden `treaty`** (~550 hits — easy fold). The current `treaty`
   variant only catches `Tratatul / Convenția / Carta`. Same shape applies
   to `Acordul de la X`, `Protocolul de la X`, `Protocolul adițional X`,
   plus broader `Carta` forms (Carta socială europeană, Carta europeană
   a autonomiei locale). Add new alternations to the existing patterns.

3. **Cross-reference linker for `art. N`** (~62K hits — context-dependent,
   NOT a regex variant). Bare `art. N (alin. M) (lit. X)` references
   point to articles of the law/bill currently being debated. Resolving
   them requires linking to the agenda item's `primary_references[]` —
   a separate cross-reference pass that mutates `unknown` refs in the
   body into typed pointers (`{type: "law_article_ref", parent_law_id,
   article}`). This is a body-level join, not a regex graduation.
"""

from __future__ import annotations

import re
from typing import Any

REFERENCES_VERSION = "0.4.0"


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


# -- motion: Moțiunea simplă / de cenzură (v0.3.0) ---------------------------


# `Moțiunea simplă privind „Title”` / `Moțiunea de cenzură nr. N` /
# `moțiunea simplă „Title”` (lowercase, common in body prose). The motion
# kind is captured directly from the qualifier word; the title (when
# quoted with „...") is extracted into `title`.
_MOTION_SIMPLE_RE = re.compile(
    r"\bMo[țt]iun(?:e|ea|ii|i)\s+simpl[ăa]\b"
    r"(?:\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)(?:\s*/\s*(?P<year>\d{4}))?)?"
    r"(?:\s+(?:privind\s+|cu\s+titlul\s+|intitulat[ăa]\s+)?"
    r"(?:„|\")(?P<title>[^„\"\n]{3,200})(?:”|\"))?",
    re.IGNORECASE,
)
_MOTION_CENSURE_RE = re.compile(
    r"\bMo[țt]iun(?:e|ea|ii|i)\s+de\s+cenzur[ăa]\b"
    r"(?:\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)(?:\s*/\s*(?P<year>\d{4}))?)?"
    r"(?:\s+(?:privind\s+|cu\s+titlul\s+|intitulat[ăa]\s+)?"
    r"(?:„|\")(?P<title>[^„\"\n]{3,200})(?:”|\"))?",
    re.IGNORECASE,
)


# -- court_decision: Decizia CCR (v0.3.0) ------------------------------------


# `Decizia CCR nr. N/Y` / `Decizia Curții Constituționale nr. N/Y` /
# `Decizia nr. N/Y a Curții Constituționale`. The reverse form (number
# first, court suffix) is common in body prose.
_COURT_DECISION_RE = re.compile(
    r"\b(?:Decizia(?:\s+nr\.?)?\s+CCR|Decizia\s+Cur[țt]ii\s+Constitu[țt]ionale|"
    r"Decizia(?:\s+nr\.?)?(?:\s+\d+(?:\.\d+)*\s*/\s*\d{4})?\s+a\s+Cur[țt]ii\s+Constitu[țt]ionale)"
    r"\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)
# Forward form: `Decizia Curții Constituționale a României nr. N/Y` with
# optional `a României` suffix between court name and number
_COURT_DECISION_FORWARD_RE = re.compile(
    r"\bDecizi(?:a|ei)\s+Cur[țt]ii\s+Constitu[țt]ionale(?:\s+a\s+Rom[âa]niei)?"
    r"\s+(?:nr\.?\s*)?(?P<number>\d+(?:\.\d+)*)\s*/\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)


# -- constitution: art. N din Constituție (v0.3.0) ---------------------------


# `art. N (alin. (M)) (lit. (X)) din Constituție` / `Constituția României,
# art. N` / `art. N din Constituția României`. The `article` field captures
# the article number plus any alin./lit. qualifiers as cited.
_CONSTITUTION_FORWARD_RE = re.compile(
    r"\bart\.?\s+(?P<article>\d+(?:\s+alin\.?\s*\(\d+(?:\^\d+)?\))?"
    r"(?:\s+(?:lit\.|litera)\s*[a-zA-Z]\)?)?)"
    r"\s+(?:din\s+)?Constitu[țt]i(?:a|e|ei|ile|ilor)(?:\s+Rom[âa]niei)?",
    re.IGNORECASE,
)
_CONSTITUTION_REVERSE_RE = re.compile(
    r"\bConstitu[țt]ia(?:\s+Rom[âa]niei)?(?:,\s+republicat[ăa])?"
    r"(?:,\s+art\.?\s+(?P<article>\d+(?:\s+alin\.?\s*\(\d+(?:\^\d+)?\))?))",
    re.IGNORECASE,
)


# -- regulation: parliamentary internal regulation (v0.3.0) ------------------


# `art. N (alin. M) din Regulamentul Camerei Deputaților` /
# `Regulamentul Senatului art. N` / `art. N din Regulamentul activităților
# comune ale Camerei Deputaților și Senatului` (joint regulation).
_REGULATION_FORWARD_RE = re.compile(
    r"\bart\.?\s+(?P<article>\d+(?:\s+alin\.?\s*\(\d+(?:\^\d+)?\))?"
    r"(?:\s+(?:lit\.|litera)\s*[a-zA-Z]\)?)?)"
    r"\s+(?:din\s+)?Regulament(?:ul|elor)?\s+"
    r"(?P<scope>Camerei\s+Deputa[țt]ilor(?:\s+[șs]i\s+Senatului)?|"
    r"Senatului|activit[ăa][țt]ilor\s+comune[^.\n]{0,80}|"
    r"[șs]edin[țt]elor\s+comune[^.\n]{0,40})",
    re.IGNORECASE,
)
_REGULATION_REVERSE_RE = re.compile(
    r"\bRegulamentul\s+"
    r"(?P<scope>Camerei\s+Deputa[țt]ilor(?:\s+[șs]i\s+Senatului)?|"
    r"Senatului|activit[ăa][țt]ilor\s+comune[^.\n]{0,80})"
    r"(?:,\s+art\.?\s+(?P<article>\d+(?:\s+alin\.?\s*\(\d+(?:\^\d+)?\))?))?",
    re.IGNORECASE,
)


def _regulation_chamber(scope: str) -> str | None:
    s = scope.lower()
    if "camerei deputaților" in s and "și senatului" in s:
        return "joint"
    if "activităților comune" in s or "ședințelor comune" in s:
        return "joint"
    if "camerei deputaților" in s:
        return "camera"
    if "senatului" in s:
        return "senat"
    return None


# -- eu_doc: EU document codes (v0.3.0) --------------------------------------


# Standard EU code: `COM(2024)123 final` / `JOIN(2024)45` / `SWD(2024)78`
_EU_CODE_RE = re.compile(
    r"\b(?P<kind>COM|JOIN|JOCE|SWD)\s*\(\s*(?P<year>\d{4})\s*\)\s*"
    r"(?P<number>\d+(?:\.\d+)*)"
    r"(?:\s+final)?",
    re.IGNORECASE,
)
# `Regulamentul (UE) 2024/123` (with space before parens)
_EU_REGULATION_RE = re.compile(
    r"\bRegulamentul\s*\(UE\)\s*(?P<year>\d{4})\s*/\s*(?P<number>\d+)",
    re.IGNORECASE,
)
# `Directiva 2024/45/UE` (year first, then number, then UE marker)
_EU_DIRECTIVE_RE = re.compile(
    r"\bDirectiva\s+(?P<year>\d{4})\s*/\s*(?P<number>\d+)\s*/\s*(?:UE|CE)\b",
    re.IGNORECASE,
)
# `Decizia (UE) 2024/123` — EU Decision (not CCR; distinguished by "(UE)")
_EU_DECISION_RE = re.compile(
    r"\bDecizia\s*\(UE\)\s*(?P<year>\d{4})\s*/\s*(?P<number>\d+)",
    re.IGNORECASE,
)


# -- treaty: international treaties / conventions (v0.3.0) -------------------


# `Tratatul de la Lisabona` / `Tratatul de la Maastricht` / `Tratatul ...`
# `Convenția de la Geneva` / `Convenția europeană a drepturilor omului`
# `Carta Națiunilor Unite` / `Carta drepturilor fundamentale a UE`
_TREATY_TRATATUL_RE = re.compile(
    r"\bTratat(?:ul|ului)\s+(?:de\s+la\s+)?(?P<name>[A-ZȘȚĂÎÂ][^.,;:\n]{2,120}?)"
    r"(?=[.,;:\n]|\s+(?:din|privind|pentru|este|a\s+fost|adoptat))",
)
_TREATY_CONVENTIA_RE = re.compile(
    r"\bConven[țt]i(?:a|ei)\s+(?:de\s+la\s+|european[ăa]\s+|interna[țt]ional[ăa]\s+)?"
    r"(?P<name>[A-ZȘȚĂÎÂ][^.,;:\n]{2,120}?)"
    r"(?=[.,;:\n]|\s+(?:din|privind|pentru|este|a\s+fost|asupra))",
)
_TREATY_CARTA_RE = re.compile(
    r"\bCarta\s+(?P<name>(?:Națiunilor\s+Unite|drepturilor\s+fundamentale[^.,;:\n]{0,60}|"
    r"social[ăa]\s+european[ăa]|olimpic[ăa]))",
    re.IGNORECASE,
)


# -- bill.procedure detector (v1.4.0 schema) ---------------------------------


_PROCEDURE_URGENCY_RE = re.compile(
    r"în\s+procedur[ăa]\s+de\s+urgen[țt][ăa]", re.IGNORECASE
)


# -- unknown emission (v0.4.0) ----------------------------------------------
#
# Cite-shape patterns: each match becomes an `unknown` reference if its
# span doesn't overlap a strict variant. Hints classify the suspected
# domain — schema enum: law-ish | court-ish | eu-doc-ish | other.
#
# Patterns are intentionally narrow: bare `art. N` ALONE (without a
# `Constituție` / `Regulamentul` / etc. suffix), Romanian named codes
# (Codul muncii / penal / fiscal / ...), Hotărâre de Guvern (HG nr. N/Y),
# Decret-lege, court-of-cassation decisions, and treaty-shaped openers
# not covered by the strict `treaty` variant. The list is meant to
# surface the long-tail; future strict variants graduate from here.


_UNKNOWN_EMIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Romanian named codes — "law-ish" hint; clean candidate for graduation
    # to a `code` variant (see module docstring "Future graduation candidates").
    (
        re.compile(
            r"\bCodul\s+(?:muncii|fiscal|civil|penal|"
            r"de\s+procedur[ăa]\s+(?:civil[ăa]|penal[ăa])|"
            r"administrativ|silvic|aerian|rutier|vamal|comercial|"
            r"familiei|navigației|consumului|insolven[țt]ei)",
            re.IGNORECASE,
        ),
        "law-ish",
    ),
    # `HG nr. N/Y` / `Hotărârea Guvernului nr. N/Y` — Government decision
    # (separate from PHCD/PHS chamber resolutions).
    (
        re.compile(
            r"\bHG\s+(?:nr\.?\s*)?\d+(?:\.\d+)*\s*/\s*\d{4}\b",
            re.IGNORECASE,
        ),
        "law-ish",
    ),
    (
        re.compile(
            r"\bHot[ăa]r[âa]re(?:a|ii)?\s+(?:a\s+)?Guvernului(?:\s+Rom[âa]niei)?"
            r"\s+nr\.?\s*\d+(?:\.\d+)*\s*/\s*\d{4}\b",
            re.IGNORECASE,
        ),
        "law-ish",
    ),
    # `Decret(-lege) nr. N/Y` — pre-1989 / transitional legislative form.
    (
        re.compile(
            r"\bDecret(?:-?lege)?\s+(?:nr\.?\s*)?\d+(?:\.\d+)*\s*/\s*\d{4}\b",
            re.IGNORECASE,
        ),
        "law-ish",
    ),
    # `Decizia ICCJ` / `Decizia Înaltei Curți de Casație` — high-court
    # decisions other than CCR (which has its own strict variant).
    (
        re.compile(
            r"\bDecizi(?:a|ei)\s+(?:nr\.?\s*\d+(?:\.\d+)*\s*/\s*\d{4}\s+(?:a\s+)?)?"
            r"(?:Cur[țt]ii\s+Supreme|Înaltei\s+Cur[țt]i\s+de\s+Casa[țt]ie|ICCJ)",
            re.IGNORECASE,
        ),
        "court-ish",
    ),
    # `Acordul de la X` / `Acordului de la X` (genitive) / `Acordul privind X`
    # — treaty-shaped, not yet a strict `treaty` variant member. Schema hint
    # is `other` (no `treaty-ish` enum value in v1.9.0).
    (
        re.compile(
            r"\bAcord(?:ul|ului)\s+(?:de\s+la\s+|dintre\s+|privind\s+)"
            r"[A-ZȘȚĂÎÂa-z][^.,;:\n]{2,80}",
            re.IGNORECASE,
        ),
        "other",
    ),
    # `Protocolul ...` / `Protocolului ...` — treaty-shaped, same status.
    (
        re.compile(
            r"\bProtocol(?:ul|ului)\s+(?:de\s+la\s+|adi[țt]ional\s+|"
            r"op[țt]ional\s+|nr\.?\s*\d+\s+)[A-ZȘȚĂÎÂa-z][^.,;:\n]{2,80}",
            re.IGNORECASE,
        ),
        "other",
    ),
    # Bare `art. N (alin. M) (lit. X)` — the dominant "unknown" bucket
    # (~62K hits in the 1001-doc probe). Context-dependent: refers to
    # articles of the law/bill currently under debate. The cross-reference
    # linker (future work) resolves these via `references_mentioned[]` →
    # agenda's `primary_references[]`. Anchored to require `art.` followed
    # by a number, NOT followed by `din Constituție` / `din Regulamentul`
    # / `din Legea nr. ...` (which the strict variants already match).
    (
        re.compile(
            r"\bart\.?\s+(\d+(?:\s+alin\.?\s*\(\d+(?:\^\d+)?\))?"
            r"(?:\s+(?:lit\.|litera)\s*[a-zA-Z]\)?)?)"
            r"(?!\s+(?:din\s+)?(?:Constitu[țt]i|Regulament|Legea\s+nr|"
            r"OUG|OG|Codul|Hot[ăa]r[âa]rea))",
            re.IGNORECASE,
        ),
        "law-ish",
    ),
]


def _detect_hint(text: str) -> str | None:
    """Coarse hint classifier — used when a regex emits an unknown without
    a built-in hint (legacy callers).
    """
    for pat, hint in _UNKNOWN_EMIT_PATTERNS:
        if pat.search(text):
            return hint
    return None


def _extract_unknown_refs(
    text: str,
    base_offset: int,
    claimed_spans: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Emit `unknown` refs for cite-shaped spans not already claimed by
    strict variants. Each match attaches the hint that flagged it.
    """
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pat, hint in _UNKNOWN_EMIT_PATTERNS:
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if span in seen:
                continue
            seen.add(span)
            # Skip if overlaps any strict-variant span
            if any(not (span[1] <= s or span[0] >= e) for s, e in claimed_spans):
                continue
            refs.append(
                _make_unknown(
                    raw=m.group(0),
                    char_offsets=[base_offset + span[0], base_offset + span[1]],
                    hint=hint,
                )
            )
    return refs


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


# v0.3.0 builders


def _make_motion(
    *,
    motion_kind: str,
    number: str | None,
    year: int | None,
    title: str | None,
    raw: str,
    char_offsets: list[int],
) -> dict[str, Any]:
    return {
        "type": "motion",
        "raw": raw,
        "char_offsets": char_offsets,
        "motion_kind": motion_kind,
        "number": number,
        "year": year,
        "title": title,
        "chamber_of_origin": None,
    }


def _make_court_decision(
    *, number: str, year: int, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "court_decision",
        "raw": raw,
        "char_offsets": char_offsets,
        "court": "CCR",
        "number": number,
        "year": year,
    }


def _make_constitution(
    *, article: str, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "constitution",
        "raw": raw,
        "char_offsets": char_offsets,
        "article": article,
    }


def _make_regulation(
    *,
    chamber: str | None,
    article: str | None,
    raw: str,
    char_offsets: list[int],
) -> dict[str, Any]:
    return {
        "type": "regulation",
        "raw": raw,
        "char_offsets": char_offsets,
        "chamber": chamber,
        "article": article,
    }


def _make_eu_doc(
    *,
    code_kind: str,
    number: str | None,
    year: int | None,
    raw: str,
    char_offsets: list[int],
) -> dict[str, Any]:
    return {
        "type": "eu_doc",
        "raw": raw,
        "char_offsets": char_offsets,
        "code_kind": code_kind,
        "number": number,
        "year": year,
    }


def _make_treaty(
    *, name: str, year: int | None, raw: str, char_offsets: list[int]
) -> dict[str, Any]:
    return {
        "type": "treaty",
        "raw": raw,
        "char_offsets": char_offsets,
        "name": name,
        "year": year,
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


def _extract_motion_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    """Extract `motion` variant refs (simple + censure)."""
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for kind, pat in (("simple", _MOTION_SIMPLE_RE), ("censure", _MOTION_CENSURE_RE)):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            number = m.group("number") if "number" in m.groupdict() else None
            year_s = m.group("year") if "year" in m.groupdict() else None
            year = int(year_s) if year_s else None
            title = m.group("title") if "title" in m.groupdict() else None
            refs.append(
                _make_motion(
                    motion_kind=kind,
                    number=number,
                    year=year,
                    title=title.strip() if title else None,
                    raw=m.group(0),
                    char_offsets=[base_offset + m.start(), base_offset + m.end()],
                )
            )
    return refs


def _extract_court_decision_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pat in (_COURT_DECISION_RE, _COURT_DECISION_FORWARD_RE):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                _make_court_decision(
                    number=m.group("number"),
                    year=int(m.group("year")),
                    raw=m.group(0),
                    char_offsets=[base_offset + m.start(), base_offset + m.end()],
                )
            )
    return refs


def _extract_constitution_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pat in (_CONSTITUTION_FORWARD_RE, _CONSTITUTION_REVERSE_RE):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            article = m.group("article") if "article" in m.groupdict() else None
            if not article:
                continue
            refs.append(
                _make_constitution(
                    article=article.strip(),
                    raw=m.group(0),
                    char_offsets=[base_offset + m.start(), base_offset + m.end()],
                )
            )
    return refs


def _extract_regulation_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pat in (_REGULATION_FORWARD_RE, _REGULATION_REVERSE_RE):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            scope = m.group("scope")
            article = m.group("article") if "article" in m.groupdict() else None
            chamber = _regulation_chamber(scope) if scope else None
            refs.append(
                _make_regulation(
                    chamber=chamber,
                    article=article.strip() if article else None,
                    raw=m.group(0),
                    char_offsets=[base_offset + m.start(), base_offset + m.end()],
                )
            )
    return refs


def _extract_eu_doc_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()

    def _push(kind: str, m: re.Match) -> None:
        key = (m.start(), m.end())
        if key in seen:
            return
        seen.add(key)
        year_s = m.group("year") if "year" in m.groupdict() else None
        number = m.group("number") if "number" in m.groupdict() else None
        refs.append(
            _make_eu_doc(
                code_kind=kind,
                number=number,
                year=int(year_s) if year_s else None,
                raw=m.group(0),
                char_offsets=[base_offset + m.start(), base_offset + m.end()],
            )
        )

    for m in _EU_CODE_RE.finditer(text):
        _push(m.group("kind").upper(), m)
    for m in _EU_REGULATION_RE.finditer(text):
        _push("regulation", m)
    for m in _EU_DIRECTIVE_RE.finditer(text):
        _push("directive", m)
    for m in _EU_DECISION_RE.finditer(text):
        _push("decision", m)
    return refs


def _extract_treaty_refs(text: str, base_offset: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pat in (_TREATY_TRATATUL_RE, _TREATY_CONVENTIA_RE, _TREATY_CARTA_RE):
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            name_raw = (m.group("name") or "").strip()
            if not name_raw:
                continue
            refs.append(
                _make_treaty(
                    name=name_raw,
                    year=None,
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
    # v0.3.0 long-tail variants
    motions = _extract_motion_refs(text, 0)
    court_decisions = _extract_court_decision_refs(text, 0)
    constitutions = _extract_constitution_refs(text, 0)
    regulations = _extract_regulation_refs(text, 0)
    eu_docs = _extract_eu_doc_refs(text, 0)
    treaties = _extract_treaty_refs(text, 0)

    all_refs = (
        bills
        + chamber
        + parl
        + laws
        + ougs
        + ogs
        + motions
        + court_decisions
        + constitutions
        + regulations
        + eu_docs
        + treaties
    )
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
    into a strict variant (v0.4.0+). Returns the strict variants + any
    unknowns in source order.

    The dominant unknown bucket on the production corpus is bare `art. N`
    cross-references (~62K of ~69K total in a 1001-doc probe). These are
    inherently context-dependent — they refer to articles of the law/bill
    currently under debate — and resolving them requires a cross-reference
    linker pass (see module docstring "Future graduation candidates").
    Surfacing them as `unknown.hint=law-ish` is the v0.4.0 stopgap.
    """
    refs = parse_primary_references(text, base_offset=base_offset)
    # Strict-variant spans (rebased to base_offset already) — exclude these
    # from the unknown emit pass so we don't double-claim.
    claimed_global = [(r["char_offsets"][0], r["char_offsets"][1]) for r in refs]
    # Translate claimed spans back to local coords for the unknown detector
    claimed_local = [(s - base_offset, e - base_offset) for s, e in claimed_global]
    unknowns = _extract_unknown_refs(text, base_offset, claimed_local)
    # Merge + sort by start
    out = list(refs) + unknowns
    out.sort(key=lambda r: (r["char_offsets"][0], r["char_offsets"][1]))
    return out


__all__ = [
    "REFERENCES_VERSION",
    "parse_primary_references",
    "parse_mentioned_references",
]
