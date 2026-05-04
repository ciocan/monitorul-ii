"""Shared boilerplate detector — patterns that occur across every MO Partea II
document and shouldn't count as extractor "gaps."

Starts with the universal MO preamble (issue header line, weekday-date line,
`# **PA R T E A A I I-A**` chain, chamber heading, session label, legislature
parenthetical). Per-extractor boilerplate (the question_register-only
`LISTA ÎNTREBĂRILOR…` heading, the report_facsimile `(RAPOARTE DE ACTIVITATE)`
title) is emitted by the per-type extractor — keeps the discovery loop
attributable per type.

Bumping `BOILERPLATE_VERSION` invalidates all sidecars (the by-policy ledger
and coverage histogram both depend on this list). Keep adds purely
additive: new patterns earn a minor bump.
"""

from __future__ import annotations

import re

from monitorul_ii.extraction.coverage import (
    Claim,
    line_offsets,
    make_boilerplate_claim,
)

BOILERPLATE_VERSION = "0.1.0"


# Patterns are anchored line-wise (multiline) so they only match standalone
# lines of MO boilerplate — never substrings inside extractor-claimed
# content. Each entry is (compiled regex, reason).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "Anul 194 (XXXVII) — Nr. 29" — issue/year banner
    (
        re.compile(
            r"^Anul\s+\d+\s*\([IVXM]+\)\s*[—–-]\s*Nr\.\s*[0-9A-Za-z]+\s*$",
            re.MULTILINE,
        ),
        "shared_boilerplate.year_issue_banner",
    ),
    # "Joi, 11 iulie 2013" — weekday + date line
    (
        re.compile(
            r"^(?:Luni|Marți|Marţi|Marti|Miercuri|Joi|Vineri|Sâmbătă|S[âaă]mb[ăa]t[ăa]|Duminică|Duminica)"
            r"\s*,\s*\d{1,2}\s+\w+\s+\d{4}\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "shared_boilerplate.weekday_date_line",
    ),
    # "# **PA R T E A  A  I I - A DEZBATERI PARLAMENTARE**" — multi-spaced banner.
    # Older docs hand the heading as H2 instead of H1, hence `#{1,2}`.
    (
        re.compile(
            r"^#{1,2}\s+\*\*\s*PA\s*R\s*T\s*E\s*A\s+A\s+I\s*I\s*-\s*A"
            r"\s+DEZBATERI\s+PARLAMENTARE\s*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "shared_boilerplate.partea_header",
    ),
    # "# **DEZBATERI PARLAMENTARE**" — duplicate banner (occurs immediately after)
    (
        re.compile(
            r"^#{1,2}\s+\*\*\s*DEZBATERI\s+PARLAMENTARE\s*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "shared_boilerplate.dezbateri_header",
    ),
    # "# **CAMERA DEPUTAȚILOR**" or "# **SENATUL**" — chamber heading.
    # 2012-2017 era docs render this as H2; modern docs as H1. Both occur.
    (
        re.compile(
            r"^#{1,2}\s+\*\*\s*(?:CAMERA\s+DEPUTA[ȚTÞ]ILOR|SENATUL|CAMERA\s+DEPUTATILOR)\s*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "shared_boilerplate.chamber_heading",
    ),
    # "SESIUNEA I ORDINARĂ – FEBRUARIE 2026" — session label
    (
        re.compile(
            r"^SESIUNEA[^\n]{0,200}$",
            re.MULTILINE,
        ),
        "shared_boilerplate.session_label",
    ),
    # "(Legislatura a X-a)" — legislature parenthetical
    (
        re.compile(
            r"^\(\s*Legislatura[^\n)]{0,80}\)\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "shared_boilerplate.legislature_paren",
    ),
]


def claim_shared_boilerplate(body_text: str) -> list[Claim]:
    """Run the shared pattern list over `body_text` and return claims.

    Patterns are line-anchored multiline regexes; each match emits one
    `Claim(kind="boilerplate")` covering the matched span. The dispatcher
    merges these with the extractor's record claims before computing
    coverage; overlapping claims are deduped by `compute_coverage`.
    """
    offsets = line_offsets(body_text)
    out: list[Claim] = []
    for pat, reason in _PATTERNS:
        for m in pat.finditer(body_text):
            start, end = m.start(), m.end()
            if end <= start:
                continue
            out.append(make_boilerplate_claim((start, end), reason, offsets))
    return out
