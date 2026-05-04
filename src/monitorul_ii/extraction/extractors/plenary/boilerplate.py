"""Plenary-specific boilerplate patterns.

Per Q1 module-org decision: plenary-specific boilerplate stays in
`extractors/plenary/` (not hoisted to the shared `extraction/boilerplate.py`)
so its version bumps only invalidate plenary sidecars.

Patterns claimed here (reasons prefixed `plenary_stenogram.*`):
  - bold-only PARTEA banner (when MD lacks the `#` prefix)
  - joint-session "ȘEDINȚE COMUNE..." header line
  - "Ședința din ziua de DD luna YYYY" subheader (no `**` wrapper variant)
"""

from __future__ import annotations

import re

from monitorul_ii.extraction.coverage import (
    Claim,
    line_offsets,
    make_boilerplate_claim,
)


_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Bold-only PARTEA banner ("**PA R T E A  A  I I - A DEZBATERI PARLAMENTARE**")
    # — appears when PyMuPDF renders the heading without the `#` prefix.
    (
        re.compile(
            r"^\*\*\s*PA\s*R\s*T\s*E\s*A\s+A\s+I\s*I\s*-\s*A"
            r"\s+DEZBATERI\s+PARLAMENTARE\s*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "plenary_stenogram.partea_header_bold_only",
    ),
    # Joint-session header line — appears just below DEZBATERI banner in
    # joint sessions; carries chamber + session label inline.
    (
        re.compile(
            r"^\*\*[ȘS]EDIN[ȚT]E\s+COMUNE\s+ALE\s+CAMEREI\s+DEPUTA[ȚT]ILOR\s+"
            r"[ȘS]I\s+SENATULUI\*\*[^\n]*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "plenary_stenogram.joint_session_header",
    ),
    # "Ședința din ziua de DD luna YYYY" — bare text variant (without
    # `## **...**` wrapper); the wrapped variant is claimed in session.py.
    (
        re.compile(
            r"^Ședin[țt]a\s+din\s+ziua\s+de[^\n]+$",
            re.MULTILINE,
        ),
        "plenary_stenogram.session_date_header_bare",
    ),
    # Standalone "(STENOGRAMA)" marker (shared boilerplate doesn't cover
    # this; session.py also claims it but only the first occurrence)
    (
        re.compile(r"^\(\s*STENOGRAMA\s*\)\s*$", re.MULTILINE),
        "plenary_stenogram.stenograma_marker_inline",
    ),
    # SUMAR keyword line
    (
        re.compile(r"^SUMAR\s*$", re.MULTILINE),
        "plenary_stenogram.sumar_keyword",
    ),
    # "Doamnelor și domnilor [deputați și senatori|deputați|senatori]" —
    # repeated chair address; treated as conventional opener boilerplate
    (
        re.compile(
            r"^##?\s*Doamnelor\s+și\s+domnilor(?:\s+(?:deputați|senatori|deputați\s+și\s+senatori))?\s*,?\s*$",
            re.MULTILINE,
        ),
        "plenary_stenogram.chair_address_opener",
    ),
]


def claim_plenary_boilerplate(body: str) -> list[Claim]:
    """Run plenary-specific patterns over `body` and return claims."""
    offsets = line_offsets(body)
    out: list[Claim] = []
    for pat, reason in _PATTERNS:
        for m in pat.finditer(body):
            start, end = m.start(), m.end()
            if end <= start:
                continue
            out.append(make_boilerplate_claim((start, end), reason, offsets))
    return out


__all__ = ["claim_plenary_boilerplate"]
