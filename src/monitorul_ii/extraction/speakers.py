"""Universal `Speaker` shape parser + shared low-level primitives.

Every callsite that records a person uses the same dict (see schema
`$defs/Speaker`). v0.2.0 adds the shared low-level primitives that the
plenary extractor needs (honorific stripper, title detector, delivery-mode
parser, "Domnul/Doamna NAME[, role]" parser); the qr questioner parser
shipped in v0.1 stays unchanged.

The constituency string ("Circumscripția electorală nr. 5 Bihor") is
preserved in `raw` for now — there's no schema slot for it yet, and the
person registry will be the place to backfill it cleanly.

Per-surface-form parsers (speech header, chair narrative paragraph,
interpellation questioner) live in their owning per-type extractor module.
This module exposes the shared building blocks they all share.
"""

from __future__ import annotations

import re
from typing import Any

SPEAKERS_VERSION = "0.2.0"


# Match the title word (deputat / senator) plus everything after it, lazily.
# We split the questioner string field-by-field rather than with one big
# regex — the corpus has enough variation in the constituency string
# (county name with/without diacritics, varying separators) that field-
# wise splitting is more robust than a single-pass regex.
_TITLE_RE = re.compile(
    r"^(?P<title>deputat|deputatã|deputatul|senator|senatoare|senatorul)\b"
    r"\s*(?P<after>.*)$",
    re.IGNORECASE | re.DOTALL,
)


# -- Shared low-level primitives (v0.2.0 — used by plenary's per-surface parsers) --


# Honorific gender markers — used by chair-narrative parser, speech-header
# parser, interpellation questioner parser to recognise the start of a
# person reference.
HONORIFIC_RE = re.compile(
    r"\b(?:domnul|doamna|domni[șs]oara|domnilor|doamnelor)\b",
    re.IGNORECASE,
)

# Extended title detector — covers deputat/senator (qr's set) plus the
# parliamentary roles that appear in plenary speech-header annotations
# (`Domnul X, președintele Senatului`) and chair narratives. Genitive forms
# (`președintelui`) included for chair-narrative case.
PARLIAMENTARY_TITLE_RE = re.compile(
    r"\b(?:deputat(?:ul|a)?|senator(?:ul|i|ii|oare)?|"
    r"ministru(?:l|lui)?|secretar(?:ul|i|ilor)?(?:\s+de\s+stat)?|"
    r"pre[șs]edinte(?:le|lui)?|vicepre[șs]edinte(?:le|lui)?|"
    r"viceprim-?ministru(?:l)?|prim-?ministru(?:l)?)\b",
    re.IGNORECASE,
)


# Delivery-mode parenthetical — recognised inside speech headers like
# `## **Domnul X (din sală):**`. Schema enum:
# `tribune | from_floor | written | online | from_balcony | null`.
# Audioconferință + videoconferință collapse to `online` (schema doesn't
# split audio vs video).
_DELIVERY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\(\s*de\s+la\s+tribun[ăa]\s*\)", re.IGNORECASE), "tribune"),
    (re.compile(r"\(\s*din\s+sal[ăa]\s*\)", re.IGNORECASE), "from_floor"),
    (
        re.compile(r"\(\s*prin\s+(?:audio|video)conferin[țt][ăa]\s*\)", re.IGNORECASE),
        "online",
    ),
    (re.compile(r"\(\s*online\s*\)", re.IGNORECASE), "online"),
    (re.compile(r"\(\s*de\s+la\s+balcon\s*\)", re.IGNORECASE), "from_balcony"),
    (re.compile(r"\(\s*în\s+scris\s*\)", re.IGNORECASE), "written"),
]


# Match a leading honorific + optional rank (deputat/senator/etc.) for the
# speech-header / chair-narrative shape: "Domnul deputat NAME" /
# "Doamna senator NAME, role". Rank is optional; the chair, ministers, etc.
# are introduced as "Domnul NAME, președintele Senatului" — no rank.
_HONORIFIC_PREFIX_RE = re.compile(
    r"^\s*(?P<honorific>[Dd]omnul|[Dd]oamna|[Dd]omni[șs]oara)"
    r"(?:\s+(?P<rank>deputat(?:ul)?|senator(?:ul|oare)?))?"
    r"\s+(?P<rest>.+?)\s*$",
    re.DOTALL,
)


def make_speaker(
    *,
    raw: str,
    name: str | None = None,
    title: str | None = None,
    role: str | None = None,
    party_group: str | None = None,
    person_id: str | None = None,
) -> dict[str, Any]:
    """Build a Speaker dict in the canonical shape (all keys present)."""
    return {
        "raw": raw,
        "name": name,
        "title": title,
        "role": role,
        "party_group": party_group,
        "person_id": person_id,
    }


def _norm_name(s: str) -> str:
    return " ".join(s.split())


def parse_questioner(raw: str) -> dict[str, Any]:
    """Parse a question_register questioner line into a Speaker dict.

    Format observed across 2012-2026: `<Name>, <title> <party>[, Circumscripția…]`.
    Older docs drop the constituency clause; older docs use lowercase party
    labels (`progresist`); modern docs use uppercase acronyms (`PNL`,
    `SOS România`). The whole string post-title is split on the first
    comma to separate party from constituency text.

    Best-effort: if the title word can't be located, returns a Speaker
    with only `raw` set. The extractor still emits the question record;
    coverage is unaffected.
    """
    raw_clean = raw.strip()
    if "," not in raw_clean:
        return make_speaker(raw=raw_clean)
    name, rest = raw_clean.split(",", 1)
    name = _norm_name(name)
    rest = rest.strip()
    m = _TITLE_RE.match(rest)
    if not m:
        return make_speaker(raw=raw_clean, name=name or None)
    title = m.group("title").lower()
    after = m.group("after").strip()
    if "," in after:
        party = after.split(",", 1)[0].strip()
    else:
        party = after
    party = _norm_name(party) or None
    return make_speaker(
        raw=raw_clean,
        name=name or None,
        title=title,
        party_group=party,
    )


# -- v0.2.0 shared primitives ------------------------------------------------


def extract_delivery_mode(raw: str) -> tuple[str, str | None]:
    """Strip a `(din sală)` / `(de la tribună)` etc. parenthetical from the
    inner header text. Returns `(stripped_raw, delivery_mode_enum_or_None)`.

    Recognised enum values: `tribune | from_floor | online | from_balcony |
    written`. `prin audioconferință` and `prin videoconferință` collapse to
    `online` (schema enum has no audio/video distinction).
    """
    for pat, mode in _DELIVERY_PATTERNS:
        m = pat.search(raw)
        if m:
            stripped = (raw[: m.start()] + raw[m.end() :]).strip()
            stripped = re.sub(r"\s{2,}", " ", stripped)
            return stripped, mode
    return raw, None


def parse_honorific_speaker(raw: str) -> dict[str, Any]:
    """Parse a `Domnul/Doamna NAME[, role]` string into a Speaker dict.

    Used by the plenary speech-header parser (after the `## **...:**` shell
    has been stripped) and by the interpellation questioner parser.

    Examples:
      "Domnul Sorin-Mihai Grindeanu" → name="Sorin-Mihai Grindeanu"
      "Doamna Alina-Ștefania Gorghiu" → name="Alina-Ștefania Gorghiu"
      "Domnul Mircea Abrudean, președintele Senatului" → name="...", role="..."
      "Domnul deputat Daniel Grofu" → name="Daniel Grofu", title="deputat"
      "Doamna senator Doina-Elena Federovici, secretar al Senatului"
        → name="...", title="senator", role="..."

    Best-effort: returns Speaker with only `raw` set if no honorific is
    present. Empty string handling: returns make_speaker(raw="") (caller
    decides whether to keep the record).
    """
    raw_clean = (raw or "").strip()
    if not raw_clean:
        return make_speaker(raw=raw_clean)

    m = _HONORIFIC_PREFIX_RE.match(raw_clean)
    if not m:
        # No honorific — fall back to "<name>, <role>" split (e.g., when
        # the source already stripped the honorific upstream).
        return _parse_no_honorific(raw_clean)

    rank = m.group("rank")
    rest = m.group("rest").strip()
    title = rank.lower() if rank else None
    if title and title.endswith("ul"):
        # Strip definite-article suffix: deputatul → deputat, senatorul → senator
        title = title[:-2]
    if title == "senatoare":
        title = "senator"

    if "," in rest:
        name_part, role_part = rest.split(",", 1)
        name = _norm_name(name_part)
        role = _norm_name(role_part) or None
    else:
        name = _norm_name(rest)
        role = None

    return make_speaker(
        raw=raw_clean,
        name=name or None,
        title=title,
        role=role,
    )


def _parse_no_honorific(raw_clean: str) -> dict[str, Any]:
    """Fallback for `<name>, <role>` shapes with no honorific prefix."""
    if "," in raw_clean:
        name_part, role_part = raw_clean.split(",", 1)
        return make_speaker(
            raw=raw_clean,
            name=_norm_name(name_part) or None,
            role=_norm_name(role_part) or None,
        )
    return make_speaker(raw=raw_clean, name=raw_clean or None)


__all__ = [
    "SPEAKERS_VERSION",
    "HONORIFIC_RE",
    "PARLIAMENTARY_TITLE_RE",
    "make_speaker",
    "parse_questioner",
    "extract_delivery_mode",
    "parse_honorific_speaker",
]
