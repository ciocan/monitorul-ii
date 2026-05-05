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

EXTRACTOR_VERSION = "0.2.0"
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


_JOINT_NAME_MARKERS_RE = re.compile(
    r"\bcomun[ăaã]?\b"
    r"|\bdeputa[țţţt]ilor\s+[șşs][iî]\s+senatului\b"
    r"|\bdeputa[țţţt]ilor\s+si\s+senatului\b",
    re.IGNORECASE,
)
_SPECIAL_NAME_RE = re.compile(
    r"\bspecial(?:[ăaã]|„)",
    re.IGNORECASE,
)
_INQUIRY_NAME_RE = re.compile(
    r"\banchet[ăaã]\b",
    re.IGNORECASE,
)


def _classify_kind(name: str) -> str:
    """Map the committee name to the schema's `kind` enum.

    `Comisia specială` / `Comisia de anchetă` are the explicit non-standard
    forms; everything else is `permanent`. Joint-with-Senate is detected
    via the `comună` modifier or the `Camerei Deputaților și Senatului`
    co-anchor.

    v0.2.0 extends the joint detection to graduate the **joint permanent**
    cohort — ~10-30 docs in the corpus carry `Comisia permanentă comună a
    Camerei Deputaților și Senatului ...` (UNESCO, securitate națională,
    Statutul deputaților) which were previously classified as plain
    `permanent`. They now resolve to `special_joint`, treating the joint
    variant of permanent as "permanent + special parliamentary status".
    """
    n = name.lower()
    is_joint = bool(_JOINT_NAME_MARKERS_RE.search(n))
    is_special = bool(_SPECIAL_NAME_RE.search(n))
    is_inquiry = bool(_INQUIRY_NAME_RE.search(n))
    if is_inquiry:
        return "inquiry_joint" if is_joint else "inquiry"
    if is_special:
        return "special_joint" if is_joint else "special"
    if is_joint:
        return "special_joint"
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
    if items:
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
                "source_span": ctx.make_source_span(
                    (item_global_start, item_global_end)
                ),
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
    # Numbered-narrative parsing found nothing — fall back to tabular if a
    # `|Nr.|...|PL-x|...|` table is present (2024+ format). Coverage was
    # already held by the partition claim, but agenda_items[] would be
    # empty for ~10-15% of post-2024 docs without this branch.
    return _build_tabular_agenda(block, block_start_global, ctx)


# Tabular agenda header: a row containing both `Nr` and (`PL-x` or
# `Titlu` adjacent to `Rezolu[țt]ie` / `Scopul`). The 2025 corpus splits
# the column header across the same row in all observed variants — each
# is anchored by the literal `Nr` token + a PL-x / Titlu / Scopul tag.
_TABULAR_AGENDA_HEADER_RE = re.compile(
    r"^\|\s*(?:[^|\n]*?\bNr[\.<]|Nr\.<br)[^\n]*?"
    r"(?:PL-?x|P\s*L-?x|Titlu|Scopul|Rezolu[țţt]ie)[^\n]*\|",
    re.IGNORECASE | re.MULTILINE,
)
_TABULAR_AGENDA_ORDINAL_RE = re.compile(r"^(\d+)\.?$")
_TABULAR_AGENDA_PLX_RE = re.compile(
    r"\b(?:PL-?x|Pl-?x)\b",
    re.IGNORECASE,
)
# Outcome-cell signature: "În urma" / "În formă" / "Aprobat"-style verbs
# anchored at the start of the cell (post-<br> stripped).
_TABULAR_OUTCOME_LEAD_RE = re.compile(
    r"^(?:[ÎIi]n\s+urma|[ÎIi]n\s+formă|deputa[țţt]ii\s+prezen[țţt]i|cu\s+majoritate"
    r"|cu\s+unanimitate|Supus[ăa]\s+la\s+vot|Aprobat|Respin[gs])",
    re.IGNORECASE,
)
# Role-cell signature: tabular Scopul cells use one of the canonical
# committee-role tokens at the start (Raport / Aviz / Studiu / etc.).
_TABULAR_ROLE_LEAD_RE = re.compile(
    r"^(?:Raport|Aviz|Studiu|Proiect\s+de\s+opinie|Am[âa]nare)\b",
    re.IGNORECASE,
)


def _strip_table_cell(cell: str) -> str:
    """Strip <br> markers and collapse whitespace for tabular cell text."""
    s = _HEADER_BR_RE.sub(" ", cell)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _split_tabular_data_row(line: str) -> list[str] | None:
    """Return the trimmed/<br>-collapsed cells of a markdown data row, or None
    if the line is not a data row (header, separator, blank).
    """
    if not line.startswith("|") or _is_separator_row(line):
        return None
    raw = _split_table_row(line)
    if not raw:
        return None
    cells = [_strip_table_cell(c) for c in raw]
    # Drop pure-empty rows
    if not any(c for c in cells):
        return None
    return cells


def _build_tabular_agenda(
    block: str,
    block_start_global: int,
    ctx: "ExtractContext",
) -> list[dict[str, Any]]:
    """Parse `|Nr.|PL-x|Titlu|Scopul|Rezoluție|`-style agendas (2024+).

    Walk the block line-by-line. After each tabular agenda header, scan
    subsequent rows; treat any row whose first non-empty cell is `\\d+\\.?`
    as an item. Within each item, identify cells by content fingerprint:

      - First numeric cell  → ordinal
      - Cell with `PL-x` token → primary_references
      - Cell starting with `Raport`/`Aviz`/`Studiu` → committee_role / output_type
      - Cell starting with `În urma...` / outcome verb → outcome_text
      - Longest remaining cell → title

    Robust to column re-orderings and variable inter-cell empty padding —
    the 2025 corpus uses 5 to 14 cells per row depending on PDF→MD pagination.
    """
    items: list[tuple[int, int, dict[str, str]]] = []
    in_table = False
    pending_row_offset = 0
    line_offset = 0
    for line in block.splitlines(keepends=True):
        line_no_nl = line.rstrip("\n")
        if _TABULAR_AGENDA_HEADER_RE.match(line_no_nl):
            in_table = True
            line_offset += len(line)
            continue
        if not in_table:
            line_offset += len(line)
            continue
        if _is_separator_row(line_no_nl):
            line_offset += len(line)
            continue
        if not line_no_nl.startswith("|"):
            # Blank or non-table line: closes the table only when we hit a
            # second consecutive non-table line. Single-line gaps (markdown
            # quirks) keep us inside.
            if not line_no_nl.strip():
                # blank line → closes
                in_table = False
            line_offset += len(line)
            continue
        cells = _split_tabular_data_row(line_no_nl)
        if cells is None:
            line_offset += len(line)
            continue
        # Try to find the ordinal among the first 3 non-empty cells.
        ordinal: int | None = None
        for c in cells[:4]:
            if not c:
                continue
            mm = _TABULAR_AGENDA_ORDINAL_RE.match(c)
            if mm:
                try:
                    ordinal = int(mm.group(1))
                except ValueError:
                    ordinal = None
                break
            # First non-empty non-numeric cell — not an item row, abort.
            ordinal = None
            break
        if ordinal is None or not (1 <= ordinal <= 200):
            line_offset += len(line)
            continue
        # Collect role / outcome / refs / title from remaining cells
        info: dict[str, str] = {
            "title": "",
            "role_text": "",
            "outcome_text": "",
            "ref_text": "",
        }
        for c in cells:
            if not c:
                continue
            if c == cells[0] and _TABULAR_AGENDA_ORDINAL_RE.match(c):
                continue
            if not info["ref_text"] and _TABULAR_AGENDA_PLX_RE.search(c):
                info["ref_text"] = c
                continue
            if not info["role_text"] and _TABULAR_ROLE_LEAD_RE.match(c):
                info["role_text"] = c
                continue
            if not info["outcome_text"] and _TABULAR_OUTCOME_LEAD_RE.match(c):
                info["outcome_text"] = c
                continue
            # Otherwise candidate title — keep the longest.
            if len(c) > len(info["title"]):
                info["title"] = c
        if not info["title"] and not info["ref_text"]:
            line_offset += len(line)
            continue
        pending_row_offset = line_offset
        items.append((ordinal, pending_row_offset, info))
        line_offset += len(line)
    if not items:
        return []
    out: list[dict[str, Any]] = []
    for i, (ordinal, local_off, info) in enumerate(items):
        next_off = items[i + 1][1] if i + 1 < len(items) else len(block)
        refs = parse_primary_references(info.get("ref_text") or info.get("title") or "")
        refs = [r for r in refs if 1990 <= int(r.get("year") or 0) <= 2100]
        title_offset_global = block_start_global + local_off
        for r in refs:
            r["char_offsets"] = [title_offset_global, title_offset_global]
        title = info["title"] or info["ref_text"] or f"item {ordinal}"
        title = _strip_title_trailing_dot(title)
        outcome_text = info.get("outcome_text") or None
        if outcome_text and len(outcome_text) > 600:
            outcome_text = outcome_text[:599] + "…"
        role_text = info.get("role_text") or ""
        committee_role = _detect_committee_role(role_text) or _detect_committee_role(
            title
        )
        output_type = (
            _detect_output_type(role_text)
            or _detect_output_type(title)
            or _detect_output_type(outcome_text or "")
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


# -- Roster parsers (v0.2.0) -----------------------------------------------


# Three observed roster formats — see docs/architecture.md § committee_synthesis
# § Roster format detection (v0.2):
#
#   1. **Tabular** (2024+): a markdown table whose first column carries
#      member names and a sibling column carries `Prezent fizic` / `Prezent
#      online` / `Absent` annotations. Often two parallel name/status pairs
#      per row (left and right halves). Header row contains `Numele și
#      prenumele` and `Prezența`.
#   2. **Narrative** (2018-): a single paragraph beginning `au fost
#      prezenți: NAME, NAME ...` followed (optionally) by `Domnii deputați
#      ... au fost prezenți on-line` / `Au absentat ... NAMES`. Names are
#      comma-separated, last item joined with `și`.
#   3. **Per-day** (2008-, niche): numbered list `1. NAME, Grupul
#      parlamentar al X[, ROLE].` after a `- au fost prezenți:` trigger.
#      Restart sequences accepted; ordinal filter 1..200 (same as agenda).
#
# Format detection is conservative — when no shape is recognised, the
# parser returns []. Empty rosters are honest (the schema supports them).

_ROSTER_TABULAR_HEADER_RE = re.compile(
    r"\bNumele\s+(?:[șş][ii])\s+prenumele\b",
    re.IGNORECASE,
)
# Status fingerprints accepted in tabular cells. The 2022/2024+ corpus uses
# `Prezent[ă] fizic`, `Prezent[ă] online`, `Prezent[ă] la sediul CD` (synonym
# for physical), `Absent[ă]`, and `Înlocuitor[...]` markers. Adding the
# `la sediul` synonym is what unlocks the 2022 hybrid-form rosters.
_ROSTER_TABULAR_STATUS_RE = re.compile(
    r"\b(?:Prezen[țţt][ăa]?\s+(?:fizic[ăa]?|online|la\s+sediul)"
    r"|Absen[țţt][ăa]?(?:\s+motivat[ăa]?)?"
    r"|[ÎIi]nlocuitor(?:\s+online|\s+fizic)?)\b",
    re.IGNORECASE,
)
_ROSTER_NARRATIVE_TRIGGER_RE = re.compile(
    r"\b(?:au\s+fost\s+prezen[țţt]i|au\s+fost\s+prezente"
    r"|i?-?au\s+înregistrat\s+prezen[țţt]a"
    r"|au\s+fost\s+prezen[țţt]i\s+urm[ăa]torii\s+deputa[țţt]i"
    r"|Membrii\s+prezen[țţt]i\s+la\s+lucr[ăa]ri)\b",
    re.IGNORECASE,
)
# 2008-era per-day form is gated by a numbered list of names with a Grupul
# parlamentar tag attached — the latter is the discriminator versus the
# narrative form (which uses inline comma-separated names with no group).
_ROSTER_PER_DAY_PROBE_RE = re.compile(
    r"^\s*\d+\.\s+[^\n,]+,\s*Grupul\s+parlamentar\s+al\b",
    re.MULTILINE | re.IGNORECASE,
)


def _detect_roster_format(block: str) -> str | None:
    """Return one of `tabular` / `narrative` / `per_day`, or None.

    Detection is conservative: tabular wins when the table-shape fingerprint
    is present (header + status cell); per_day wins when numbered lines
    with `Grupul parlamentar` are present; narrative is the fallback when
    a `au fost prezenți` trigger is present without the table or per-day
    fingerprint.
    """
    has_pipe = "\n|" in block or block.startswith("|")
    if (
        has_pipe
        and _ROSTER_TABULAR_HEADER_RE.search(block)
        and _ROSTER_TABULAR_STATUS_RE.search(block)
    ):
        return "tabular"
    if _ROSTER_PER_DAY_PROBE_RE.search(block):
        return "per_day"
    if _ROSTER_NARRATIVE_TRIGGER_RE.search(block):
        return "narrative"
    return None


# Status-to-mode mapping for tabular cells. Cell text is normalised
# (whitespace collapsed) before lookup; Romanian feminine forms map to the
# same enum as masculine. `Prezent[ă] la sediul` is the 2022-corpus synonym
# for `physical` (literally "present at HQ").
_TABULAR_STATUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^[ÎIi]nlocuit", re.IGNORECASE), "substituted"),
    (re.compile(r"^Prezen[țţt][ăa]?\s+fizic", re.IGNORECASE), "physical"),
    (re.compile(r"^Prezen[țţt][ăa]?\s+la\s+sediul", re.IGNORECASE), "physical"),
    (re.compile(r"^Prezen[țţt][ăa]?\s+online", re.IGNORECASE), "online"),
    (re.compile(r"^Absen[țţt][ăa]?", re.IGNORECASE), "absent"),
]
# Tabular-cell name-cleaner: strip trailing `– președinte` / `– vicepreședinte`
# / `– secretar` / `– membru` and capture the role. The 2022 corpus uses
# this in-cell suffix form for committee leadership annotations.
_TABULAR_NAME_ROLE_RE = re.compile(
    r"^(?P<name>.+?)\s*[–-]\s*"
    r"(?P<role>pre[șs]edinte|vicepre[șs]edinte|secretar|membru)\b",
    re.IGNORECASE,
)

# Tabular row name shape: `Surname Given-Name` with diacritics + hyphens +
# spaces. We only require the first character to be uppercase + at least
# one lowercase letter to keep table-junk lines out (e.g. `MONITORUL OFICIAL
# AL ROMÂNIEI`, `---`, `Pagina`).
_NAME_CELL_RE = re.compile(
    r"^[A-ZȘȚĂÂÎŞŢ][A-ZȘȚĂÂÎŞŢa-zșțăâîşţăáàäéèëíìïóòöúùüçÉÈÀ-ſ\-–—\.\s]+$"
)
_TABULAR_HEADER_TOKENS_RE = re.compile(
    r"\b(?:Numele|Prezen[țţt][ăa]?|Absen[țţt][ăa]?|Pagina|MONITORUL|OFICIAL"
    r"|Nr\.\s*crt\.|PL-x|Titlu|Scopul|Rezolu[țţt]ie|Ordine"
    r"|Neafiliat[ăa]?|Grupul\s+parlamentar|UDMR|PSD|PNL|USR|AUR|POT|UNESCO"
    r"|min(?:o|ó)rit[ăa]ti?|na[țţ]ionale)\b",
    re.IGNORECASE,
)
_HEADER_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _split_table_row(line: str) -> list[str]:
    """Split a markdown-table row into trimmed cells (excluding the leading/trailing pipe)."""
    if not line.startswith("|"):
        return []
    raw = line.rstrip()
    if raw.endswith("|"):
        raw = raw[:-1]
    parts = raw.split("|")
    if parts and parts[0] == "":
        parts = parts[1:]
    return [c.strip() for c in parts]


def _is_separator_row(line: str) -> bool:
    return bool(re.match(r"^\|\s*[-:|\s]+\s*$", line.strip()))


def _classify_status_cell(cell: str) -> str | None:
    if not cell:
        return None
    for pat, mode in _TABULAR_STATUS_PATTERNS:
        if pat.search(cell):
            return mode
    return None


def _looks_like_person_name(cell: str) -> bool:
    if not cell:
        return False
    if _TABULAR_HEADER_TOKENS_RE.search(cell):
        return False
    if not _NAME_CELL_RE.match(cell):
        return False
    # Real Romanian member names always have at least 2 capitalised
    # tokens (surname + given name) — single-token cells are usually
    # party-group labels (`Neafiliată`, `UDMR`) or stray header noise.
    tokens = cell.split()
    if len(tokens) < 2:
        return False
    return True


def _parse_substitute_speaker(cell: str) -> dict[str, Any] | None:
    """`Înlocuitor online: NAME` / `Înlocuit de NAME` → Speaker dict or None."""
    m = re.search(
        r"[ÎIi]nlocuit(?:or|[ăa])?(?:\s+(?:online|fizic|par[țţt]ial))?"
        r"(?:\s*[:,]\s*|\s+(?:de|prin)\s+)(?P<name>[^|\n]+)",
        cell,
        re.IGNORECASE,
    )
    if not m:
        return None
    name = _norm_name(m.group("name"))
    if not name:
        return None
    return make_speaker(raw=name, name=name)


def _split_tabular_name_and_role(cell: str) -> tuple[str, str | None]:
    """Strip `– președinte` / `– vicepreședinte` / `– secretar` / `– membru`
    suffix from a name-cell and return (clean_name, role).
    """
    m = _TABULAR_NAME_ROLE_RE.match(cell)
    if not m:
        return cell, None
    name = _norm_name(m.group("name"))
    role = m.group("role").lower().replace("ş", "ș")
    return name, role


def _parse_roster_tabular(block: str) -> list[dict[str, Any]]:
    """Parse roster from markdown-table rows.

    Each table row may carry one or two (name, status) pairs separated by
    empty cells. We scan adjacent cell pairs greedily: a `physical` /
    `online` / `absent` / `substituted` status cell pairs with the nearest
    preceding name-shaped cell on the same row. Name cells with a `– role`
    suffix are split into clean-name + role; the role populates
    `intra_committee_role`.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in block.splitlines():
        if not line.startswith("|") or _is_separator_row(line):
            continue
        cells = _split_table_row(line)
        if not cells:
            continue
        # Pair-walk: for each status cell, find its name partner among the
        # preceding cells on the same row.
        last_name_cell: tuple[int, str] | None = None
        for idx, cell in enumerate(cells):
            mode = _classify_status_cell(cell)
            if mode is None:
                if _looks_like_person_name(cell):
                    last_name_cell = (idx, cell)
                continue
            if last_name_cell is None:
                continue
            _, name_text = last_name_cell
            clean_name, role = _split_tabular_name_and_role(name_text)
            name = _norm_name(clean_name)
            if not name or name.lower() in seen:
                last_name_cell = None
                continue
            substitute = (
                _parse_substitute_speaker(cell) if mode == "substituted" else None
            )
            entry = {
                "speaker": make_speaker(raw=name, name=name),
                "mode": mode,
                "intra_committee_role": role,
                "substituted_by": substitute,
            }
            out.append(entry)
            seen.add(name.lower())
            last_name_cell = None
    return out


# Narrative roster pattern set. The trigger captures the segment that
# follows; the segment is then walked for `Domnii deputați ... on-line` /
# `... au absentat` annotations to enrich modes.
# Trigger forms observed in the corpus (all match the same `<list>` shape):
#   - `au fost prezenți următorii deputați:`
#   - `au fost prezenți: ...`
#   - `au fost prezenți N deputați[, și anume]:` (count-and-list, 2018-)
#   - `Și-au înregistrat prezența la lucrări următorii deputați:`
#   - `și-au înregistrat prezența N deputați[, și anume]:`
#   - `Membrii prezenți la lucrări:` (rare, formal)
# A flexible `[^:]{0,80}?:` tail absorbs the optional `N deputați, și anume`
# noise between the trigger and the colon — bounded so we don't accidentally
# swallow the next paragraph if a doc lacks a colon.
_NARRATIVE_PRESENT_RE = re.compile(
    r"\b(?:"
    r"au\s+fost\s+prezen[țţt]i\s+urm[ăa]torii\s+deputa[țţt]i"
    r"|au\s+fost\s+prezen[țţt]i"
    r"|[șşs]i-au\s+înregistrat\s+prezen[țţt]a\s+(?:la\s+lucr[ăa]ri\s+)?(?:urm[ăa]torii\s+deputa[țţt]i)?"
    r"|Membrii\s+prezen[țţt]i\s+la\s+lucr[ăa]ri"
    r")"
    r"(?:[^:.]{0,120}?)?\s*[:,]\s*(?P<list>[^.]+)",
    re.IGNORECASE,
)
_NARRATIVE_ONLINE_RE = re.compile(
    r"(?:Domnii\s+deputa[țţt]i|Domnul\s+deputat|Doamnele\s+deputat|Doamna\s+deputat)\s+"
    r"(?P<list>[^.]+?)\s+au?\s+fost\s+prezen[țţt]i\s+(?:on-?line|online)",
    re.IGNORECASE,
)
_NARRATIVE_ABSENT_RE = re.compile(
    r"(?:Au\s+absentat\s+motivat|au\s+absentat\s+motivat|au\s+absentat)\s*[:,]?\s*"
    r"(?P<list>[^.]+)",
    re.IGNORECASE,
)
# Substitution side-comment: `Cătălina Ciofu – înlocuită de domnul deputat NAME` —
# captures (subject, substitute) pairs. Subject must look like a proper
# personal name (≥2 capitalised tokens) so we don't catch trailing
# `parlamentar al PNL)` fragments from earlier mid-sentence party-group
# parentheticals.
_NARRATIVE_SUBSTITUTE_RE = re.compile(
    r"(?P<subject>[A-ZȘȚĂÂÎŞŢ][\w\-]+(?:\s+[A-ZȘȚĂÂÎŞŢ][\w\-]+)+)\s*[–-]\s*"
    r"[ÎIi]nlocuit(?:[ăa])?(?:\s+par[țţt]ial)?\s+de\s+"
    r"(?:domnul|doamna)?\s*deputat[ăa]?\s+"
    r"(?P<sub>[A-ZȘȚĂÂÎŞŢ][\w\-]+(?:\s+[A-ZȘȚĂÂÎŞŢ][\w\-]+)+)",
    re.IGNORECASE,
)
_NAMES_SPLIT_RE = re.compile(r",\s*|\s+[șşs][ii]\s+", re.IGNORECASE)
# Strip per-name suffix tags that the narrative shape sometimes appends:
# `– președinte`, `– vicepreședinte`, `– secretar`, `(online)`, etc.
_NAME_SUFFIX_STRIP_RE = re.compile(
    r"\s*[–-]\s*(?:pre[șs]edinte|vicepre[șs]edinte|secretar|membru|membri)\b[^,\n]*"
    r"|\s*\([^)]*\)",
    re.IGNORECASE,
)
_NAME_ROLE_RE = re.compile(
    r"\s*[–-]\s*(?P<role>pre[șs]edinte|vicepre[șs]edinte|secretar|membru)\b",
    re.IGNORECASE,
)


def _looks_like_narrative_name(token: str) -> bool:
    t = token.strip()
    if not t:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    if len(t) < 3 or len(t) > 100:
        return False
    # First non-whitespace character must be uppercase or a Unicode
    # capital with diacritic; this filters trailing prepositional fragments.
    return t[0].isupper()


def _split_narrative_names(segment: str) -> list[tuple[str, str | None]]:
    """Split a `A, B, C și D – president` segment into [(name, role|None), ...]."""
    out: list[tuple[str, str | None]] = []
    for chunk in _NAMES_SPLIT_RE.split(segment):
        c = chunk.strip()
        if not c:
            continue
        # Capture role suffix before stripping
        role: str | None = None
        m = _NAME_ROLE_RE.search(c)
        if m:
            r = m.group("role").lower().replace("ş", "ș")
            role = r
        c = _NAME_SUFFIX_STRIP_RE.sub("", c).strip(" ,.;–-")
        if not _looks_like_narrative_name(c):
            continue
        out.append((c, role))
    return out


def _parse_roster_narrative(block: str) -> list[dict[str, Any]]:
    """Parse narrative roster from prose.

    Two complementary captures: present-list (`au fost prezenți: A, B, C`)
    and absent-list (`au absentat motivat: D, E`). Within the present-list,
    a follow-on `Domnii deputați ... on-line` clause flips matching names
    from `physical` to `online`. Substitution side-comments produce
    `substituted` entries with a `substituted_by` Speaker.
    """
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _add(
        name: str, mode: str, role: str | None, substitute: dict[str, Any] | None
    ) -> None:
        if name in by_name:
            entry = by_name[name]
            if mode == "substituted" or entry["mode"] == "physical":
                entry["mode"] = mode
            if substitute is not None:
                entry["substituted_by"] = substitute
            if role and entry["intra_committee_role"] is None:
                entry["intra_committee_role"] = role
            return
        order.append(name)
        by_name[name] = {
            "speaker": make_speaker(raw=name, name=name),
            "mode": mode,
            "intra_committee_role": role,
            "substituted_by": substitute,
        }

    # Present (default mode physical)
    for m in _NARRATIVE_PRESENT_RE.finditer(block):
        for name, role in _split_narrative_names(m.group("list")):
            _add(name, "physical", role, None)
    # Override with online subset
    for m in _NARRATIVE_ONLINE_RE.finditer(block):
        for name, role in _split_narrative_names(m.group("list")):
            if name in by_name:
                by_name[name]["mode"] = "online"
            else:
                _add(name, "online", role, None)
    # Substitution side-comments — flip subject to substituted, attach
    # substitute Speaker.
    for m in _NARRATIVE_SUBSTITUTE_RE.finditer(block):
        subject = _norm_name(m.group("subject"))
        sub_name = _norm_name(m.group("sub"))
        if not subject or not sub_name:
            continue
        substitute = make_speaker(raw=sub_name, name=sub_name)
        if subject in by_name:
            by_name[subject]["mode"] = "substituted"
            by_name[subject]["substituted_by"] = substitute
        else:
            _add(subject, "substituted", None, substitute)
    # Absent
    for m in _NARRATIVE_ABSENT_RE.finditer(block):
        for name, role in _split_narrative_names(m.group("list")):
            if name in by_name:
                # Don't overwrite a more-specific mode (online/substituted)
                if by_name[name]["mode"] == "physical":
                    by_name[name]["mode"] = "absent"
            else:
                _add(name, "absent", role, None)
    return [by_name[n] for n in order]


# Per-day numbered list parser. Two observed shapes:
#   2008-form:  `N. NAME, Grupul parlamentar al X[, ROLE].`
#               (group with dots: `P.N.L.`, `P.D.-L.`, `P.S.D.`)
#   2021-form:  `N. NAME [– ROLE], Grupul parlamentar al X – prezent[ă].`
# Both shapes are captured by a single regex: optional ` – ROLE` after the
# name, then `, Grupul parlamentar al X`, then an optional trailing `,
# ROLE` (2008-form). At most one role anchor fires per line; whichever
# matches first wins.
# Match the line in two phases: header (ordinal + name + optional role_pre +
# `, Grupul parlamentar al `) anchors the row, then the rest of the line
# is captured into `tail` for downstream group/role_post extraction. This
# avoids the non-greedy/lookahead pitfalls when the group string contains
# dots (`P.N.L.`).
_PER_DAY_LINE_RE = re.compile(
    r"^\s*(?P<ord>\d+)\.\s+"
    r"(?P<name>[^,\n–-]+?)"
    r"(?:\s*[–-]\s*(?P<role_pre>pre[șşs]edinte|vicepre[șşs]edinte|secretar|membru))?"
    r"\s*,\s*"
    r"Grupul\s+parlamentar\s+al\s+(?P<tail>[^\n]+)$",
    re.MULTILINE | re.IGNORECASE,
)
_PER_DAY_TAIL_ROLE_RE = re.compile(
    r"\b(?P<role>pre[șşs]edinte|vicepre[șşs]edinte|secretar|membru)\b",
    re.IGNORECASE,
)
_PER_DAY_TAIL_GROUP_RE = re.compile(
    r"^(?P<group>[A-Z][A-Za-z\.\-]*(?:\s+[A-Z][A-Za-z\.\-]*)*)",
)


def _parse_roster_per_day(block: str) -> list[dict[str, Any]]:
    """Parse roster from numbered `N. NAME[, ROLE] , Grupul parlamentar al X[, ROLE]`
    lines. Tail is split into group + role separately so dot-bearing
    abbreviations (`P.N.L.`, `P.D.-L.`) parse cleanly.

    Joins with the same-block per-day trigger; restart sequences and
    multi-day re-listings collapse into the first occurrence per name.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _PER_DAY_LINE_RE.finditer(block):
        try:
            ordinal = int(m.group("ord"))
        except (TypeError, ValueError):
            continue
        if not (1 <= ordinal <= 200):
            continue
        name = _norm_name(m.group("name"))
        if not name or name in seen:
            continue
        tail = m.group("tail") or ""
        # Group: leading capital-prefixed token sequence at the start of the
        # tail. `P.N.L.`, `P.D.-L.`, `UDMR`, `minorităților naționale` all
        # match. Stops at a `,` / ` – ` / `.` that follows whitespace.
        group: str | None = None
        gm = _PER_DAY_TAIL_GROUP_RE.match(tail.strip())
        if gm:
            group = gm.group("group").rstrip(" .,")
        # Role: take the FIRST role keyword anywhere in the tail OR the
        # role_pre captured before the group (whichever populates first).
        role_raw: str | None = None
        if m.group("role_pre"):
            role_raw = m.group("role_pre").lower().replace("ş", "ș")
        else:
            rm = _PER_DAY_TAIL_ROLE_RE.search(tail)
            if rm:
                role_raw = rm.group("role").lower().replace("ş", "ș")
        out.append(
            {
                "speaker": make_speaker(raw=name, name=name, party_group=group or None),
                "mode": "physical",
                "intra_committee_role": role_raw,
                "substituted_by": None,
            }
        )
        seen.add(name)
    return out


def _parse_roster(block: str) -> list[dict[str, Any]]:
    """Run all three sub-parsers and merge the results.

    Hybrid blocks (e.g. 2024+ docs that have a tabular roster for one day
    and a narrative roster for another, or 2008-era docs that interleave
    per-day numbered lists with narrative summaries) need all three
    sub-parsers' outputs combined. Each sub-parser is internally
    self-gated by its own trigger pattern — running on a block without
    its trigger returns [] safely.

    Merging strategy: name-keyed dedup with priority `tabular > per_day >
    narrative`. The first sub-parser that emits a name wins (richer mode
    + role information from explicit roster forms is preferred over
    inferred narrative-prose data). Empty rosters return [] honestly.
    """
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entries in (
        _parse_roster_tabular(block),
        _parse_roster_per_day(block),
        _parse_roster_narrative(block),
    ):
        for entry in entries:
            name = entry["speaker"].get("name") or entry["speaker"].get("raw") or ""
            key = name.lower()
            if not key:
                continue
            if key in by_name:
                # Enrich existing entry: pick up substituted_by / role if
                # the new entry has them and the existing doesn't.
                exist = by_name[key]
                if exist["substituted_by"] is None and entry["substituted_by"]:
                    exist["substituted_by"] = entry["substituted_by"]
                if exist["intra_committee_role"] is None and entry.get(
                    "intra_committee_role"
                ):
                    exist["intra_committee_role"] = entry["intra_committee_role"]
                continue
            order.append(key)
            by_name[key] = entry
    return [by_name[k] for k in order]


# -- Joint-with parser (v0.2.0) --------------------------------------------


# Discriminator: each comma-separated chunk that names a joint committee
# must literally start with `Comisia` / `Comisiei` / `Comisiilor` AND its
# second token must be a known committee-name connector (`pentru`,
# `juridică`, `de`, `economică`, `permanentă`, `comună`, `specială`,
# `pentru…`, etc.) — not an article like `a` or a verb. This
# disambiguates Romanian conjunction:
#   "Comisia X, Comisia Y și Comisia Z"     → 3 committees
#   "Comisia pentru X, Y și Z"              → 1 committee (subject list)
#   "Comisia X, comisia a deliberat"        → 1 committee (verb tail)
_JOINT_HEADER_RE = re.compile(
    r"\b[îi]n\s+(?:[șş]edin[țţt][ăa]\s+)?comun[ăa]?\s+cu\s+"
    r"(?P<list>Comisi(?:a|ile|ilor|ei)\b[^.\n;:]+)",
    re.IGNORECASE,
)
# Bill-review false-positive filter: `raport/aviz/sesizare/fond/studiu
# (în) comun cu Comisia X` is a joint *bill review*, not a joint *meeting*.
# Schema's `joint_with[]` lives on `CommitteeMeeting` and means joint
# meeting only — these forms must be rejected. We scan the ~30-char
# window preceding the `în [ședință] comună cu` trigger; if we see one of
# the bill-review nouns, we drop the match.
_JOINT_BILL_REVIEW_PREFIX_RE = re.compile(
    r"\b(?:raport(?:\s+comun)?|aviz(?:\s+comun)?|sesizar[eo]|sesizat[ăa]|fond"
    r"|studiu|raportor)\b",
    re.IGNORECASE,
)
# Splitting on `Comisi(a|ile|ilor|ei)` starts — NOT on commas — handles the
# 2022-era multi-clause names like `Comisia juridică, de disciplină și
# imunități a Camerei Deputaților, Comisia ...` where each committee
# carries internal commas inside its own descriptive clause.
_JOINT_COMISIA_START_RE = re.compile(
    r"\bComisi(?:a|ile|ilor|ei)\b",
    re.IGNORECASE,
)
# A real committee name's second token is one of these connectors. The
# corpus inventory: `pentru` (overwhelmingly common), `juridic[ăa]`,
# `economic[ăa]`, `de`, `permanent[ăa]`, `comun[ăa]`, `special[ăa]`,
# `parlamentar[ăa]`, plus a few proper-noun openers (`Națională`,
# `Centrală`). Stops verb-phrase imposters like `Comisia a deliberat`.
_JOINT_CONNECTOR_TOKEN_RE = re.compile(
    r"^Comisi(?:a|ile|ilor|ei)\s+"
    r"(?:pentru|juridic[ăa]|economic[ăa]|de|permanent[ăa]|comun[ăa]"
    r"|special[ăa]|parlamentar[ăa]|na[țţ]ional[ăa]|central[ăa]|anchet[ăa])\b",
    re.IGNORECASE,
)
# Per-chunk chamber tail: `a Camerei Deputaților` (genitive) /
# `din Camera Deputaților` (nominative) / `din cadrul Camerei Deputaților`
# (the older formal locative) / `a Senatului` / `din Senat[ul]` /
# `din cadrul Senatului`.
# Detected per chunk because multi-chamber clauses (Camera+Senat
# side-by-side) lose info if we collapse to one chamber at the clause
# level. The `Camer[ae]i?` class accepts both `Camera` and `Camerei`.
_JOINT_CHAMBER_RE = re.compile(
    r"\b(?:din(?:\s+cadrul)?|a)\s+(?P<chamber>(?:Camer[ae]i?\s+Deputa[țţt]ilor|Senat(?:ul|ului)?))\b",
    re.IGNORECASE,
)
# Trim trailing chamber/verb noise off the captured name.
_JOINT_NAME_TRIM_RE = re.compile(
    r"\s+(?:din(?:\s+cadrul)?|a)\s+(?:Camer[ae]i?\s+Deputa[țţt]ilor|Senat(?:ul|ului)?).*$"
    r"|\s+(?:a\s+desf[ăa][șş]urat|s-a\s+desf[ăa][șş]urat|a\s+avut\s+loc).*$"
    r"|,\s+a\s+desf[ăa][șş]urat.*$"
    r"|,\s+a\s+deliberat.*$",
    re.IGNORECASE,
)


def _parse_joint_with(block: str) -> list[dict[str, Any]]:
    """Detect `în comun cu Comisia X[, Comisia Y[, Comisia Z]]` clauses.

    Each chunk must literally start with `Comisia` / `Comisiei` /
    `Comisiilor` to count — this is the conjunction-disambiguation
    contract. `chamber` is best-effort: `din Camera Deputaților` /
    `din Senat[ul]` / null when the clause carries no explicit chamber.
    Subject-list ambiguity (`Comisia pentru X, Y și Z` — one committee
    with a 3-item subject) is resolved by the literal-chunk-prefix check;
    the trailing chunks aren't `Comisia`-prefixed so they're discarded.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for m in _JOINT_HEADER_RE.finditer(block):
        # Reject bill-review false-positives: when the trigger is preceded
        # within 30 chars by `raport/aviz/sesizare/fond/studiu`, this is a
        # joint *bill review* annotation in an agenda item — not a joint
        # *meeting*. The schema slot is meeting-level, so drop it.
        prefix_window = block[max(0, m.start() - 30) : m.start()]
        if _JOINT_BILL_REVIEW_PREFIX_RE.search(prefix_window):
            continue
        list_text = m.group("list").strip()
        # Find every `Comisia/Comisiei/Comisiilor` start in the list. Each
        # one opens a new chunk; chunks span [start_i, start_{i+1}) and
        # may contain internal commas (`Comisia juridică, de disciplină și
        # imunități a Camerei Deputaților`) without splitting them.
        starts = [sm.start() for sm in _JOINT_COMISIA_START_RE.finditer(list_text)]
        if not starts:
            continue
        # Trailing-chamber inheritance: when a list has a single chamber
        # tail at the end (`Comisia X, Comisia Y și Comisia Z din Senat`),
        # all chunks inherit that chamber. We compute the inherited chamber
        # from the last chunk and apply it to chunks that don't carry an
        # explicit chamber of their own.
        chunks_data: list[tuple[str, str | None]] = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(list_text)
            chunk = list_text[start:end].strip()
            # Strip leading/trailing punctuation glue ("și ", ", ").
            chunk = re.sub(r"^[,;\s]+|[,;\s]+$", "", chunk)
            chunk = re.sub(r"\s+[șşs][ii]\s*$", "", chunk, flags=re.IGNORECASE)
            if not chunk:
                continue
            # Connector check — verb-phrase imposters like `Comisia a
            # deliberat` are filtered here (`a` is not in the connector
            # vocabulary).
            if not _JOINT_CONNECTOR_TOKEN_RE.match(chunk):
                continue
            cm = _JOINT_CHAMBER_RE.search(chunk)
            chamber: str | None = None
            if cm:
                ch = cm.group("chamber").lower()
                if "camer" in ch:
                    chamber = "camera"
                elif "senat" in ch:
                    chamber = "senat"
            chunks_data.append((chunk, chamber))
        # Trailing-chamber inheritance: if the LAST chunk has a chamber and
        # at least one earlier chunk has no chamber, propagate the last
        # chunk's chamber back to chunks that lack one. This handles
        # `Comisia X, Comisia Y și Comisia Z din Senat` (all senat) without
        # over-applying when explicit per-chunk chambers exist
        # (`Comisia X din Camera Deputaților, Comisia Y din Senat`).
        if chunks_data:
            last_chamber = chunks_data[-1][1]
            explicit_chambers = {ch for _, ch in chunks_data if ch is not None}
            if last_chamber is not None and len(explicit_chambers) == 1:
                chunks_data = [
                    (c, ch if ch is not None else last_chamber) for c, ch in chunks_data
                ]
        for chunk, chamber in chunks_data:
            # Trim trailing chamber + verb noise off the captured name.
            name = _JOINT_NAME_TRIM_RE.sub("", chunk).strip(" .,;")
            if not name:
                continue
            name = _norm_name(name)
            key = (name.lower(), chamber)
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "chamber": chamber})
    return out


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
    roster = _parse_roster(block)
    joint_with = _parse_joint_with(block)
    meeting_span = ctx.make_source_span((block_start, block_end))
    meeting = {
        "dates": dates,
        "time_windows": time_windows,
        "format": fmt,
        "purpose": purpose,
        "joint_with": joint_with,
        "roster": roster,
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
