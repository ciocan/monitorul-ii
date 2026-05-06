"""Interpellation block extractor for plenary_stenogram.

Boundary detection by chair's canonical transition phrase. Block ends at EOF.
Per-interpellation parser extracts questioner / addressed_to / topic /
interpellation_number / response_deferred / question_text.

`question_text` (v0.2.0): the policy-substance body of the interpellation,
extracted from the questioner's turn by stripping opening pleasantries,
topic-naming lead-ins, trailing signatures/closures, and capping at
section-break markers (e.g., when the same speaker continues into a
political declaration in the same turn). Returns null when the recovered
body is too short to be meaningful.

`addressed_to` (v0.2.3): rewritten to capture the full addressee phrase
instead of single-letter noise. The previous v0.2.2 regex used non-greedy
quantifiers without a trailing anchor, so `(?P<role>[^.,\\n]+?)` collapsed
to 1 character and emitted values like `'D'` / `'m'` / `'C'` for ~76% of
non-null hits (1383/1953 production sweep). v0.2.3 uses three patterns
in priority order: (1) `Ministerul[ui]? X` near an address verb, (2)
`ministrul[ui|l]? X` role-form (lowercase initial) which is transformed
to `Ministerul X` for downstream registry normalization, (3) bare
`(doamnei|domnului|doamna|domnul) NAME` person fallback. Search bounded
to the first ~800 chars of the questioner's turn body so incidental
ministry mentions deeper in the question content don't fire as the
addressee.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    lines_for_range,
    make_boilerplate_claim,
    make_record_claim,
)
from monitorul_ii.extraction.speakers import (
    extract_delivery_mode,
    parse_honorific_speaker,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext


INTERPELLATIONS_VERSION = "0.2.3"
INTERPELLATIONS_LABEL = f"regex@plenary_interpellations@{INTERPELLATIONS_VERSION}"


# -- Transition phrases (block boundary detector) ---------------------------
#
# Survey of 5551 production MDs (2026-05) showed only 1 doc matched the
# v0.1 pattern set. v0.2 widens coverage with the chair-declares-the-block
# phrases observed across 2009-2025: "Declar deschisă sesiunea/ședința de
# întrebări/interpelări" (most common modern form), "Începem sesiunea de
# întrebări și interpelări" (2009-2017), "Urmează prezentarea/sesiunea de
# interpelări" (2009-era), "Răspunsuri la interpelări." (terse).
#
# Patterns are intentionally chair-declarative — they fire on the chair
# OPENING the session, not on incidental mentions of interpelări in
# regular speech. Anchored line-or-substring as appropriate.


_TRANSITION_PHRASES: list[re.Pattern[str]] = [
    # "trecem la primirea răspunsurilor la interpelări"
    re.compile(
        r"trecem\s+la\s+primirea\s+r[ăa]spunsurilor\s+la\s+interpel[ăa]ri",
        re.IGNORECASE,
    ),
    # "Începem sesiunea/ora de întrebări/interpelări..." (chair opens)
    re.compile(
        r"începem\s+(?:sesiunea|ora)\s+(?:de\s+)?(?:întreb[ăa]r(?:i|ilor)?\s+)?"
        r"(?:[șs]i\s+)?interpel[ăa]r(?:i|ilor)?",
        re.IGNORECASE,
    ),
    # "(vom) intra(m) în ora întrebărilor/interpelărilor"
    re.compile(
        r"(?:vom\s+)?intr[ăa]m?\s+în\s+ora\s+(?:întreb[ăa]rilor|interpel[ăa]rilor)",
        re.IGNORECASE,
    ),
    # "trecem la prezentarea interpelărilor noi"
    re.compile(r"trecem\s+la\s+prezentarea\s+interpel[ăa]rilor\s+noi", re.IGNORECASE),
    # "trecem la (ultimul) punct (de pe / din) ordinea de zi: întrebări, interpelări"
    # (chair passes to the interpellations agenda point — 37 additional docs)
    re.compile(
        r"trecem\s+la\s+(?:ultimul\s+)?punct\s+(?:de\s+pe\s+|din\s+)?ordinea\s+de\s+zi"
        r"[\s,:.;]+(?:[^\n]{0,40})(?:întreb[ăa]ri|interpel[ăa]ri)",
        re.IGNORECASE,
    ),
    # "Declar deschisă sesiunea/ședința [consacrată/de] ... interpelări/întrebări"
    # (chair's canonical opener; 136 hits across the corpus)
    re.compile(
        r"declar\s+deschis[ăa]\s+(?:[șs]edin[țt]a|sesiunea)\s+"
        r"(?:consacrat[ăa]\s+|de\s+|pentru\s+)?[^\n]{0,80}"
        r"(?:interpel[ăa]r|întreb[ăa]r)",
        re.IGNORECASE,
    ),
    # "Urmează prezentarea/sesiunea ... de interpelări/întrebări"
    re.compile(
        r"urmeaz[ăa]\s+(?:prezentarea|sesiunea)\s+"
        r"(?:pe\s+scurt\s+)?(?:de\s+|a\s+)?[^\n]{0,80}"
        r"interpel[ăa]r",
        re.IGNORECASE,
    ),
    # "Deschidem ședința consacrată răspunsurilor orale ..."
    re.compile(
        r"deschidem\s+[șs]edin[țt]a\s+consacrat[ăa]\s+r[ăa]spunsurilor",
        re.IGNORECASE,
    ),
    # "Răspunsuri la interpelări." — chair-anchored line. Allows trailing
    # courtesy phrases ("ale domnilor deputați...") but not table cells
    # (excluded by negative-lookbehind for `|`, common in SUMAR rows).
    re.compile(
        r"(?<![|])^\s*R[ăa]spunsuri\s+la\s+interpel[ăa]ri[ie]?[^\n|]{0,160}\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "## **Întrebări orale ...**" header
    re.compile(r"^##?\s+\*\*?\s*Întreb[ăa]ri\s+orale", re.IGNORECASE | re.MULTILINE),
]


def find_interpellation_block(body: str) -> tuple[int, int] | None:
    """Return (block_start, block_end=len(body)) or None if no transition found."""
    earliest: int | None = None
    for pat in _TRANSITION_PHRASES:
        m = pat.search(body)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None:
        return None
    return (earliest, len(body))


# -- Per-interpellation parser ----------------------------------------------


# Interpellation header — `## **<inner>:**` shape; **same as activities,
# but anchored to require the inner text to terminate with a colon. v0.1
# accepted any `## **anything**` line, which falsely matched the trailing
# `## **EDITOR: GUVERNUL ROMÂNIEI**` MO footer (colon embedded mid-string)
# as an interpellation header. True speaker headers always end the bold
# with `:` — `Doamna NAME:`, `Domnul NAME (din sală):`, `Mr X – role:`. The
# tightened regex requires `:**` at end, with optional whitespace.
_INTERP_HEADER_RE = re.compile(
    r"^##\s+\*\*\s*(?P<inner>[^*\n]+?)\s*:\s*\*\*\s*$",
    re.MULTILINE,
)


# Responder header — minister / secretar de stat replying to a prior
# interpellation. The bold `**` closes around just the NAME, then an
# italic `_– role_` marker follows. Three concrete forms across the
# corpus:
#   (1) Single-line, complete role:
#       `## **Doamna Lidia Barac** _– secretar de stat în Ministerul Justiției_ **:**`
#   (2) Multi-line, role split across paragraph break, continuation has `## ` prefix:
#       `## **Domnul Marius Lazăr** _– secretar de stat_`
#       ``                                                            (blank)
#       `## _în Ministerul Muncii, Familiei și Protecției Sociale_ **:**`
#   (3) Multi-line, continuation has NO `## ` prefix:
#       `## **Domnul Cristian Anton Irimie** _– secretar de stat_`
#       ``
#       `_în Ministerul Sănătății_ **:**`
#
# We match the first line (mandatory `## **NAME** _– role_`); the role is
# considered "settled" if the same line ends with `**:**`, otherwise we
# scan the next few lines for the closing `**:**` to capture role
# continuation.
_RESPONDER_FIRST_LINE_RE = re.compile(
    r"^##\s+\*\*\s*(?P<name>[^*\n]+?)\s*\*\*"
    r"\s*_\s*[-–]\s*(?P<role>[^_\n]+?)\s*_"
    r"(?P<tail>\s*\*\*:\*\*\s*)?$",
    re.MULTILINE,
)
_RESPONDER_ROLE_CONT_RE = re.compile(
    r"^(?:##\s+)?_\s*(?P<role_cont>[^_\n]+?)\s*_\s*\*\*:\*\*\s*$",
    re.MULTILINE,
)
# Role-of-responder enum tokens — used to filter out matches that look
# like responder shape but aren't actually ministers/state secretaries
# (e.g., chair-narrative italic decoration in old docs).
_RESPONDER_ROLE_TOKENS = re.compile(
    r"\b(?:secretar(?:ul|i)?\s+de\s+stat|ministru(?:l|lui)?|"
    r"viceprim-?ministru|prim-?ministru|consilier(?:ul)?|[șs]eful|"
    r"director(?:ul|i|ii)?|pre[șs]edinte(?:le)?\s+(?:Cur[țt]ii|Consiliului)|"
    r"avocatul\s+poporului|guvernator(?:ul)?)\b",
    re.IGNORECASE,
)


# Genre detection — `interpelare` and `întrebare` heuristics
_INTERPELARE_HINTS = re.compile(r"interpel[ăa]r", re.IGNORECASE)
_INTREBARE_HINTS = re.compile(r"întreb[ăa]r", re.IGNORECASE)


# -- addressed_to detection (v0.2.3) ----------------------------------------
#
# Three patterns tried in priority order against the head of the
# questioner's turn body. The pre-v0.2.3 single regex used non-greedy
# `(?P<role>[^.,\n]+?)` captures with no trailing anchor, so they
# collapsed to a single character — emitting `'D'` / `'m'` / `'C'` for
# 76% of non-null hits. v0.2.3 uses greedy captures bounded by sentence
# punctuation (`,.;\n`), and prefers ministry-bearing forms over bare
# person captures so `addressed_to` is normalisable downstream by the
# ministries registry.
#
# Search is bounded to the first 800 chars of the post-header turn body
# (`_ADDRESSED_TO_HEAD_LIMIT`). Without this bound, incidental mentions
# of `Ministerul X` deep in the question content (sentence prose like
# `Ministerul Educației a anunțat...`) get captured as the addressee.

_ADDRESSED_TO_HEAD_LIMIT = 800

# Romanian alphabet char classes — accept modern (ă, â, î, ș, ț), cedilla
# (ş, ţ), and mojibake artefacts (ã, þ, ª) seen in pre-2010 OCR'd PDFs.
# The downstream registry's diacritic-stripping tier folds all of these
# into the same key, so capturing them is enough; we don't need to
# normalise here.
_ROM_LOWER = r"a-zșțăîâşţãþ"
_ROM_UPPER = r"A-ZȘȚĂÎÂŞŢÃ"

# Address-verb anchor — required before the addressee. Without an
# anchor, ANY `ministru` / `Ministerul` mention in the head would fire,
# including incidental references in the questioner's framing prose.
# The lazy `[^\n]{0,200}?` allows up to ~200 chars of text between the
# verb and the addressee phrase (e.g. `adresată domnului NAME, ministrul X`
# has ~30 chars of `domnului NAME, ` between verb and `ministrul`).
# Verb stems also accept `ã` for mojibake-form `ă`.
_ADDRESS_VERBS = (
    r"adresat[ăaã]?|adresez(?:[ăaã])?|adreseaz[ăã]|"
    r"c[ăa]tre|interpel[ăaã]rii|întreb[ăaã]rii"
)
_VERB_CONTEXT = rf"(?i:\b(?:{_ADDRESS_VERBS})\b)[^\n]{{0,200}}?"


# 1. `Ministerul[ui]? X` — institutional name (preferred). Both genitive
#    (`Ministerului`) and nominative (`Ministerul`) accepted. Greedy
#    capture up to the next sentence-ish boundary (`,.;\n`). The
#    captured X is the ministry's distinguishing tail (e.g. `Sănătății`).
_ADDRESSED_TO_MINISTRY_RE = re.compile(
    rf"{_VERB_CONTEXT}\bMinisterul(?:ui)?\s+(?P<addressee>[{_ROM_UPPER}][^.,;\n]*)"
)

# 2. `(vice)?prim-ministru[l|lui]?` — Prime Minister / Deputy PM role.
#    The corpus carries `prim-ministru al României` / `prim-ministru al
#    Guvernului României` / `viceprim-ministru` forms; emit the canonical
#    `Prim-ministrul` so the ministries registry's `prime_minister` id
#    matches via case/diacritic tier. Match consumes any trailing role
#    qualifiers (`al României`, `al Guvernului României`) but doesn't
#    capture them — addressee is fixed.
_ADDRESSED_TO_PM_RE = re.compile(
    rf"{_VERB_CONTEXT}\b(?:[Vv]ice-?)?[Pp]rim-?ministru(?:l|lui)?\b"
)

# 3. `ministrul[ui|l]? X` — role form (lowercase initial). Optional
#    `al/a/ale` connector consumed before the addressee (`ministru al
#    culturii` → addressee = `culturii`). The captured X is in genitive
#    (`educației`, `sănătății`, `apelor și pădurilor`) — exactly the
#    distinguishing tail of `Ministerul X`. Downstream `_extract_addressed_to`
#    transforms `ministrul X` → `Ministerul X` for registry normalisation
#    (the registry has `Ministerul Educației` aliased, not `ministrul
#    educației`).
#
#    The leading `(?<![-{_ROM_LOWER}{_ROM_UPPER}])` lookbehind rejects
#    `prim-ministru` / `viceprim-ministru` / `Primul-ministru` matches
#    that would otherwise fire here and produce nonsensical
#    `Ministerul Al României` captures (where the `al` connector is
#    consumed and the rest of the prepositional phrase is captured as
#    the ministry name). The PM form is handled by pattern 2 above.
_ADDRESSED_TO_MINISTRU_RE = re.compile(
    rf"{_VERB_CONTEXT}(?<![-{_ROM_LOWER}{_ROM_UPPER}])ministru(?:lui|l)?\s+"
    r"(?:(?:al|a|ale)\s+)?"
    rf"(?P<addressee>(?:de\s+)?[{_ROM_LOWER}][^.,;\n]*)"
)

# 4. Person form: `(doamnei|domnului|doamna|domnul) [ministru[lui|l]] NAME`.
#    Captures only the person name (no useful ministry hint when the
#    role isn't named explicitly). Returns the bare name as a fallback
#    so consumers can still see the addressee was a specific person, even
#    though the registry won't normalise it.
_ADDRESSED_TO_PERSON_RE = re.compile(
    rf"{_VERB_CONTEXT}(?:doamnei|domnului|doamna|domnul)\s+"
    r"(?:ministru(?:lui|l)?\s+)?"
    rf"(?P<addressee>[{_ROM_UPPER}][^.,;\n]*)"
)


def _extract_addressed_to(turn_body: str) -> str | None:
    """Return the addressed party for an interpellation, or None.

    Searches the head of `turn_body` (first ~800 chars) for an addressee
    using four patterns in priority order:

      1. `Ministerul[ui]? X` — clean ministry name.
      2. `(vice)?prim-ministru[l|lui]?` — Prime Minister / Deputy PM
         role, returned as `Prim-ministrul` (the registry's
         `prime_minister` canonical alias).
      3. `ministrul[ui|l]? X` — role form, transformed to `Ministerul X`
         so the value normalises against the ministries registry (which
         enumerates institutional aliases, not role-form aliases).
      4. `(doamnei|domnului|doamna|domnul) NAME` — bare person fallback.

    Each capture is greedy up to `,.;\\n` so single-letter misfires
    from non-greedy quantifiers (the v0.2.2 bug) cannot occur. Returns
    None when no address-verb anchor + addressee combination matches.
    """
    head = turn_body[:_ADDRESSED_TO_HEAD_LIMIT]

    m = _ADDRESSED_TO_MINISTRY_RE.search(head)
    if m:
        x = m.group("addressee").strip().rstrip(".,;: ")
        if x:
            return f"Ministerul {x}"

    if _ADDRESSED_TO_PM_RE.search(head):
        return "Prim-ministrul"

    m = _ADDRESSED_TO_MINISTRU_RE.search(head)
    if m:
        x = m.group("addressee").strip().rstrip(".,;: ")
        if x:
            # Capitalise first letter so the produced string looks like
            # the canonical `Ministerul X` form. The registry's case
            # tier handles minor case differences anyway, but a clean
            # title-case start helps ad-hoc consumers reading the field.
            x = x[0].upper() + x[1:]
            return f"Ministerul {x}"

    m = _ADDRESSED_TO_PERSON_RE.search(head)
    if m:
        x = m.group("addressee").strip().rstrip(".,;: ")
        if x:
            return x

    return None


# Interpellation number — same regex as qr's
_INTERPELLATION_NUMBER_RE = re.compile(
    r"\bNr\.\s*(?P<num>\d+(?:\.\d+)*[A-Za-z]?)", re.IGNORECASE
)

# Response_deferred markers
_RESPONSE_DEFERRED_RE = re.compile(
    r"\(\s*în\s+scris\s*\)|răspuns(?:ul)?\s+în\s+scris", re.IGNORECASE
)


# -- question_text extraction (v0.2.0) --------------------------------------


# Lines that are pure pleasantries or salutations — drop from the start of
# question_text. Anchored line-only (full match) so substantive sentences
# that *contain* these phrases are preserved.
_PLEASANTRY_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:v[ăa]\s+)?mul[țt]umesc(?:[\s,!.][^\n]*)?|"
    r"bun[ăa]\s+(?:diminea[țt]a|seara|ziua)(?:[\s,!.][^\n]*)?|"
    r"doamn[ăa]\s+pre[șs]edint[eă][^\n]*|"
    r"domnule\s+pre[șs]edint[eă][^\n]*|"
    r"stima[țt]i\s+colegi[^\n]*|"
    r"stimate\s+colege[^\n]*|"
    r"stima[țt]i\s+(?:domni\s+(?:senatori|deputa[țt]i)|senatori|deputa[țt]i)[^\n]*|"
    r"dragi\s+români[^\n]*|"
    r"doamnelor\s+[șs]i\s+domnilor[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)

# Topic-only skip lines: more aggressive than `_PLEASANTRY_LINE_RE` —
# additionally skips ministerial-salutation lines like `Domnule ministru,`,
# `Stimate domnule ministru,`, `Doamna ministru,` (these belong in the
# question_text body where they signal the addressed party, but they're
# never the topic). Used exclusively by `_extract_topic` fallback path.
_TOPIC_SKIP_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:stimat[eă]\s+)?(?:doamn[ăa]|domnule)\s+"
    r"(?:pre[șs]edint[eă]|ministru|secretar(?:[ăa])?\s+de\s+stat|"
    r"viceprim-?ministru|prim-?ministru|deputat|senator)\b[^\n]*|"
    r"stima[țt]i?\s+colegi[^\n]*|"
    r"stimate\s+colege[^\n]*|"
    r"doamnelor\s+[șs]i\s+domnilor[^\n]*|"
    r"dragi\s+români[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)

# Topic-naming preamble lines — the formal "I shall now read out..." opening
# that names the addressed minister + subject. Also covers the older
# "Interpelarea este adresată..." / "Interpelarea se adresează..." form.
# Drop these from the start because the topic + addressee are captured
# separately as their own fields.
_TOPIC_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"voi\s+(?:da\s+curs|prezenta|citi)[^.\n]*"
    r"(?:întreb[ăa]rii|interpel[ăa]rii|întreb[ăa]ri|interpel[ăa]ri)[^\n]*|"
    r"interpelarea\s+(?:este\s+adresat[ăa]|se\s+adreseaz[ăa])[^\n]*|"
    r"întrebarea\s+(?:este\s+adresat[ăa]|se\s+adreseaz[ăa])[^\n]*|"
    r"obiectul\s+interpel[ăa]rii\s*[:.]?\s*[^\n]*|"
    r"obiectul\s+întreb[ăa]rii\s*[:.]?\s*[^\n]*|"
    r"adresez(?:[ăa])?\s+(?:aceast[ăa]\s+)?(?:întrebare|interpelare)[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)

# Closing patterns — cap question_text at the start of the earliest match.
# These signal the speaker is wrapping up (request for written response,
# courtesy close, signature). Anchored loosely so the trailing text
# ("Solicit răspuns în scris, în termen de 15 zile...") is excluded too.
_CLOSING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bSolicit\s+r[ăa]spuns\b", re.IGNORECASE),
    re.compile(r"\bA[șs]tept(?:[ăa]m)?\s+r[ăa]spuns(?:ul)?\b", re.IGNORECASE),
    re.compile(r"\bDoresc\s+un\s+r[ăa]spuns\b", re.IGNORECASE),
    re.compile(r"^\s*Cu\s+stim[ăa]\s*[,.]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Cu\s+respect\s*[,.]", re.IGNORECASE | re.MULTILINE),
]

# Section-break patterns — same speaker continues into a different topic
# (typically a political declaration after the interpellation). Cap
# question_text at the start of the earliest match.
_SECTION_BREAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\bvoi\s+(?:citi|prezenta|continua\s+cu)\s+(?:[șs]i\s+)?"
        r"declara[țt]ia\s+politic[ăa]\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdeclara[țt]ia\s+politic[ăa]\s+(?:cu\s+titlul|cu\s+tema|"
        r"pe\s+care\s+(?:vreau|doresc))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:trec(?:em)?|voi\s+trece)\s+(?:acum\s+)?la\s+"
        r"(?:a\s+doua|cea\s+de-a\s+doua|cealalt[ăa])\s+"
        r"(?:întrebare|interpelare)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bvoi\s+citi\s+[șs]i\s+declara[țt]ia\b", re.IGNORECASE),
    # `Titlul declarației [...adjectives...] [: | . | este | este: | – | -]`
    # — generalised marker. Any phrase starting with `Titlul declarației`
    # caps the question_text, regardless of what follows (`politice`,
    # `mele`, `de astăzi`, `din această săptămână`, then either `:` / `.`
    # / `este` / `–` / `-`). The `este` form is the most common (`Titlul
    # declarației politice este: „...”`).
    re.compile(
        r"\bTitlul\s+declara[țt]iei\b",
        re.IGNORECASE,
    ),
    # `Declarație politică` standalone marker — any phrasing where the
    # speaker announces "this is a political declaration". Patterns observed:
    # `Declarație politică.` / `Declarație politică:` / `Declarație politică
    # privind X` / `Declarație politică adresată X` / `Declarație politică.
    # Subiectul este X` / `Declarația politică este adresată X` / `prima
    # declarație politică în calitate de senator`. Generalises by matching
    # the phrase + a small set of follow-on connectives. Anchored at line
    # start OR after sentence-end punctuation to avoid incidental matches.
    re.compile(
        r"(?:^|(?<=[.!?]\s)|(?<=[.!?]\s\s))\s*"
        r"(?:[Aa]ceast[ăa]\s+|[Pp]rima\s+|[Oo]\s+)?"
        r"Declara[țt]i(?:e|a)\s+politic[ăa]"
        r"(?:\s*[:.]|\s+(?:privind|cu\s+titlul|cu\s+tema|"
        r"adresat[ăa]|este\s+adresat[ăa]|în\s+calitate|pe\s+care|"
        r"face\s+referire)\b)",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Body-text mention: "supun atenției o declarație politică ce face
    # referire la faptul că „...”" — speaker frames declaration mid-prose.
    re.compile(
        r"\b(?:supun(?:e|em)?\s+(?:aten[țt]iei|votului)|"
        r"prezint(?:[ăa])?|aleg(?:em)?\s+s[ăa]\s+prezint)"
        r"\s+o\s+declara[țt]ie\s+politic[ăa]\b",
        re.IGNORECASE,
    ),
    # `Senator/Deputat NAME[, ...] Declarație politică` — speaker's
    # signature line that starts the declaration block.
    re.compile(
        r"\b(?:Senator|Deputat)(?:\s+(?:AUR|PNL|PSD|USR|UDMR|REPER|SOS|S\.O\.S\.|"
        r"independent))?\s+[A-ZȘȚĂÎÂ][^.\n]{0,80}[.,]\s+Declara[țt]ie\s+politic[ăa]",
        re.IGNORECASE,
    ),
    # `Sunt NAME, senator/deputat ... declarație politică` — speaker
    # introduces themselves then announces declaration.
    re.compile(
        r"\bSunt\s+[A-ZȘȚĂÎÂ][^.\n]{0,80},\s*(?:senator|deputat)[^\n]{0,200}"
        r"declara[țt]ie\s+politic[ăa]",
        re.IGNORECASE,
    ),
]


# Pure-declaration markers — when these appear in the FIRST 600 chars of a
# turn body, the turn contains only a political declaration (no interpellation).
# Skip the entry entirely rather than emit a confused interpellation record.
# (`\b` only at the start; the trailing punctuation classes are non-word so a
# closing `\b` would never match.)
_PURE_DECLARATION_HEAD_RE = re.compile(
    # Any mention of `Titlul declarației` OR `declarație politică` /
    # `declarația politică` — if this appears in the first 600 chars of a
    # turn AND no interpellation/question marker exists anywhere in the
    # turn (checked by `_is_pure_declaration_turn`), the speaker is reading
    # a political declaration only. Permissive on the head, conservative on
    # the negative gate.
    r"(?:\bTitlul\s+declara[țt]iei\b|\bdeclara[țt]i(?:e|a|ei)\s+politic[ăa])",
    re.IGNORECASE,
)


# Boilerplate-name detector: when the inner text of a `## **inner:**`
# header is clearly NOT a person's name (uppercase-heavy, contains MO
# footer keywords, > 80 chars), reject as a header. This catches the rare
# `## **ABONAMENTE LA PUBLICAȚIILE OFICIALE ... DISTRIBUȚIE:**` form which
# happens to end with `:**` and would otherwise pass the tightened header
# regex. Real speaker names are < 80 chars and don't contain these tokens.
_BOILERPLATE_NAME_TOKENS = re.compile(
    r"\b(?:ABONAMENTE|MONITORUL\s+OFICIAL|PUBLICA[ȚT]III?LE|"
    r"EDITOR|GUVERNUL\s+ROM[ÂA]NIEI|DISTRIBU[ȚT]IE|"
    r"R\.A\.|S\.A\.|SOCIET[ĂA][ȚT]I)\b",
    re.IGNORECASE,
)


def _is_boilerplate_name(inner: str) -> bool:
    """Heuristic: True when the inner header text is MO footer boilerplate.

    Rules:
      - Length > 80 chars (real names are shorter)
      - Contains a footer keyword (ABONAMENTE, MONITORUL OFICIAL, EDITOR,
        DISTRIBUȚIE, R.A. / S.A. / GUVERNUL ROMÂNIEI, etc.)
      - All-uppercase content > 30 chars (real names use Title Case)
    """
    s = (inner or "").strip()
    if len(s) > 80 and _BOILERPLATE_NAME_TOKENS.search(s):
        return True
    if _BOILERPLATE_NAME_TOKENS.search(s):
        return True
    # All-uppercase block (no lowercase letters at all) and > 30 chars
    if len(s) > 30 and not any(c.islower() for c in s if c.isalpha()):
        return True
    return False


# -- response extraction (v0.2.2) -------------------------------------------


def _find_responder_headers(
    block_text: str,
) -> list[dict[str, Any]]:
    """Locate every responder header in the block.

    Returns a list of dicts with keys:
      - start: char offset of the header line in block_text
      - body_start: char offset where the response body begins (after the
        closing `**:**`, possibly on a continuation line)
      - name: speaker name (raw, with honorific stripped to bare last/first)
      - role: combined role string (first line + any continuation italic)

    Filters: the role must contain a recognised responder-role token
    (`secretar de stat`, `ministru`, etc.) — keeps the matcher honest
    against false positives where the italic is pure narrative decoration.
    """
    out: list[dict[str, Any]] = []
    for fm in _RESPONDER_FIRST_LINE_RE.finditer(block_text):
        name = fm.group("name").strip()
        role = fm.group("role").strip()
        tail = fm.group("tail")
        body_start = fm.end()
        if tail is None:
            # Look for a role-continuation line within the next ~400 chars
            scan_start = fm.end()
            scan_end = min(scan_start + 400, len(block_text))
            cont = _RESPONDER_ROLE_CONT_RE.search(block_text, scan_start, scan_end)
            if cont:
                role = f"{role} {cont.group('role_cont').strip()}".strip()
                body_start = cont.end()
        if not _RESPONDER_ROLE_TOKENS.search(role):
            continue
        out.append(
            {
                "start": fm.start(),
                "body_start": body_start,
                "name": name,
                "role": role,
            }
        )
    return out


def _extract_response_text(body: str) -> str | None:
    """Clean up the responder's body text. Strip leading pleasantries and
    return None if the residue is too short to be a meaningful reply.
    Reuses the same pleasantry vocabulary as `_extract_question_text`."""
    lines = body.splitlines()
    while lines:
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        if (
            _PLEASANTRY_LINE_RE.match(line)
            or line.startswith("#")
            or line.startswith("_")
        ):
            lines.pop(0)
            continue
        break
    while lines and not lines[-1].strip():
        lines.pop()
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) < _MIN_QUESTION_TEXT_CHARS:
        return None
    return cleaned


def _is_pure_declaration_turn(turn_body: str) -> bool:
    """Return True when the turn body is a political declaration only —
    i.e., contains a declaration-title marker AND no interpellation/question
    markers. Used to skip declaration-only turns within the interpellation
    block (Carmen Orban-style: speaker reads only a declaration during the
    interpellations agenda point).
    """
    head = turn_body[:600]
    if not _PURE_DECLARATION_HEAD_RE.search(head):
        return False
    # If interpellation/question phrasing appears anywhere in the turn,
    # the speaker did both — keep it (question_text capping handles the rest).
    if re.search(
        r"\b(?:interpel[ăa]r|am\s+(?:o\s+)?(?:întrebare|interpelare)|"
        r"adresez(?:[ăa])?\s+(?:aceast[ăa]\s+)?(?:întrebare|interpelare)|"
        r"obiectul\s+(?:întreb[ăa]rii|interpel[ăa]rii))\b",
        turn_body,
        re.IGNORECASE,
    ):
        return False
    return True


_MIN_QUESTION_TEXT_CHARS = 40


# -- topic detection (v0.2.2) --------------------------------------------


# Quoted-subject regex — matches Romanian „...", French/Belgian «...», and
# straight ASCII "..." quotes. Captures the inner subject (≤ 280 chars).
_QUOTED_TOPIC_RE = re.compile(
    r"(?:„|«|\")(?P<topic>[^„«\"\n]{8,280}?)(?:”|»|\")",
    re.MULTILINE,
)

# Explicit subject markers — `obiectul interpelării/întrebării: ...` /
# `cu obiectul: ...` / `obiect: ...` / `subiectul: ...` / `tema: ...`.
_OBJECT_TOPIC_RE = re.compile(
    r"\b(?:obiectul\s+(?:interpel[ăa]rii|întreb[ăa]rii)|"
    r"cu\s+obiectul|obiect(?:ul)?|subiectul|tema)\s*[:.]?\s*"
    r"(?P<topic>[^\n]{8,280})",
    re.IGNORECASE,
)


def _extract_topic(turn_body: str) -> str | None:
    """Recover the per-question topic from the questioner's turn body.

    Strategy (in order):
      1. First quoted subject (`„...", «...», "..."`) — most reliable.
         Romanian parliamentary practice puts the interpellation's title
         in quotes after `cu obiectul:` or in a topic-naming sentence.
      2. `obiectul interpelării: <text>` / `cu obiectul: <text>` / `tema: ...`
         capture (explicit marker, no quotes).
      3. Fallback: first non-pleasantry line (legacy v0.1 behaviour).
    """
    head = turn_body[:2500]  # quoted subject is almost always near the top

    # 1. Quoted subject — scan for the FIRST quoted span that's plausibly
    #    a topic (length 8-280 chars, doesn't span newlines).
    qm = _QUOTED_TOPIC_RE.search(head)
    if qm:
        return qm.group("topic").strip()[:300]

    # 2. Explicit `obiectul: <text>` marker
    om = _OBJECT_TOPIC_RE.search(head)
    if om:
        topic = om.group("topic").strip()
        # Strip leading punctuation / quotes that might have leaked in
        topic = topic.lstrip(":–- ").strip()
        if len(topic) >= 8:
            return topic[:300]

    # 3. Fallback: first non-pleasantry, non-salutation, non-italic,
    #    non-heading line. Uses `_TOPIC_SKIP_LINE_RE` (more aggressive than
    #    `_PLEASANTRY_LINE_RE`) to also skip `Domnule ministru,` /
    #    `Doamna ministru,` salutations which are never topics.
    for line in turn_body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("_") or line.startswith("#"):
            continue
        if (
            _PLEASANTRY_LINE_RE.match(line)
            or _TOPIC_SKIP_LINE_RE.match(line)
            or _TOPIC_PREAMBLE_RE.match(line)
        ):
            continue
        return line[:300]
    return None


def _extract_question_text(turn_body: str) -> str | None:
    """Return the policy-substance text of an interpellation turn, or None.

    `turn_body` is the post-header content (everything after the
    `## **NAME:**` line). Strategy:
      1. Cap at the earliest section-break (next political declaration,
         next interpellation in same turn).
      2. Cap at the earliest closing pattern ("Solicit răspuns", "Cu stimă,"...).
      3. Drop leading pleasantry / topic-preamble lines.
      4. Trim and reject if shorter than `_MIN_QUESTION_TEXT_CHARS`
         (very short residue = boilerplate stripped, no real content).

    Paragraph breaks inside the body are preserved (collapsed to single
    blank lines). Returns None when no meaningful body remains.
    """
    text = turn_body

    cap = len(text)
    for pat in _SECTION_BREAK_PATTERNS:
        m = pat.search(text)
        if m and m.start() < cap:
            cap = m.start()
    for pat in _CLOSING_PATTERNS:
        m = pat.search(text, 0, cap)
        if m and m.start() < cap:
            cap = m.start()
    text = text[:cap]

    lines = text.splitlines()
    while lines:
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        if (
            _PLEASANTRY_LINE_RE.match(line)
            or _TOPIC_PREAMBLE_RE.match(line)
            or line.startswith("#")
            or line.startswith("_")
        ):
            lines.pop(0)
            continue
        break

    while lines and not lines[-1].strip():
        lines.pop()

    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    if len(cleaned) < _MIN_QUESTION_TEXT_CHARS:
        return None
    return cleaned


def _parse_interpellation(
    block_text: str,
    block_start: int,
    inner: str,
    interp_start: int,
    interp_end: int,
    next_responder_start: int | None,
) -> dict[str, Any]:
    """Parse one interpellation block into the schema shape."""
    questioner_text = block_text[interp_start:interp_end]
    inner_text, _ = extract_delivery_mode(inner)
    if inner_text.endswith(":"):
        inner_text = inner_text[:-1].strip()
    questioner = parse_honorific_speaker(inner_text)

    # Genre — default interpelare
    genre = "interpelare"
    if _INTREBARE_HINTS.search(questioner_text) and not _INTERPELARE_HINTS.search(
        questioner_text
    ):
        genre = "întrebare"

    # addressed_to — search the post-header turn body, bounded to the head
    # so incidental ministry mentions in the question content don't leak in.
    body_only = "\n".join(questioner_text.splitlines()[1:])
    addressed_to = _extract_addressed_to(body_only)

    # interpellation_number — last match wins (matching qr pattern)
    nm_matches = list(_INTERPELLATION_NUMBER_RE.finditer(questioner_text))
    interpellation_number = nm_matches[-1].group("num") if nm_matches else None

    # response_deferred
    response_deferred = bool(_RESPONSE_DEFERRED_RE.search(questioner_text))

    # topic — preferred order:
    #   1. Quoted „...", "...", «...» subject (most reliable; speaker names
    #      the topic explicitly between Romanian / French / ASCII quotes)
    #   2. `obiectul (interpelării|întrebării):? <text>` capture
    #   3. `cu obiectul:? <text>` capture
    #   4. Fallback: first non-pleasantry, non-italic, non-heading line
    body_lines = questioner_text.splitlines()[1:]
    turn_body = "\n".join(body_lines)
    topic = _extract_topic(turn_body)

    # question_text — body content of the questioner's turn (post-header),
    # with pleasantries / topic-preamble / closing / section-breaks removed.
    question_text = _extract_question_text(turn_body)

    # response: null in v0.1 (response detection is downstream work)
    response = None

    return {
        "genre": genre,
        "questioner": questioner,
        "addressed_to": addressed_to,
        "addressed_to_normalized": None,
        "interpellation_number": interpellation_number,
        "topic": topic,
        "question_text": question_text,
        "response": response,
        "response_deferred": response_deferred,
    }


def _normalize_chair_name(s: str) -> str:
    """Lowercase + collapse whitespace for chair-name comparison."""
    return " ".join((s or "").lower().split())


# Chair-self-introduction patterns inside the transition phrase. Many docs
# (especially 2020+) carry the chair's identity inline:
#   "...conducerea fiind asigurată de subsemnata, Anca Dana Dragu,
#    președintele Senatului, asistată de domnul senator A și doamna senator B..."
#   "...conducerea fiind asigurată de subsemnatul, Mircea Abrudean,
#    președinte al Senatului, asistat de domnul senator X și doamna senator Y..."
# Capture the chair name (group 'chair') and any secretary names that
# follow inside the same sentence.
_CHAIR_SELFINTRO_RE = re.compile(
    r"conducerea\s+fiind\s+asigurat[ăa]\s+de\s+subsemnat[ăa]?,?\s+"
    r"(?P<chair>[A-ZȘȚĂÎÂa-zșțăîâ\-' .]+?)"
    r",\s+pre[șs]edinte",
    re.IGNORECASE,
)
# Secretaries follow: "asistat[ă] de domnul/doamna senator/deputat NAME [și NAME2]"
_SECRETARY_RE = re.compile(
    r"asistat[ăa]?\s+de\s+(?:domnul|doamna)\s+(?:senator|deputat)\s+"
    r"(?P<sec1>[A-ZȘȚĂÎÂ][A-ZȘȚĂÎÂa-zșțăîâ\-' .]+?)"
    r"(?:\s+[șs]i\s+(?:domnul|doamna)\s+(?:senator|deputat)\s+"
    r"(?P<sec2>[A-ZȘȚĂÎÂ][A-ZȘȚĂÎÂa-zșțăîâ\-' .]+?))?"
    r"\s*,\s*(?:secretari|asistat)",
    re.IGNORECASE,
)


def _extract_inline_chair_names(block_text: str) -> set[str]:
    """Pull chair + secretary names out of the chair's self-introduction
    sentence at the start of the interpellation block.

    Used to backfill chair_names when the session-level extractor missed
    them (common in pandemic-era docs where the chair-narrative italic
    block is replaced by the chair speaking inline).
    """
    found: set[str] = set()
    head = block_text[:1500]
    cm = _CHAIR_SELFINTRO_RE.search(head)
    if cm:
        chair = cm.group("chair").strip().strip(",")
        if chair:
            found.add(chair)
    sm = _SECRETARY_RE.search(head)
    if sm:
        for g in ("sec1", "sec2"):
            v = sm.group(g)
            if v:
                v = v.strip().strip(",")
                if v:
                    found.add(v)
    return found


def extract_interpellations(
    body: str,
    block_span: tuple[int, int],
    ctx: "ExtractContext",
    *,
    chair_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[Claim]]:
    """Build interpellations[] + emit record/boilerplate claims.

    `chair_names` (v0.2.1+): set of session-extracted chair + secretary
    names (case-insensitive, whitespace-collapsed). Headers whose parsed
    questioner matches a chair name are emitted as boilerplate (not as
    interpellation records). The chair speaks between questioners
    procedurally — those turns must not show up as `interpellations[]`
    entries.
    """
    offsets = ctx.line_offsets
    content_sha = ctx.content_sha
    block_start, block_end = block_span
    block_text = body[block_start:block_end]
    out: list[dict[str, Any]] = []
    claims: list[Claim] = []
    chair_set = {_normalize_chair_name(n) for n in (chair_names or set()) if n}
    # Backfill chair names from the chair's self-introduction inside the
    # transition phrase (`conducerea fiind asigurată de subsemnata, NAME,
    # ...`). Common in 2020+ docs where session.py's chair-narrative
    # detector misses the inline form.
    for n in _extract_inline_chair_names(block_text):
        chair_set.add(_normalize_chair_name(n))

    # Find earliest transition phrase line and emit boilerplate claim for it
    for pat in _TRANSITION_PHRASES:
        m = pat.search(block_text)
        if m:
            line_start = block_start + m.start()
            line_end = block_start + m.end()
            # Extend to end of line
            nl = body.find("\n", line_end)
            if nl != -1 and nl > line_end:
                line_end = nl
            claims.append(
                make_boilerplate_claim(
                    (line_start, line_end),
                    "plenary_stenogram.interpellation_transition",
                    offsets,
                )
            )
            break

    # Find each `## **<inner>:**` header (questioner shape) inside the block
    headers = list(_INTERP_HEADER_RE.finditer(block_text))
    # Find responder headers (`## **NAME** _– role_ [**:**]`)
    responder_headers = _find_responder_headers(block_text)

    if not headers and not responder_headers:
        return out, claims

    # Compute spans for each questioner header. The end of a questioner
    # span is the start of the NEXT header of any kind (questioner or
    # responder), so a responder's header doesn't get folded into the
    # previous questioner's span.
    all_starts = sorted(
        [h.start() for h in headers] + [r["start"] for r in responder_headers]
    )

    def _next_boundary_after(pos: int) -> int:
        for s in all_starts:
            if s > pos:
                return s
        return len(block_text)

    for i, h in enumerate(headers):
        interp_start = h.start()
        interp_end = _next_boundary_after(interp_start)
        inner = h.group("inner")
        global_start = block_start + interp_start
        global_end = block_start + interp_end

        # Boilerplate-name skip
        if _is_boilerplate_name(inner):
            claims.append(
                make_boilerplate_claim(
                    (global_start, global_end),
                    "plenary_stenogram.interpellation_footer_match",
                    offsets,
                )
            )
            continue

        record_dict = _parse_interpellation(
            block_text,
            block_start,
            inner,
            interp_start,
            interp_end,
            None,
        )

        skip_reason: str | None = None
        questioner_name_norm = _normalize_chair_name(
            record_dict["questioner"].get("name") or ""
        )
        if questioner_name_norm and questioner_name_norm in chair_set:
            skip_reason = "plenary_stenogram.interpellation_chair_turn"
        else:
            turn_body = "\n".join(block_text[interp_start:interp_end].splitlines()[1:])
            if _is_pure_declaration_turn(turn_body):
                skip_reason = "plenary_stenogram.interpellation_pure_declaration"

        if skip_reason is not None:
            claims.append(
                make_boilerplate_claim((global_start, global_end), skip_reason, offsets)
            )
            continue

        record_dict["source_span"] = _make_source_span(
            (global_start, global_end), offsets, content_sha
        )
        confidence = 0.85 if record_dict.get("question_text") else 0.7
        record_dict["extraction"] = {
            "extractor": INTERPELLATIONS_LABEL,
            "confidence": confidence,
            "source_span": _make_source_span(
                (global_start, global_end), offsets, content_sha
            ),
        }
        out.append(record_dict)
        claims.append(make_record_claim((global_start, global_end), offsets))

    # Process responder headers: for each, attach to the most recent
    # questioner whose end is BEFORE the responder's start. Claim the
    # responder's bytes as a record (separate from the questioner record).
    for r in responder_headers:
        r_start = r["start"]
        r_end = _next_boundary_after(r_start)
        r_global_start = block_start + r_start
        r_global_end = block_start + r_end

        # Body text starts after the closing `**:**` of the header
        body_start_in_block = r["body_start"]
        response_text = _extract_response_text(block_text[body_start_in_block:r_end])

        # Build the responder Speaker dict
        speaker = parse_honorific_speaker(r["name"])
        # Inject role from the regex's role capture (cleanup: strip `– `)
        role = r["role"].strip()
        if role.startswith("-") or role.startswith("–"):
            role = role[1:].strip()
        speaker["role"] = role or speaker.get("role")

        # Locate the questioner this responder replies to: most recent
        # emitted record whose source_span ends before r_global_start.
        target_idx: int | None = None
        for idx in range(len(out) - 1, -1, -1):
            span_chars = out[idx]["source_span"]["chars"]
            if span_chars[1] <= r_global_start:
                target_idx = idx
                break

        if target_idx is not None and response_text is not None:
            out[target_idx]["response"] = {
                "speaker": speaker,
                "text": response_text,
            }
            # Bump confidence when both qt + response are recovered
            ext = out[target_idx]["extraction"]
            ext["confidence"] = max(ext["confidence"], 0.9)

        # Claim the responder span (record claim — keeps coverage tight)
        claims.append(make_record_claim((r_global_start, r_global_end), offsets))

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
    "INTERPELLATIONS_VERSION",
    "INTERPELLATIONS_LABEL",
    "find_interpellation_block",
    "extract_interpellations",
]
