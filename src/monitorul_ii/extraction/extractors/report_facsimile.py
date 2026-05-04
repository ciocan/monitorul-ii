"""Extractor for `report_facsimile` documents.

External annual / activity reports submitted to Parliament and reproduced
verbatim ("reprodus în facsimil") under MO Partea II R-suffix issues.
Cohort: ~50 docs across 2014-2024 from constitutional bodies — CSAT, SRI,
SIE, BNR, ANCOM, ANRE, Avocatul Poporului, Consiliul Legislativ, SRTv/SRR,
Curtea de Conturi, etc. Single-file extractor under 300 LOC; the body
shape is intentionally minimal (report metadata + heading outline +
excerpt) since the full text remains in the sidecar markdown.

Recurring layout::

    [shared boilerplate banner]                         (handled by shared helper)

    **PA R T E A  A  I I - A** Anul N (XX) — Nr. N/R    (year banner — shared)

    Weekday, DD month YYYY                              (weekday — shared)

    ## **DEZBATERI PARLAMENTARE**                       (banner — shared)

    **ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI         (joint header — handled by
       SENATULUI** SESIUNEA …                            shared once it lands)

    (Legislatura a N-a)                                 (legislature — shared)

    ## **Ședința din ziua de DD month YYYY**            (reception session)

    (RAPOARTE DE ACTIVITATE)                            (rf-specific marker)

    SUMAR                                               (TOC; rf-specific block)

    Pagina <Title> N–M                                  (TOC entry — also Title source)

    N O T Ă:                                            (rf-specific facsimil note)
    Raportul X este reprodus în facsimil.

    [report body — H2/H1 sections, sometimes empty when
     the source PDF is image-only]

    **EDITOR: GUVERNUL ROMÂNIEI** ...                   (trailing footer; rf variant
                                                         used GUVERNUL, not PARLAMENTUL)

Title harvesting tries the SUMAR row first (the cleanest single-line
form), falls back to the body's first `## **RAPORT ...**` heading. Issuing
body is parsed from the title via four canonical patterns (`Raportul <X>
privind`, `Raportul privind activitatea desfășurată de <X>`, `Raport
asupra activității desfășurate de <X>`, `Raport de activitate al <X>`).
Reporting period is `în anul YYYY` / `pe anul YYYY` → annual range.
Reception session date comes from the `Ședința din ziua de DD month YYYY`
line; session_kind defaults to `joint` when the joint header is present
(every R-suffix doc observed in the corpus is received in joint session).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    make_boilerplate_claim,
    make_record_claim,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext

EXTRACTOR_VERSION = "0.1.0"
EXTRACTOR_LABEL = f"regex@report_facsimile@{EXTRACTOR_VERSION}"


# -- Constants --------------------------------------------------------------


_ROMANIAN_MONTHS: dict[str, int] = {
    "ianuarie": 1,
    "februarie": 2,
    "martie": 3,
    "aprilie": 4,
    "mai": 5,
    "iunie": 6,
    "iulie": 7,
    "august": 8,
    "septembrie": 9,
    "octombrie": 10,
    "noiembrie": 11,
    "decembrie": 12,
}

EXCERPT_CHARS = 500


# -- Title detection -------------------------------------------------------


# SUMAR row form 1 (modern, observed across all eras): the report title sits
# in a single line that starts with `Raport`/`Raportul` and ends in either a
# page-range (`N–M`), a series of dots, or a `|Pagina|` table cell. Three
# observed surface forms:
#
#   `Raportul Consiliului Suprem de Apărare a Țării privind activitatea
#    desfășurată în anul 2010` (CSAT — preposition `Raportul X privind`)
#   `Raport privind activitatea desfășurată de Serviciul Român de
#    Informații în anul 2007` (SRI/SIE — preposition `Raport privind ... de X`)
#   `Raport de activitate al Societății Române de Televiziune pe anul 2013`
#    (SRTv/SRR — preposition `Raport de activitate al X`)
#   `Raport asupra activității desfășurate de Consiliul Legislativ în anul
#    2010` (Consiliul Legislativ, ANCOM — preposition `Raport asupra ... de X`)
#
# Single regex captures all four; `_extract_issuing_body` discriminates
# below.
_SUMAR_TITLE_LINE_RE = re.compile(
    r"^[^\n]*?(?P<title>Raport(?:ul)?\s+[^\n.|]+?(?:[îiî]n\s+anul|pe\s+anul)\s+\d{4})",
    re.MULTILINE | re.IGNORECASE,
)
# Fallback: a `## **RAPORT ...**` heading inside the report body (modern docs
# repeat the title at the top of the body).
_BODY_TITLE_HEADING_RE = re.compile(
    r"^##\s+\*\*\s*(?P<title>RAPORT[^\n*]+?)\s*\*\*\s*$",
    re.MULTILINE,
)


def _clean_title(t: str) -> str:
    """Trim trailing dots / page-range hints / leading-trailing whitespace."""
    t = re.sub(r"\.{3,}.*$", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" .\t\n")


def _extract_title(body: str) -> str | None:
    m = _SUMAR_TITLE_LINE_RE.search(body)
    if m:
        cleaned = _clean_title(m.group("title"))
        if cleaned:
            return cleaned
    m = _BODY_TITLE_HEADING_RE.search(body)
    if m:
        cleaned = _clean_title(m.group("title"))
        if cleaned:
            return cleaned
    return None


# -- Issuing body --------------------------------------------------------


# Discriminate by preposition pattern. Each captures the institutional name
# noun phrase. Stop at `în anul`/`pe anul` / `privind` to keep the body
# label tight (no trailing period/year).
_ISSUER_PATTERNS: list[re.Pattern[str]] = [
    # `Raport privind activitatea desfășurată de <X> în anul YYYY`
    re.compile(
        r"\bRaport(?:ul)?\s+privind\s+activitatea\s+desf[ăa][șş]urat[ăa]\s+de\s+"
        r"(?P<body>[^\n]+?)(?=\s+(?:[îiî]n\s+anul|pe\s+anul)\b)",
        re.IGNORECASE,
    ),
    # `Raport asupra activității desfășurate de <X> în anul YYYY`
    re.compile(
        r"\bRaport(?:ul)?\s+asupra\s+activit[ăa][țţt]ii\s+desf[ăa][șş]urat[eaă]\s+de\s+"
        r"(?P<body>[^\n]+?)(?=\s+(?:[îiî]n\s+anul|pe\s+anul)\b)",
        re.IGNORECASE,
    ),
    # `Raport de activitate al <X> pe anul YYYY` / `Raportul de activitate al <X>`
    re.compile(
        r"\bRaport(?:ul)?\s+de\s+activitate\s+al\s+"
        r"(?P<body>[^\n]+?)(?=\s+(?:[îiî]n\s+anul|pe\s+anul)\b)",
        re.IGNORECASE,
    ),
    # `Raportul <X> privind activitatea ... în anul YYYY`
    # (CSAT form — the body sits between `Raportul` and `privind`)
    re.compile(
        r"\bRaportul\s+(?P<body>[^\n]+?)\s+privind\s+activitatea\s+desf[ăa][șş]urat[ăa]",
        re.IGNORECASE,
    ),
]


def _extract_issuing_body(title: str | None) -> str | None:
    if not title:
        return None
    for pat in _ISSUER_PATTERNS:
        m = pat.search(title)
        if m:
            return re.sub(r"\s{2,}", " ", m.group("body")).strip(" .\t,;:")
    return None


# -- Reporting period ------------------------------------------------------


# `în anul YYYY` / `pe anul YYYY`. Multi-year (`în perioada YYYY-YYYY`)
# observed only on a handful of CSAT bi-annual reports — same regex picks
# up the start year and we widen the end via the dual-year pattern.
_REPORTING_YEAR_RE = re.compile(
    r"\b(?:[îiî]n\s+anul|pe\s+anul)\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_REPORTING_PERIOD_RE = re.compile(
    r"\b[îiî]n\s+perioada\s+(?P<y1>\d{4})\s*[-–—]\s*(?P<y2>\d{4})\b",
    re.IGNORECASE,
)


def _extract_reporting_period(title: str | None, body: str) -> dict[str, str | None]:
    """Annual reports → Jan 1 – Dec 31 of the reported year.

    Searches the title first (anchored), falls back to the body's first
    occurrence. Returns `{start: null, end: null}` when no year is found.
    """
    candidates = [title or "", body[:2000]]
    for text in candidates:
        m = _REPORTING_PERIOD_RE.search(text)
        if m:
            y1, y2 = int(m.group("y1")), int(m.group("y2"))
            return {
                "start": f"{y1:04d}-01-01",
                "end": f"{y2:04d}-12-31",
            }
        m = _REPORTING_YEAR_RE.search(text)
        if m:
            y = int(m.group("year"))
            return {
                "start": f"{y:04d}-01-01",
                "end": f"{y:04d}-12-31",
            }
    return {"start": None, "end": None}


# -- Reception session ---------------------------------------------------


# `## **Ședința din ziua de DD month YYYY**` — the reception session header.
# Bold-only variant `**Ședința din ziua de ...**` and naked `Ședința din ziua
# de ...` both appear; we accept all three. Diacritic-tolerant (cedilla).
_RECEPTION_SESSION_RE = re.compile(
    r"(?:##\s+)?\*?\*?\s*[ŞȘŞS]edin[țţt]a\s+din\s+ziua\s+de\s+"
    r"(?P<day>\d{1,2})\s+(?P<month>"
    + "|".join(_ROMANIAN_MONTHS.keys())
    + r")\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
_JOINT_HEADER_RE = re.compile(
    r"\b[ȘŞS]EDIN[ȚŢT]E\s+COMUNE\s+ALE\s+CAMEREI\s+DEPUTA[ȚŢT]ILOR"
    r"\s+[ȘŞS]I\s+SENATULUI\b",
    re.IGNORECASE,
)


def _extract_received_at(body: str, frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Build the `received_at` block from body + frontmatter.

    `session_date` prefers the body's `Ședința din ziua de ...` line over
    the frontmatter (the frontmatter is a converter-side projection that
    can lag the body); falls back to frontmatter when the body line is
    absent. `session_kind` is `joint` when the joint header is present
    (true for every R-suffix doc observed); `received_in_document` stays
    null until the v0.2 cross-document linker runs.
    """
    session_date: str | None = None
    m = _RECEPTION_SESSION_RE.search(body)
    if m:
        try:
            day = int(m.group("day"))
            month = _ROMANIAN_MONTHS[m.group("month").lower()]
            year = int(m.group("year"))
            if 1 <= day <= 31 and 1990 <= year <= 2100:
                session_date = f"{year:04d}-{month:02d}-{day:02d}"
        except (KeyError, ValueError):
            session_date = None
    if session_date is None:
        fm_date = frontmatter.get("session_date")
        if isinstance(fm_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fm_date):
            session_date = fm_date
    session_kind: str | None = None
    if _JOINT_HEADER_RE.search(body):
        session_kind = "joint"
    elif frontmatter.get("chamber") == "Camera Deputaților":
        session_kind = "camera"
    elif frontmatter.get("chamber") == "Senatul":
        session_kind = "senat"
    return {
        "session_kind": session_kind,
        "session_date": session_date,
        "received_in_document": None,
    }


# -- Headings outline ----------------------------------------------------


# Standard markdown H1/H2 (with optional `**...**` bold wrapper). Skip
# headings that match shared boilerplate / rf boilerplate to keep the
# outline focused on report content.
_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,2})\s+(?P<text>[^\n]+?)\s*$",
    re.MULTILINE,
)
_HEADING_SKIP_RE = re.compile(
    r"^(?:\*\*\s*)?(?:DEZBATERI\s+PARLAMENTARE"
    r"|CAMERA\s+DEPUTA[ȚŢT]ILOR|SENATUL"
    r"|EDITOR\s*:\s*GUVERNUL"
    r"|EDITOR\s*:\s*PARLAMENTUL"
    r"|[ŞȘS]edin[țţt]a\s+din\s+ziua\s+de"
    r"|N\s+O\s+T\s+[ĂA]\s*:?"
    r"|SUMAR"
    r"|Sumar"
    r"|PA\s*R\s*T\s*E\s*A\s+A\s+I\s*I)",
    re.IGNORECASE,
)


def _extract_headings(body: str, offsets: list[int]) -> list[dict[str, Any]]:
    """Build the heading outline.

    Returns one entry per `#` or `##` markdown heading whose text doesn't
    match a skip pattern (banner / footer / SUMAR / NOTĂ / reception session
    line). Strip enclosing `**...**` so the text is reader-friendly.
    """
    out: list[dict[str, Any]] = []
    for m in _HEADING_RE.finditer(body):
        text = m.group("text").strip()
        text = re.sub(r"^\*\*\s*", "", text)
        text = re.sub(r"\s*\*\*$", "", text)
        text = text.strip()
        if not text:
            continue
        if _HEADING_SKIP_RE.match(text):
            continue
        line_no = _line_for_offset(m.start(), offsets)
        out.append(
            {
                "level": len(m.group("hashes")),
                "text": text,
                "line": line_no,
            }
        )
    return out


def _line_for_offset(offset: int, offsets: list[int]) -> int:
    import bisect

    return bisect.bisect_left(offsets, offset) + 1


# -- Boilerplate (rf-specific) ------------------------------------------


_RF_BOILERPLATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # `(RAPOARTE DE ACTIVITATE)` rf-specific marker — the genre header.
    (
        re.compile(
            r"^\s*\(RAPOARTE\s+DE\s+ACTIVITATE\)\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "report_facsimile.rapoarte_genre_marker",
    ),
    # `## **Ședința din ziua de ...**` reception session line.
    (
        re.compile(
            r"^(?:##\s+)?\*?\*?\s*[ŞȘŞS]edin[țţt]a\s+din\s+ziua\s+de[^\n]+?\*?\*?\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "report_facsimile.reception_session_header",
    ),
    # SUMAR keyword + the page/title row that follows.
    (
        re.compile(
            r"^SUMAR(?:\s+Pagina)?[^\n]*$"
            r"(?:\n[^\n]*?Pagina[^\n]*?$)?"
            r"(?:\n\|[-:|\s]+\|\s*$)?"
            r"(?:\n\|[^\n]*\|\s*$)*",
            re.MULTILINE,
        ),
        "report_facsimile.sumar_block",
    ),
    # `N O T Ă: Raportul ... este reprodus în facsimil.` — the genre
    # disclaimer paragraph. Multi-line: claim the heading line + the
    # following sentence-paragraph up to the next blank line.
    (
        re.compile(
            r"^(?:##\s+)?N\s+O\s+T\s+[ĂA]\s*:\s*$"
            r"(?:\s*\n[^\n]+?este\s+reprodus[ăaeio]*\s+[îi]n\s+facsimil\.?\s*$)?",
            re.MULTILINE | re.IGNORECASE,
        ),
        "report_facsimile.facsimil_note",
    ),
    # Single-line variant: `N O T Ă: Raport ... este reprodus în facsimil.`
    (
        re.compile(
            r"^N\s+O\s+T\s+[ĂA]\s*:\s+[^\n]+?este\s+reprodus[ăaeio]*\s+[îi]n\s+facsimil\.?\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "report_facsimile.facsimil_note",
    ),
    # Per-page running header `N MONITORUL OFICIAL AL ROMÂNIEI, PARTEA a II-a, Nr. ...`
    # — universal boilerplate in image-heavy 2014-2017 docs.
    (
        re.compile(
            r"^\d+\s+MONITORUL\s+OFICIAL\s+AL\s+ROM[ÂA]NIEI,\s+PARTEA\s+a\s+II-a[^\n]+$",
            re.MULTILINE,
        ),
        "report_facsimile.page_running_header",
    ),
    # Trailing footer — rf docs use `EDITOR: GUVERNUL` (older) and
    # `EDITOR: PARLAMENTUL` (newer; same as plenary/committee).
    (
        re.compile(
            r"^(?:##\s+)?\*\*EDITOR\s*:\s*(?:GUVERNUL|PARLAMENTUL)\s+ROM[ÂA]NIEI[\s\S]*\Z",
            re.MULTILINE | re.IGNORECASE,
        ),
        "report_facsimile.trailing_footer",
    ),
    # Joint-session header `**ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI
    # SENATULUI** SESIUNEA …` — the same line as committee/plenary joint
    # docs. We claim it as rf-specific so a future bump doesn't have to
    # cross-touch other types' boilerplate.
    (
        re.compile(
            r"^\*\*\s*[ȘŞS]EDIN[ȚŢT]E\s+COMUNE[^\n]*\*\*[^\n]*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "report_facsimile.joint_session_header",
    ),
]


def _claim_rf_boilerplate(body: str, offsets: list[int]) -> list[Claim]:
    out: list[Claim] = []
    for pat, reason in _RF_BOILERPLATE_PATTERNS:
        for m in pat.finditer(body):
            start, end = m.start(), m.end()
            if end <= start:
                continue
            out.append(make_boilerplate_claim((start, end), reason, offsets))
    return out


# -- Report content span -----------------------------------------------


# Anchor for the "report content area": from `(RAPOARTE DE ACTIVITATE)`
# (the genre marker) to the trailing footer (or EOF). The body content is
# the report itself, reproduced verbatim — we claim the whole span as one
# record because the spec stores it in `raw_markdown_excerpt` + the
# sidecar markdown rather than per-section records.
_RAPOARTE_MARKER_RE = re.compile(
    r"^\s*\(RAPOARTE\s+DE\s+ACTIVITATE\)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_TRAILING_FOOTER_OFFSET_RE = re.compile(
    r"^(?:##\s+)?\*\*EDITOR\s*:\s*(?:GUVERNUL|PARLAMENTUL)\s+ROM[ÂA]NIEI",
    re.MULTILINE | re.IGNORECASE,
)


def _report_content_span(body: str) -> tuple[int, int] | None:
    """Find `[start, end)` span of the report content area.

    Start: the `(RAPOARTE DE ACTIVITATE)` line (claim includes the marker
    so the whole rf-specific content area is accounted for in one record).
    End: the trailing footer's `**EDITOR:` line, or EOF when absent.
    Returns None if the marker is missing — fall back to no record claim.
    """
    m_start = _RAPOARTE_MARKER_RE.search(body)
    if not m_start:
        return None
    start = m_start.start()
    m_end = _TRAILING_FOOTER_OFFSET_RE.search(body, pos=start)
    end = m_end.start() if m_end else len(body)
    if end <= start:
        return None
    return (start, end)


# -- Main entrypoint ----------------------------------------------------


def _confidence_for_report(report: dict[str, Any]) -> float:
    """0.6 → 0.95 ramp on field presence."""
    score = 0.6
    if report.get("title"):
        score += 0.15
    if report.get("issuing_body"):
        score += 0.1
    if report.get("reporting_period", {}).get("start"):
        score += 0.05
    if report.get("received_at", {}).get("session_date"):
        score += 0.05
    return min(0.95, score)


def extract(
    ctx: "ExtractContext",
) -> tuple[dict[str, Any], list[Claim]]:
    """Extract a report_facsimile MD into (body_dict, claims).

    Body matches `$defs/ReportFacsimileBody`; claims carry one record claim
    over the rf-specific marker block (RAPOARTE DE ACTIVITATE through end
    of the facsimil note) plus all rf-boilerplate spans (page running
    headers, trailing footer, etc.). The full report body is mostly image-
    only PDF residue in 2014-2017 docs; modern (2024+) docs carry real
    headings that get claimed via the heading record claims.
    """
    body = ctx.body_text
    offsets = ctx.line_offsets

    boilerplate_claims = _claim_rf_boilerplate(body, offsets)
    title = _extract_title(body)
    issuing_body = _extract_issuing_body(title)
    reporting_period = _extract_reporting_period(title, body)
    received_at = _extract_received_at(body, ctx.frontmatter)
    headings = _extract_headings(body, offsets)

    report = {
        "title": title,
        "issuing_body": issuing_body,
        "issuing_body_normalized": None,
        "reporting_period": reporting_period,
        "received_at": received_at,
    }
    body_dict: dict[str, Any] = {
        "report": report,
        "headings": headings,
        "raw_markdown_excerpt": body[:EXCERPT_CHARS],
    }

    # Single record claim covering the report content area (genre marker
    # through trailing footer). The full report text lives in the sidecar
    # markdown; this claim accounts for it in coverage without forcing
    # per-section records on a body the spec calls "reproduced verbatim".
    # Falls back to per-heading claims when the genre marker is absent
    # (rare older docs without the `(RAPOARTE DE ACTIVITATE)` line).
    record_claims: list[Claim] = []
    span = _report_content_span(body)
    if span is not None:
        record_claims.append(make_record_claim(span, offsets))
    else:
        if title:
            m = _SUMAR_TITLE_LINE_RE.search(body) or _BODY_TITLE_HEADING_RE.search(body)
            if m is not None:
                record_claims.append(make_record_claim((m.start(), m.end()), offsets))
        for m in _HEADING_RE.finditer(body):
            text = m.group("text").strip()
            text = re.sub(r"^\*\*\s*|\s*\*\*$", "", text).strip()
            if not text or _HEADING_SKIP_RE.match(text):
                continue
            record_claims.append(make_record_claim((m.start(), m.end()), offsets))

    return body_dict, record_claims + boilerplate_claims


__all__ = ["EXTRACTOR_VERSION", "EXTRACTOR_LABEL", "extract"]
