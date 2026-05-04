"""Extractor for `committee_synthesis` documents.

Weekly synthesis of one or more parliamentary committees' work, published as
MO Partea II issues with a `c` suffix (`13c/2013`, `28c/2025`). The MD layout
is consistent across the 25-year corpus by virtue of the `## N. **<name>**`
per-committee partitioner — all year/era variation falls inside each block.

Recurring layout::

    [shared boilerplate banner]                         (handled by shared helper)

    ## **PA R T E A  A  I I - A SINTEZA LUCRĂRILOR    (committee-specific PARTEA banner)
       COMISIILOR ...**

    Anul N (XXX) — Nr. N/C                              (year banner — shared)

    Weekday, DD month YYYY                              (weekday — shared)

    ## **SINTEZA LUCRĂRILOR COMISIILOR ...
       Perioada: DD[-DD][.MM][-DD.MM].YYYY [SUMAR]**    (period banner)

    ## **SUMAR**                                        (TOC heading; sometimes folded)

    | … SUMAR table … |                                 (TOC table)

    ## 1. **Comisia pentru ...**                        (per-committee block)
        … "Comisia ... și-a desfășurat lucrările
            în zilele de **DD, DD și DD month YYYY**"
        … "în intervalul orar HH:MM-HH:MM"
        … numbered agenda OR tabular agenda
        … numbered roster OR tabular roster OR prose
        PREȘEDINTE, **Name**
        SECRETAR, **Name**

    ## 2. **Comisia pentru ...**
        …

    **EDITOR: PARLAMENTUL ROMÂNIEI ...**                (trailing footer)

The committee-block partitioner is the load-bearing claim — every line
between two consecutive `## N. **Comisia ...**` headers belongs to the
preceding committee. Per-committee fields (dates, format, agenda items,
signatures) are best-effort extracted within the block; ungettable fields
emit `null` / `[]` per the strict schema.

Header-shape variants tolerated:

  - `## N. **Name**`           — modern (most common)
  - `## **N. Name**`           — number inside the bold
  - `## **Comisia ...**`       — number absent (rare older format)

Diacritic-tolerance rules: cedilla forms (`ş`/`ţ`), mojibake (`�` for
diacritics, `„`/`˛`/`ã` from broken transliteration), and stripped ASCII
all map to the same regex via character classes. The 2000-2008 corpus is
where mojibake hits hardest; modern docs (2018+) use clean Unicode.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    line_offsets,
    make_boilerplate_claim,
    make_record_claim,
)
from monitorul_ii.extraction.references import parse_primary_references
from monitorul_ii.extraction.speakers import make_speaker

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext

EXTRACTOR_VERSION = "0.1.0"
EXTRACTOR_LABEL = f"regex@committee_synthesis@{EXTRACTOR_VERSION}"


# -- Period header ----------------------------------------------------------


_ROMANIAN_MONTHS: dict[str, int] = {
    # Modern Unicode
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


# `Perioada: DD.MM–DD.MM.YYYY` (explicit months on both ends)
# `Perioada: DD–DD.MM.YYYY`    (single-month, abbreviated form)
# `Perioada DD.MM…YYYY`        (no colon — older 2003 layout)
# Tolerates dash variants (`-`, `–`, `—`) and the full-stop spacing the
# corpus uses interchangeably (`DD.MM. – DD.MM.YYYY`).
_PERIOD_FULL_RE = re.compile(
    r"Perioada\s*:?\s*"
    r"(?P<d1>\d{1,2})\s*\.\s*(?P<m1>\d{1,2})\s*\.?\s*[-–—]\s*"
    r"(?P<d2>\d{1,2})\s*\.\s*(?P<m2>\d{1,2})\s*\.\s*(?P<y>\d{4})",
    re.IGNORECASE,
)
_PERIOD_SINGLE_MONTH_RE = re.compile(
    r"Perioada\s*:?\s*"
    r"(?P<d1>\d{1,2})\s*[-–—]\s*"
    r"(?P<d2>\d{1,2})\s*\.\s*(?P<m>\d{1,2})\s*\.\s*(?P<y>\d{4})",
    re.IGNORECASE,
)
# `Perioada: DD.MM.YYYY` — single day (rare but observed on one-day inquiry
# committee outputs).
_PERIOD_SINGLE_DAY_RE = re.compile(
    r"Perioada\s*:?\s*"
    r"(?P<d>\d{1,2})\s*\.\s*(?P<m>\d{1,2})\s*\.\s*(?P<y>\d{4})",
    re.IGNORECASE,
)
# `Perioada: 2.08 – 5.08; 16.08 – 19.08.2004` — disjoint multi-window form
# observed in 2003-2004 docs. Collapses to (first DD.MM, first range end's
# DD.MM with the trailing year). Information loss is intentional — the
# alternative (a list of ranges) breaks consumer expectations and the
# discovery loop hasn't surfaced a query that needs it.
_PERIOD_DISJOINT_RE = re.compile(
    r"Perioada\s*:?\s*"
    r"(?P<d1>\d{1,2})\s*\.\s*(?P<m1>\d{1,2})\s*[-–—]\s*"
    r"(?P<d2>\d{1,2})\s*\.\s*(?P<m2>\d{1,2})\s*"
    r"[;,][^\n]*?(?P<y>\d{4})",
    re.IGNORECASE,
)


def _safe_iso(year: int, month: int, day: int) -> str | None:
    if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_period(body: str) -> dict[str, Any]:
    """Extract the `Perioada: …` header into ISO start/end strings.

    Multi-window `Perioada: 2.08 – 5.08; 16.08 – 19.08.2004` collapses to
    the outermost range — small information loss, pragmatic. Returns
    `{"start": null, "end": null}` if no header is found.
    """
    m = _PERIOD_FULL_RE.search(body)
    if m:
        y = int(m.group("y"))
        start = _safe_iso(y, int(m.group("m1")), int(m.group("d1")))
        end = _safe_iso(y, int(m.group("m2")), int(m.group("d2")))
        return {"start": start, "end": end}
    m = _PERIOD_SINGLE_MONTH_RE.search(body)
    if m:
        y = int(m.group("y"))
        mo = int(m.group("m"))
        start = _safe_iso(y, mo, int(m.group("d1")))
        end = _safe_iso(y, mo, int(m.group("d2")))
        return {"start": start, "end": end}
    m = _PERIOD_DISJOINT_RE.search(body)
    if m:
        y = int(m.group("y"))
        # First range only — outermost-window heuristic.
        start = _safe_iso(y, int(m.group("m1")), int(m.group("d1")))
        end = _safe_iso(y, int(m.group("m2")), int(m.group("d2")))
        return {"start": start, "end": end}
    m = _PERIOD_SINGLE_DAY_RE.search(body)
    if m:
        y = int(m.group("y"))
        d = _safe_iso(y, int(m.group("m")), int(m.group("d")))
        return {"start": d, "end": d}
    return {"start": None, "end": None}


# -- Committee block partitioner -------------------------------------------


# Four accepted header shapes, all anchored to a standalone H2 line:
#   `## N. **Comisia ...**`        — number outside bold (most common modern)
#   `## **N. Comisia ...**`        — number inside the bold (older era)
#   `## **Comisia ...**`           — no number (rare; pre-numbering)
#   `## N **. Comisia ...**`       — bold opens between digit and period
#                                     (2004-era PDF-MD conversion quirk)
#
# Inner content must start with `Comisia` (case-insensitive) so signature
# lines like `## **Bogdan-Iulian Huțucă**` do NOT match. Tolerates leading
# whitespace inside the bold (PDF-MD conversion sometimes inserts it).
_COMMITTEE_HEADER_RE = re.compile(
    r"^##\s+(?:"
    r"(?P<ord_outside>\d+)\s*\.\s*\*\*\s*(?P<name1>Comisia[^*\n]+?)\s*\*\*"
    r"|"
    r"(?P<ord_split>\d+)\s+\*\*\s*\.\s*(?P<name3>Comisia[^*\n]+?)\s*\*\*"
    r"|"
    r"\*\*\s*(?P<ord_inside>\d+)\s*\.\s*(?P<name2>Comisia[^*\n]+?)\s*\*\*"
    r"|"
    r"\*\*\s*(?P<name_only>Comisia[^*\n]+?)\s*\*\*"
    r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)


# Trailing footer — appears immediately after the last committee block.
# Recognise the `**EDITOR: PARLAMENTUL ROMÂNIEI` line (optionally H2-
# prefixed in older docs); partition cuts here. Diacritic class
# `[ÂA¬‚]` accepts the modern `Â`, plain `A`, the 2003 mojibake `¬`, and
# the 2004 mojibake `‚` — every observed corpus variant.
_TRAILING_FOOTER_RE = re.compile(
    r"^(?:##\s+)?\*\*EDITOR\s*:\s*PARLAMENTUL\s+ROM[ÂA¬‚]NIEI",
    re.MULTILINE | re.IGNORECASE,
)


def _trailing_footer_offset(body: str) -> int:
    m = _TRAILING_FOOTER_RE.search(body)
    return m.start() if m else len(body)


# -- Per-committee field parsers -------------------------------------------


# Trigger phrase: `Comisia ... și-a desfășurat lucrările în [zilele/ziua/
# perioada] de`. Tolerates cedilla forms (`şi-a desfăşurat`) and the
# slightly different older lead `și-a desfășurat activitatea`. We capture
# only the trigger; date harvesting happens on the trailing window because
# the actual bold-wrapped date span is split inconsistently:
#
#   `în zilele de **DD, DD, DD** și **DD month YYYY**` (two bolds)
#   `în zilele de **DD, DD ... și DD month YYYY**`     (single bold)
#   `în perioada **DD-DD month YYYY**`                  (single bold range)
_DATES_TRIGGER_RE = re.compile(
    r"[șş]i-a\s+desf[ăa][șş]urat\s+"
    r"(?:lucr[ăa]rile|activit[ăa][țţþt]ea)\s+"
    r"(?:[îi]n\s+)?"
    r"(?:zilele\s+de|ziua\s+de|perioada(?:\s+(?:de|cuprins[ăa]))?)\s+",
    re.IGNORECASE,
)
_DAY_NUM_RE = re.compile(r"\b(\d{1,2})\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(?P<month>" + "|".join(_ROMANIAN_MONTHS.keys()) + r")\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
# Strip markdown bold + emphasis markers so day-harvesting doesn't trip on
# them. We don't care about formatting, only the numbers and trailing
# month/year.
_MD_EMPH_RE = re.compile(r"[*_]+")


def _parse_dates(block: str) -> list[str]:
    """Extract ISO dates from the lead "Comisia ... în zilele de DD ... month YYYY".

    Strategy: find the trigger phrase, then scan a 200-char window for the
    first month+year occurrence. Day numbers between the trigger and the
    month/year (after stripping markdown emphasis) become the days. This
    handles single-bold, multi-bold, and bold-less variants uniformly.
    Returns [] when the trigger or the month/year is absent.
    """
    t = _DATES_TRIGGER_RE.search(block)
    if not t:
        return []
    window = block[t.end() : t.end() + 200]
    window = _MD_EMPH_RE.sub("", window)
    my = _MONTH_YEAR_RE.search(window)
    if not my:
        return []
    month = _ROMANIAN_MONTHS[my.group("month").lower()]
    year = int(my.group("year"))
    pre_my = window[: my.start()]
    days: list[int] = []
    for dm in _DAY_NUM_RE.finditer(pre_my):
        d = int(dm.group(1))
        if 1 <= d <= 31:
            days.append(d)
    if not days:
        # `în ziua de DD month YYYY` — the single day is baked into the
        # month/year span itself; rescan with the full window.
        for dm in _DAY_NUM_RE.finditer(window[: my.end()]):
            d = int(dm.group(1))
            if 1 <= d <= 31 and d != year % 100:
                days.append(d)
                break
    out: list[str] = []
    seen: set[str] = set()
    for d in days:
        iso = _safe_iso(year, month, d)
        if iso and iso not in seen:
            seen.add(iso)
            out.append(iso)
    return out


# Time windows: `intervalul orar HH:MM-HH:MM` / `intervalele orare HH:MM-HH:MM,
# respectiv HH:MM-HH:MM` / `de la HH:MM` / `începând cu ora HH:MM`.
# Modern docs use HH.MM (full-stop), older use HH,MM (comma); tolerate both.
_TIME_WINDOW_RE = re.compile(
    r"(?P<sh>\d{1,2})[.:,](?P<sm>\d{2})\s*[-–—]\s*"
    r"(?P<eh>\d{1,2})[.:,](?P<em>\d{2})",
)


def _parse_time_windows(block: str) -> list[dict[str, str | None]]:
    """All (start, end) pairs found in the block, in source order.

    Format strings preserve the source layout (e.g. `15.00`, `15:00`); no
    normalisation. Best-effort and uncalibrated — over-matching here is
    acceptable because v0.1 stores them verbatim.
    """
    out: list[dict[str, str | None]] = []
    for m in _TIME_WINDOW_RE.finditer(block):
        # Filter unrealistic hour/minute combos to avoid catching
        # "art. 1.234" or section refs that look like times.
        sh, sm = int(m.group("sh")), int(m.group("sm"))
        eh, em = int(m.group("eh")), int(m.group("em"))
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
            continue
        out.append(
            {
                "start": f"{sh:02d}:{sm:02d}",
                "end": f"{eh:02d}:{em:02d}",
            }
        )
    return out


# Format markers — mixed wins over online wins over in_person. Mixed is the
# pandemic-and-after default; pure online is rare; pre-2020 default is
# in_person (no positive evidence required).
_FORMAT_MIXED_RE = re.compile(
    r"\b(?:cu\s+prezen[țţt][ăa]\s+fizic[ăa]\s+[șşs]i\s+online"
    r"|prezen[țţt][ăa]\s+fizic[ăa]\s+[șşs]i\s+online"
    r"|în\s+sistem\s+mixt"
    r"|atât\s+la\s+sediul\s+Camerei\s+Deputa[țţt]ilor"
    r"[^\n]*?(?:cât\s+[șşs]i\s+prin|prin)\s+(?:intermediul\s+)?mijloacelor?\s+electronice"
    r"|fizic\s+[șşs]i\s+online)\b",
    re.IGNORECASE,
)
_FORMAT_ONLINE_RE = re.compile(
    r"\b(?:exclusiv\s+(?:prin\s+(?:intermediul\s+)?mijloacelor?\s+electronice|online)"
    r"|integral\s+online"
    r"|în\s+întregime\s+online)\b",
    re.IGNORECASE,
)


def _parse_format(block: str) -> str | None:
    """Detect meeting format from prose markers.

    Returns `mixed` / `online` / None. Pre-pandemic blocks (no markers,
    pre-2020 dates) leave format null — the schema's null is the honest
    answer when there's no positive evidence. Caller may default to
    `in_person` based on date.
    """
    if _FORMAT_MIXED_RE.search(block):
        return "mixed"
    if _FORMAT_ONLINE_RE.search(block):
        return "online"
    return None


# Purpose markers — most blocks are dezbatere_decizie (no positive
# marker); `documentare și consultare` and `audiere ... candidat` are the
# explicit signals worth picking up.
_PURPOSE_DOC_RE = re.compile(
    r"\bdocumentare\s+[șşs]i\s+consultare\b",
    re.IGNORECASE,
)
_PURPOSE_AUDIERE_RE = re.compile(
    r"\bAudierea?\s+(?:domnului|doamnei)?[^\n]{0,80}?candidat\s+pentru\s+ocuparea\s+func[țţt]iei",
    re.IGNORECASE,
)
_PURPOSE_RAPORT_RE = re.compile(
    r"\baprobarea?\s+raport(?:ului)?\b",
    re.IGNORECASE,
)


def _parse_purpose(block: str) -> str | None:
    if _PURPOSE_AUDIERE_RE.search(block):
        return "audiere_candidați"
    if _PURPOSE_DOC_RE.search(block):
        return "documentare_consultare"
    if _PURPOSE_RAPORT_RE.search(block):
        return "aprobare_raport"
    return None


# -- Signature extraction ---------------------------------------------------


# `PREȘEDINTE,` followed by `**Name**` — modern form. Mojibake `PRE�EDINTE`
# from broken UTF-8 + cedilla `PREŞEDINTE` both accepted. Name captured
# inside `**...**` either on the same line or the next H2 line.
_PRESIDENTE_PATTERNS = [
    re.compile(
        r"\bPRE(?:[ȘŞS�])?EDINTE\s*,\s*\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bPRE(?:[ȘŞS�])?EDINTE\s*,?\s*\n+##\s*\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bPRE(?:[ȘŞS�])?EDINTE\s*,?\s*\n+\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
]
# `SECRETAR,` followed by `**Name**` — same shape.
_SECRETAR_PATTERNS = [
    re.compile(
        r"(?<!\w)SECRETAR\s*,\s*\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)##\s+SECRETAR\s*,\s*\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)SECRETAR\s*,?\s*\n+##\s*\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)SECRETAR\s*,?\s*\n+\*\*\s*(?P<name>[^*\n]+?)\s*\*\*",
        re.IGNORECASE,
    ),
]


def _norm_name(s: str) -> str:
    return " ".join(s.split())


def _first_match(patterns: list[re.Pattern[str]], text: str) -> str | None:
    """First non-empty `name` group across the pattern list."""
    for pat in patterns:
        m = pat.search(text)
        if m:
            name = _norm_name(m.group("name"))
            if name:
                return name
    return None


def _signature_speaker(
    name: str | None, title: str | None = None
) -> dict[str, Any] | None:
    if not name:
        return None
    return make_speaker(raw=name, name=name, title=title)


def _parse_signatures(
    block: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (chair_speaker, secretary_speaker) extracted from PREȘEDINTE
    / SECRETAR signature lines. Either may be None when not present.
    """
    chair_name = _first_match(_PRESIDENTE_PATTERNS, block)
    secretary_name = _first_match(_SECRETAR_PATTERNS, block)
    return _signature_speaker(chair_name), _signature_speaker(secretary_name)


# -- Committee kind classifier ---------------------------------------------


def _classify_kind(name: str) -> str:
    """Map the committee name to the schema's `kind` enum.

    `Comisia specială` / `Comisia de anchetă` are the explicit non-standard
    forms; everything else is `permanent`. Joint-with-Senate is detected
    via the `permanentă comună` / `permanentă a Camerei Deputaților și
    Senatului` sub-strings.
    """
    n = name.lower()
    is_joint = (
        "permanent[ăa]\\s+comun[ăa]" in n
        or "permanent[ăa]\\s+a\\s+camerei" in n
        or "permanentă comună" in n
        or "permanenta comuna" in n
        or "deputaților și senatului" in n
        or "deputatilor si senatului" in n
    )
    if "specială" in n or "speciala" in n or "special„" in n or "specialã" in n:
        return "special_joint" if is_joint else "special"
    if "anchetă" in n or "ancheta" in n:
        return "inquiry_joint" if is_joint else "inquiry"
    return "permanent"


# -- Committee name normalisation -------------------------------------------


def _clean_committee_name(name: str) -> str:
    """Trim trailing TOC-page noise + collapse whitespace.

    The PDF→MD convert sometimes leaves a `(Perioada N–N septembrie 2001)`
    parenthetical or a trailing `.....N–N` page reference inside the bold;
    we keep the parenthetical (load-bearing for joint-meeting context) but
    strip the page-noise dots.
    """
    cleaned = re.sub(r"\.{3,}\s*\d+(?:[-–]\d+)?\s*$", "", name)
    return _norm_name(cleaned)


# -- Agenda items -----------------------------------------------------------


# Numbered narrative item: `^N. <title>.` (multi-line title acceptable; we
# stop at a blank line followed by another numbered item or section break).
# Tolerates leading whitespace from MD list-indent quirks.
_AGENDA_ITEM_RE = re.compile(
    r"^\s*(?P<ord>\d+)\.\s+(?P<title>[^\n]+(?:\n(?!\s*\d+\.\s|\s*$)[^\n]+)*)",
    re.MULTILINE,
)
# Item title hooks for committee_role + output_type. The title regularly
# carries `(PL-x N/Y; aviz)` / `(PL-x N/Y – raport preliminar pentru …)`.
_ROLE_FOND_RE = re.compile(r"\bfond\s+comun\b|\bfond\s+împreună\b", re.IGNORECASE)
_ROLE_FOND_SOLE_RE = re.compile(
    r"\b(?:sesiz(?:are|at[ăa])\s+)?(?:în\s+)?fond\b",
    re.IGNORECASE,
)
_ROLE_AVIZ_RE = re.compile(r"\baviz\b(?!\s+pentru\s+Comisi)", re.IGNORECASE)
_ROLE_AVIZ_FOR_RE = re.compile(r"\baviz\s+pentru\s+Comisi", re.IGNORECASE)

_OUTPUT_RAPORT_PRELIM_RE = re.compile(
    r"\braport\s+preliminar\b",
    re.IGNORECASE,
)
_OUTPUT_RAPORT_SUPL_RE = re.compile(r"\braport\s+suplimentar\b", re.IGNORECASE)
_OUTPUT_RAPORT_COMUN_SUPL_RE = re.compile(
    r"\braport\s+comun\s+suplimentar\b",
    re.IGNORECASE,
)
_OUTPUT_RAPORT_COMUN_RE = re.compile(r"\braport\s+comun\b", re.IGNORECASE)
_OUTPUT_RAPORT_RE = re.compile(r"\braport(?:ul)?\b", re.IGNORECASE)
_OUTPUT_AVIZ_RE = re.compile(r"\baviz(?:are)?\b", re.IGNORECASE)
_OUTPUT_STUDIU_RE = re.compile(r"\bstudiu\b", re.IGNORECASE)
_OUTPUT_PROIECT_OPINIE_RE = re.compile(
    r"\bproiect\s+de\s+opinie\b",
    re.IGNORECASE,
)
_OUTPUT_AMANARE_RE = re.compile(
    r"\b(?:am[âa]nat[ăa]?|am[âa]nare)\b",
    re.IGNORECASE,
)


def _detect_committee_role(title: str) -> str | None:
    if _ROLE_FOND_RE.search(title):
        return "fond_comun"
    if _ROLE_AVIZ_RE.search(title) or _ROLE_AVIZ_FOR_RE.search(title):
        return "aviz"
    if _ROLE_FOND_SOLE_RE.search(title):
        return "fond"
    return None


def _detect_output_type(title: str) -> str | None:
    """Order matters: longest specific match first, then the generic
    `raport` / `aviz` / `amânare` fall-throughs.
    """
    if _OUTPUT_RAPORT_COMUN_SUPL_RE.search(title):
        return "raport_comun_suplimentar"
    if _OUTPUT_RAPORT_COMUN_RE.search(title):
        return "raport_comun"
    if _OUTPUT_RAPORT_PRELIM_RE.search(title):
        return "raport_preliminar"
    if _OUTPUT_RAPORT_SUPL_RE.search(title):
        return "raport_suplimentar"
    if _OUTPUT_PROIECT_OPINIE_RE.search(title):
        return "proiect_de_opinie"
    if _OUTPUT_STUDIU_RE.search(title):
        return "studiu"
    if _OUTPUT_AMANARE_RE.search(title):
        return "amânare"
    if _OUTPUT_RAPORT_RE.search(title):
        return "raport"
    if _OUTPUT_AVIZ_RE.search(title):
        return "aviz"
    return None


# Outcome-text scan window — first paragraph after the agenda title that
# starts with `În urma` / `Supusă la vot` / `Proiectul de lege a fost ...`
# / `Propunerea legislativă a fost ...` / `Amânat` / `Amânat[ăa]`.
_OUTCOME_LEAD_RE = re.compile(
    r"\b(?:În\s+urma|Supus[ăa]\s+la\s+vot|Proiectul\s+de\s+lege\s+a\s+fost"
    r"|Propunerea\s+legislativ[ăa]\s+a\s+fost"
    r"|Am[âa]nat[ăa]?|Membrii\s+comisiei\s+au\s+hot[ăa]r[âa]t)\b",
    re.IGNORECASE,
)


def _strip_title_trailing_dot(s: str) -> str:
    s = s.rstrip()
    while s.endswith("."):
        s = s[:-1].rstrip()
    return s


def _extract_outcome_text(after_title: str, max_chars: int = 600) -> str | None:
    """First paragraph (separated by blank line) after the title that
    starts with an outcome-lead phrase. Capped at `max_chars` to avoid
    ballooning the sidecar with multi-paragraph dezbateri text.
    """
    if not after_title:
        return None
    # Split into paragraphs and find the first that starts with an outcome lead.
    paragraphs = re.split(r"\n\s*\n", after_title)
    for para in paragraphs[:5]:  # bound the look-ahead
        p = para.strip()
        if not p:
            continue
        if _OUTCOME_LEAD_RE.match(p):
            if len(p) > max_chars:
                p = p[: max_chars - 1] + "…"
            return p
    return None


# Vote-summary lead phrases inside outcome text: "cu majoritate de voturi
# (N pentru, M împotrivă, K abțineri)" / "cu unanimitate de voturi" /
# "în unanimitate".
_VOTE_UNANIMOUS_RE = re.compile(
    r"\b(?:cu\s+unanimitate(?:a)?\s+(?:de\s+)?voturi(?:lor)?|în\s+unanimitate)\b",
    re.IGNORECASE,
)
_VOTE_MAJORITY_RE = re.compile(
    r"\b(?:cu\s+majoritate\s+(?:de\s+)?voturi)\b",
    re.IGNORECASE,
)
_VOTE_AGAINST_RE = re.compile(
    r"(\d+)\s+(?:vot(?:uri)?|de\s+voturi)?\s*(?:împotriv[ăa]|contra)",
    re.IGNORECASE,
)
_VOTE_ABSTAIN_RE = re.compile(
    r"(\d+|dou[ăa]|trei|patru|cinci)\s+(?:ab[țţt]ineri?|de\s+ab[țţt]ineri)",
    re.IGNORECASE,
)
_VOTE_FOR_RE = re.compile(
    r"(\d+)\s+(?:de\s+)?voturi?\s+pentru\b",
    re.IGNORECASE,
)
_RO_NUM_WORDS: dict[str, int] = {
    "două": 2,
    "doua": 2,
    "trei": 3,
    "patru": 4,
    "cinci": 5,
}
_OUTCOME_DEFER_RE = re.compile(
    r"\b(?:am[âa]nat[ăa]?|s-a\s+amânat|amânare(?:a)?)\b",
    re.IGNORECASE,
)
# Outcome verbs — we allow any trailing inflection (`-at[ăaei]`, `-are[ai]?`,
# `-ării?`, plurals). Stem-based matching: the verb-stem prefix is followed
# by `\w*` to swallow Romanian noun/verb inflections (genitive `adoptarea`,
# plural `aprobate`, dative `aprobării`, etc.). Word-boundary anchors keep
# us out of substring traps like `aprobativă`.
_OUTCOME_REJECT_RE = re.compile(
    r"\b(?:respin(?:s|g)\w*|nefavorabil\w*|aviz(?:\s+|are\s+)negativ\w*"
    r"|raport\s+de\s+respingere)\b",
    re.IGNORECASE,
)
_OUTCOME_APPROVE_RE = re.compile(
    r"\b(?:aprob\w*|adopt\w*|favorabil\w*|aviz(?:\s+|are\s+)favorabil\w*"
    r"|raport\s+de\s+adoptare)\b",
    re.IGNORECASE,
)


def _word_to_int(s: str) -> int | None:
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return _RO_NUM_WORDS.get(s)


def _parse_vote_summary(outcome_text: str | None) -> dict[str, Any] | None:
    """Best-effort vote-summary extraction from the narrative outcome text.

    Returns None when no outcome verb is detectable (the schema's null is
    the honest answer). When an outcome verb is present, returns a dict
    with majority + counts filled where extractable; null otherwise.
    """
    if not outcome_text:
        return None
    outcome: str | None = None
    if _OUTCOME_DEFER_RE.search(outcome_text):
        outcome = "deferred"
    elif _OUTCOME_REJECT_RE.search(outcome_text):
        outcome = "rejected"
    elif _OUTCOME_APPROVE_RE.search(outcome_text):
        outcome = "approved"
    if outcome is None:
        return None
    majority: str | None = None
    if _VOTE_UNANIMOUS_RE.search(outcome_text):
        majority = "unanimous"
    elif _VOTE_MAJORITY_RE.search(outcome_text):
        majority = "majority"
    against: int | None = None
    abstain: int | None = None
    voted_for: int | None = None
    m = _VOTE_AGAINST_RE.search(outcome_text)
    if m:
        try:
            against = int(m.group(1))
        except ValueError:
            against = None
    m = _VOTE_ABSTAIN_RE.search(outcome_text)
    if m:
        n = _word_to_int(m.group(1))
        if n is not None:
            abstain = n
    m = _VOTE_FOR_RE.search(outcome_text)
    if m:
        try:
            voted_for = int(m.group(1))
        except ValueError:
            voted_for = None
    return {
        "outcome": outcome,
        "majority": majority,
        "for": voted_for,
        "against": against,
        "abstain": abstain,
        "amendments_passed": None,
    }


def _split_agenda(block: str) -> list[tuple[int, int, str]]:
    """Find numbered agenda items inside a committee block.

    Returns `(ordinal, start_offset, title)` triples. Limits ordinals to
    1..200 to avoid catching unrelated numerics (article references in
    quoted law text).
    """
    items: list[tuple[int, int, str]] = []
    for m in _AGENDA_ITEM_RE.finditer(block):
        ordinal = int(m.group("ord"))
        if not (1 <= ordinal <= 200):
            continue
        title = _strip_title_trailing_dot(_norm_name(m.group("title")))
        if not title:
            continue
        items.append((ordinal, m.start(), title))
    # Filter spurious matches: keep only items where the ordinal sequence
    # makes sense as an agenda list (1, 2, 3, … with at most one restart).
    filtered: list[tuple[int, int, str]] = []
    last_ord = 0
    for ord_, off, t in items:
        if ord_ == last_ord + 1 or (ord_ == 1 and last_ord >= 1):
            filtered.append((ord_, off, t))
            last_ord = ord_
    return filtered


def _build_agenda(
    block: str,
    block_start_global: int,
    ctx: "ExtractContext",
) -> list[dict[str, Any]]:
    items = _split_agenda(block)
    if not items:
        return []
    out: list[dict[str, Any]] = []
    for i, (ordinal, local_off, title) in enumerate(items):
        next_off = items[i + 1][1] if i + 1 < len(items) else len(block)
        # Reference parsing on the title only (not the entire body) — avoids
        # over-matching bill cites in dezbateri commentary.
        refs = parse_primary_references(title)
        # Filter implausibly-yeared cites — OCR typos like `PL-x 527/2917`
        # appear in 2003/2018/2021/2022 docs and the schema's [1990, 2100]
        # range correctly rejects them. Dropping at this layer keeps the
        # references parser uncontaminated; the cite is preserved in
        # `outcome_text` if present in the surrounding paragraph.
        refs = [r for r in refs if 1990 <= int(r.get("year") or 0) <= 2100]
        # Re-route char_offsets onto the global-body coordinate system. The
        # title we parsed is a substring of the full block; we don't track
        # title-vs-block offsets here, so reset to [0,0] when refs are found
        # to keep the offsets schema-compliant. The text is preserved in
        # `raw`, which is what matters for downstream queries.
        title_offset_global = block_start_global + local_off
        for r in refs:
            r["char_offsets"] = [title_offset_global, title_offset_global]
        body_after_title = block[local_off + len(title) : next_off]
        outcome_text = _extract_outcome_text(body_after_title)
        committee_role = _detect_committee_role(title)
        output_type = _detect_output_type(title) or _detect_output_type(
            outcome_text or ""
        )
        item_global_start = block_start_global + local_off
        item_global_end = block_start_global + next_off
        record = {
            "ordinal": ordinal,
            "title": title,
            "primary_references": refs,
            "co_committees": [],
            "committee_role": committee_role,
            "output_type": output_type,
            "for_committees": [],
            "outcome_text": outcome_text,
            "vote_summary": _parse_vote_summary(outcome_text),
            "source_span": ctx.make_source_span((item_global_start, item_global_end)),
            "extraction": {
                "extractor": EXTRACTOR_LABEL,
                "confidence": 0.0,
                "source_span": ctx.make_source_span(
                    (item_global_start, item_global_end)
                ),
            },
        }
        record["extraction"]["confidence"] = round(_agenda_confidence(record), 4)
        out.append(record)
    return out


def _agenda_confidence(item: dict[str, Any]) -> float:
    """0.6 → 0.95 ramp on field presence, mirroring qr."""
    score = 0.6
    if item["primary_references"]:
        score += 0.15
    if item["committee_role"]:
        score += 0.05
    if item["output_type"]:
        score += 0.05
    if item["outcome_text"]:
        score += 0.05
    if item["vote_summary"]:
        score += 0.05
    return min(0.95, score)


# -- Boilerplate (committee-specific) --------------------------------------


_COMMITTEE_BOILERPLATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Committee-specific PARTEA banner: `## **PA R T E A A I I - A SINTEZA
    # LUCRĂRILOR COMISIILOR ...**`. Distinct from the shared `... DEZBATERI
    # PARLAMENTARE` banner so the discovery loop attributes blame correctly.
    # Diacritic class `[ĂÃA√]` accepts modern Unicode (`Ă`), 2003-era
    # mojibake (`Ã`/`√`), and stripped ASCII (`A`).
    (
        re.compile(
            r"^#{1,2}\s+\*\*[^\n]*PA\s*R\s*T\s*E\s*A\s+A\s+I\s*I\s*-\s*A"
            r"[^\n]*?SINTEZA\s+LUCR[ĂÃA√]RILOR[^\n]*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "committee_synthesis.partea_sinteza_banner",
    ),
    # Standalone `## **SINTEZA LUCRĂRILOR COMISIILOR ...**` heading (plural)
    # OR `# **SINTEZA LUCRĂRILOR COMISIEI ...**` (singular form for
    # single-committee docs). Either H1 or H2 prefix; converter sometimes
    # picks H1 for the wrapper.
    (
        re.compile(
            r"^#{1,2}\s+\*\*\s*SINTEZA\s+LUCR[ĂÃA√]RILOR"
            r"\s+(?:COMISIILOR|COMISIEI)[^\n]*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "committee_synthesis.sinteza_heading",
    ),
    # Standalone `## **Perioada: …**` heading (older docs render it as its
    # own H2 line instead of folding it into the SINTEZA heading). Colon
    # optional — the 2003 corpus drops it.
    (
        re.compile(
            r"^##\s+\*\*\s*Perioada\b[^\n]*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "committee_synthesis.perioada_heading",
    ),
    # Standalone `## **SUMAR**` / `**SUMAR**` heading.
    (
        re.compile(
            r"^(?:##\s+)?\*\*\s*SUMAR\s*\*\*\s*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "committee_synthesis.sumar_heading",
    ),
    # SUMAR table — markdown table whose first row is the cellpack header
    # we observe (`|Nr.<br>...|Pagina|`). Conservative match: a table row
    # containing both `Comisia` and `Pagina` in its cell text plus the
    # subsequent separator + body rows. Claim is line-anchored so it
    # doesn't bleed into committee blocks.
    (
        re.compile(
            r"^\|[^\n]*?Pagina[^\n]*\|\s*$"
            r"(?:\n\|[-:|\s]+\|\s*$)?"
            r"(?:\n\|[^\n]*\|\s*$)*",
            re.MULTILINE,
        ),
        "committee_synthesis.sumar_table",
    ),
    # Trailing footer block: from `**EDITOR: PARLAMENTUL ROMÂNIEI` to EOF.
    # Optional `## ` H2 prefix matches older docs that render the footer
    # as a heading. Mojibake variants `¬`/`‚` in place of `Â` are
    # accepted (2003 / 2004 era).
    (
        re.compile(
            r"^(?:##\s+)?\*\*EDITOR\s*:\s*PARLAMENTUL\s+ROM[ÂA¬‚]NIEI[\s\S]*\Z",
            re.MULTILINE | re.IGNORECASE,
        ),
        "committee_synthesis.trailing_footer",
    ),
]


def _claim_committee_boilerplate(body: str, offsets: list[int]) -> list[Claim]:
    out: list[Claim] = []
    for pat, reason in _COMMITTEE_BOILERPLATE_PATTERNS:
        for m in pat.finditer(body):
            start, end = m.start(), m.end()
            if end <= start:
                continue
            out.append(make_boilerplate_claim((start, end), reason, offsets))
    return out


# -- Per-committee block extraction ----------------------------------------


def _committee_confidence(committee: dict[str, Any]) -> float:
    """Aggregate confidence for the committee record itself.

    Mean of agenda-item confidences when agenda is non-empty; otherwise a
    0.6 → 0.85 ramp on signature/dates presence so empty-agenda blocks
    don't drag the document mean to zero.
    """
    items = (committee.get("meetings") or [{}])[0].get("agenda") or []
    if items:
        scores = [
            (it.get("extraction") or {}).get("confidence", 0.0)
            for it in items
            if isinstance((it.get("extraction") or {}).get("confidence"), (int, float))
        ]
        if scores:
            return sum(scores) / len(scores)
    score = 0.6
    if committee.get("chair") is not None:
        score += 0.1
    if committee.get("secretary") is not None:
        score += 0.05
    meeting = (committee.get("meetings") or [{}])[0]
    if meeting.get("dates"):
        score += 0.1
    return min(0.85, score)


def _build_committee(
    *,
    raw_name: str,
    block_start: int,
    block_end: int,
    block: str,
    ctx: "ExtractContext",
) -> dict[str, Any]:
    name = _clean_committee_name(raw_name)
    chair, secretary = _parse_signatures(block)
    dates = _parse_dates(block)
    time_windows = _parse_time_windows(block)
    fmt = _parse_format(block)
    purpose = _parse_purpose(block)
    agenda = _build_agenda(block, block_start, ctx)
    meeting_span = ctx.make_source_span((block_start, block_end))
    meeting = {
        "dates": dates,
        "time_windows": time_windows,
        "format": fmt,
        "purpose": purpose,
        "joint_with": [],
        "roster": [],
        "agenda": agenda,
        "source_span": meeting_span,
        "extraction": {
            "extractor": EXTRACTOR_LABEL,
            "confidence": 0.0,
            "source_span": meeting_span,
        },
    }
    committee = {
        "name": name,
        "kind": _classify_kind(name),
        "chair": chair,
        "secretary": secretary,
        "meetings": [meeting],
        "source_span": ctx.make_source_span((block_start, block_end)),
        "extraction": {
            "extractor": EXTRACTOR_LABEL,
            "confidence": 0.0,
            "source_span": ctx.make_source_span((block_start, block_end)),
        },
    }
    score = round(_committee_confidence(committee), 4)
    committee["extraction"]["confidence"] = score
    meeting["extraction"]["confidence"] = score
    return committee


# Single-committee fallback: 2002-2004 docs that report ONE committee's
# multi-session work use `SINTEZA LUCRĂRILOR COMISIEI` (singular) and have
# no `## N. **Comisia ...**` headers. The committee name appears as the
# first SUMAR row (`1. Comisia <name>...PAGE-RANGE`).
_SUMAR_FIRST_COMMITTEE_RE = re.compile(
    r"^\s*1\.\s*(?P<name>Comisia[^\n]+?)(?:\.{3,}|\s+\d+(?:[-–]\d+)?\s*$)",
    re.MULTILINE,
)
# Fallback name from the SINTEZA heading singular form (when the SUMAR
# anchor is absent / mangled). Pulls everything between
# `LUCRĂRILOR COMISIEI` and the closing `**`.
_SINTEZA_SINGULAR_NAME_RE = re.compile(
    r"\bSINTEZA\s+LUCR[ĂÃA√]RILOR\s+COMISIEI(?P<name>[^*\n]+?)\*\*",
    re.IGNORECASE,
)


# Last-ditch prose-opening detector: docs that open with
# `Comisiile pentru ...` / `Comisia pentru ...` directly (no SUMAR, no
# numbered headers) — observed in the December 2004 budget-debate sub-
# committee output. Captures everything from `Comisi[ai]` to the first
# `s-au întrunit` / `și-a desfășurat` / `au dezbătut` trigger.
_PROSE_FIRST_COMMITTEE_RE = re.compile(
    r"^\s*(?P<name>Comisi(?:a|ile)\s+pentru[^\n.]{5,200}?)\s+"
    r"(?:s-au\s+[îÓo]ntrunit|[șş]i-au?\s+desf[ăa][șş]urat|au\s+dezb[ăa]tut)",
    re.MULTILINE | re.IGNORECASE,
)


def _detect_single_committee_name(body: str) -> str | None:
    """Find the single committee's name from SUMAR / SINTEZA heading / prose.

    Three fallback anchors, tried in order:
    1. SUMAR's first row (`1. Comisia <name>...PAGE`)
    2. SINTEZA singular heading (`SINTEZA LUCRĂRILOR COMISIEI <descriptor>`)
    3. Body's first prose `Comisi[ai] pentru ...` opening

    Returns the cleaned name, or None when no anchor matches. Used only
    when the partitioner finds zero `## N. **Comisia ...**` headers.
    """
    m = _SUMAR_FIRST_COMMITTEE_RE.search(body)
    if m:
        return _clean_committee_name(m.group("name"))
    m = _SINTEZA_SINGULAR_NAME_RE.search(body)
    if m:
        # The heading carries the descriptor in the genitive ("ÎEI PENTRU
        # ELABORAREA ..."). Prepend "Comisia" to recover the canonical
        # nominative form.
        descriptor = _norm_name(m.group("name"))
        if descriptor:
            return f"Comisia {descriptor}"
    m = _PROSE_FIRST_COMMITTEE_RE.search(body)
    if m:
        return _norm_name(m.group("name"))
    return None


def extract(
    ctx: "ExtractContext",
) -> tuple[dict[str, Any], list[Claim]]:
    """Extract a committee_synthesis MD into (body_dict, claims).

    Body matches `$defs/CommitteeSynthesisBody`; claims carry the committee
    block partition (record claims) + committee-specific boilerplate
    (heading + SUMAR + trailing footer). Shared MO boilerplate is added by
    the dispatcher.
    """
    body = ctx.body_text
    offsets = ctx.line_offsets

    boilerplate_claims = _claim_committee_boilerplate(body, offsets)
    period = _parse_period(body)

    headers = list(_COMMITTEE_HEADER_RE.finditer(body))
    footer_off = _trailing_footer_offset(body)

    record_claims: list[Claim] = []
    committees: list[dict[str, Any]] = []

    if headers:
        for i, m in enumerate(headers):
            block_start = m.start()
            if i + 1 < len(headers):
                block_end = headers[i + 1].start()
            else:
                block_end = footer_off
            block_end = min(block_end, footer_off)
            if block_end <= block_start:
                continue
            block_text = body[block_start:block_end]
            raw_name = (
                m.group("name1")
                or m.group("name2")
                or m.group("name3")
                or m.group("name_only")
                or ""
            )
            committees.append(
                _build_committee(
                    raw_name=raw_name,
                    block_start=block_start,
                    block_end=block_end,
                    block=block_text,
                    ctx=ctx,
                )
            )
            record_claims.append(make_record_claim((block_start, block_end), offsets))
    else:
        # Single-committee fallback: claim the entire body (up to the
        # trailing footer) as one committee record. Name lifted from the
        # SUMAR's first row OR from the SINTEZA-singular heading. When
        # neither anchor fires, emit no record (rare; coverage stays at
        # whatever the boilerplate claims provide).
        single_name = _detect_single_committee_name(body)
        if single_name:
            block_start = 0
            block_end = footer_off if footer_off > 0 else len(body)
            block_text = body[block_start:block_end]
            committees.append(
                _build_committee(
                    raw_name=single_name,
                    block_start=block_start,
                    block_end=block_end,
                    block=block_text,
                    ctx=ctx,
                )
            )
            record_claims.append(make_record_claim((block_start, block_end), offsets))

    body_dict: dict[str, Any] = {
        "period": period,
        "committees": committees,
    }
    return body_dict, record_claims + boilerplate_claims


__all__ = ["EXTRACTOR_VERSION", "EXTRACTOR_LABEL", "extract"]


# Re-export `line_offsets` for tests that exercise the boilerplate helper
# without spinning up a full ExtractContext.
_line_offsets = line_offsets
