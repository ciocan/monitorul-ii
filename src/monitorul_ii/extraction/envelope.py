"""YAML-frontmatter parsing + envelope-metadata helpers.

`split_md(text) -> (frontmatter, body)` is the single source of truth for
"where does the body start." Every source span coordinate in this package is
relative to `body[0]`; frontmatter content is already projected into
`envelope.metadata`, so no extracted record references frontmatter spans.

The frontmatter parser is regex-based and intentionally limited to the shape
the converter emits (flat `key: value` lines, optional double quotes around
strings, ISO dates, integers). Adding PyYAML for a 5-key file would be
overkill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

# Matches the converter's output:
#   ---
#   key: value
#   ...
#   ---
#   <body>
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*\n",
    re.DOTALL,
)
_KV_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*(?P<value>.*?)\s*$")
_INT_RE = re.compile(r"^-?\d+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def split_md(text: str) -> tuple[dict[str, Any], str]:
    """Split an MD into (frontmatter dict, body string).

    Body coordinates throughout the extraction package are 0-indexed char
    offsets into the returned body string, with 1-indexed line numbers.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = _parse_frontmatter(m.group("body"))
    body = text[m.end() :]
    return fm, body


def _parse_frontmatter(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        km = _KV_RE.match(line)
        if not km:
            continue
        out[km.group("key")] = _coerce_value(km.group("value"))
    return out


def _coerce_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return _unescape_yaml_string(raw[1:-1])
    if _INT_RE.match(raw):
        try:
            return int(raw)
        except ValueError:
            return raw
    if _ISO_DATE_RE.match(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return raw
    return raw


def _unescape_yaml_string(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


@dataclass(frozen=True)
class EnvelopeMeta:
    """The slice of envelope metadata derivable from frontmatter + filename.

    Fed into the dispatcher; per-extractor code reads this rather than
    re-parsing frontmatter from the body string.
    """

    issue: str
    year: int
    part: str
    published: date
    chamber: str | None = None
    session: str | None = None
    session_date: date | None = None
    legislature: str | None = None

    def to_metadata_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "issue": self.issue,
            "year": self.year,
            "part": self.part,
            "published": self.published.isoformat(),
        }
        out["chamber"] = self.chamber
        out["session"] = self.session
        out["session_type"] = _session_type_from_label(self.session)
        out["session_date"] = (
            self.session_date.isoformat() if self.session_date else None
        )
        out["legislature"] = self.legislature
        return out


_SESSION_TYPE_RE = re.compile(r"\bSESIUNEA\s+(?P<kind>\w+)", re.IGNORECASE)


def _session_type_from_label(label: str | None) -> str | None:
    """Best-effort: map a Romanian session label to ordinary | extraordinary | None.

    The label is e.g. "SESIUNEA I ORDINARĂ – FEBRUARIE 2026" or
    "SESIUNEA EXTRAORDINARĂ – AUGUST 2024". The kind word follows SESIUNEA
    after an optional roman-numeral ordinal.
    """
    if not label:
        return None
    text = label.upper()
    if "EXTRAORDINAR" in text:
        return "extraordinary"
    if "ORDINAR" in text:
        return "ordinary"
    return None


def envelope_meta_from_frontmatter(
    fm: dict[str, Any],
) -> EnvelopeMeta:
    """Coerce the parsed frontmatter dict into an EnvelopeMeta.

    Frontmatter fields written by `converter.IssueMeta.to_yaml_frontmatter`:
    issue (string), year (int), part (string), published (date), chamber,
    session, session_date, legislature — all best-effort.
    """
    issue = fm.get("issue")
    year = fm.get("year")
    part = fm.get("part")
    published = fm.get("published")
    if not isinstance(issue, str) or not issue:
        raise ValueError(f"frontmatter missing or invalid issue: {issue!r}")
    if not isinstance(year, int):
        raise ValueError(f"frontmatter missing or invalid year: {year!r}")
    if not isinstance(part, str) or not part:
        raise ValueError(f"frontmatter missing or invalid part: {part!r}")
    if not isinstance(published, date):
        raise ValueError(f"frontmatter missing or invalid published: {published!r}")
    chamber = _norm_str(fm.get("chamber"))
    session = _norm_str(fm.get("session"))
    sd = fm.get("session_date")
    session_date = sd if isinstance(sd, date) else None
    legislature = _norm_str(fm.get("legislature"))
    return EnvelopeMeta(
        issue=issue,
        year=year,
        part=part,
        published=published,
        chamber=chamber,
        session=session,
        session_date=session_date,
        legislature=legislature,
    )


def _norm_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str) and v:
        return v
    return None


def document_id(meta: EnvelopeMeta) -> str:
    """Build the canonical `mo://YYYY/PART/ISSUE` document ID."""
    return f"mo://{meta.year}/{meta.part}/{meta.issue}"
