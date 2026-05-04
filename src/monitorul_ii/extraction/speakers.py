"""Universal `Speaker` shape parser.

Every callsite that records a person uses the same dict (see schema
`$defs/Speaker`). v0.1 covers the question_register questioner format —
deputat / senator with a party group and an optional constituency. Plenary
speaker headers (`Domnul/Doamna NAME` with rank prefixes, italicised
signatures with role suffixes) are handled by future revisions when the
plenary_stenogram extractor lands.

The constituency string ("Circumscripția electorală nr. 5 Bihor") is
preserved in `raw` for now — there's no schema slot for it yet, and the
person registry will be the place to backfill it cleanly.
"""

from __future__ import annotations

import re
from typing import Any

SPEAKERS_VERSION = "0.1.0"


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


def _norm_name(s: str) -> str:
    return " ".join(s.split())


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
