"""Curated registries for backfill normalization.

Each registry is a JSON file in this package shipped with the wheel
(`src/monitorul_ii/registries/<name>.json`). Loaders are lazy + cached
via `functools.lru_cache` and validate the file shape at first access.

# Match strategy (shared across registries)

For each registry, the normaliser tries match tiers in order, returning
the first hit:

  1. **exact** — input string equals canonical_name or any alias
     (case-sensitive, exact byte equality).
  2. **case** — case-insensitive equality against the same set.
  3. **diacritic** — case- AND diacritic-stripped equality. Stripping
     uses `unicodedata.normalize('NFKD')` + filters combining marks,
     plus a cedilla→comma normalisation pass for `ţ→t` / `ş→s` so
     pre-2010 cedilla variants and modern comma-diacritic variants
     collide. Mojibake artefacts that survive NFKD (replacement chars
     `�`, byte-mangled sequences) are also stripped.
  4. **token_set** — token-set equality after diacritic+case strip and
     punctuation removal. Catches reorderings and minor inflections
     (e.g. word-order differences in long institutional names).
  5. **prefix** (ministries only) — last-resort, longest-prefix match
     where the cleaned input STARTS WITH a registered alias and the
     next character is a token boundary (whitespace or end-of-string).
     This recovers from the plenary extractor's habit of bleeding
     sentence prose into `addressed_to` (`Ministerul Justiției a fost
     să modifice legislația…`) without admitting fuzzy matches —
     `Ministerul nostru` falls through because no registered alias is
     just `Ministerul`. Disabled on the institutional registry where
     it isn't needed and would be more dangerous (institutional names
     overlap less cleanly than ministry name variants).

There is intentionally NO fuzzy / Levenshtein tier — the registries
target precision over recall and a fuzzy tier would silently merge
distinct bodies whose names share a long prefix (e.g. multiple
"Agenția Națională ..." entries).

Each `normalize_*` function returns a tuple
`(id_or_none, matched_via_or_none)` so callers can record provenance.
`matched_via` is one of `"exact" | "case" | "diacritic" | "token_set"
| "prefix"` when matched, `None` otherwise.

# Versioning

Each registry carries its own `version` field in the JSON, surfaced as
`<NAME>_REGISTRY_VERSION` for telemetry. Backfill versions live next to
their backfill pass (in `extraction/backfills.py`) and are NOT part of
`extractor_versions` — re-extracting a sidecar clobbers backfill-written
fields, so the user must re-run `backfill` after `extract`. Same
contract as the cross-doc linker.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Literal

_REGISTRIES_DIR = Path(__file__).resolve().parent

MatchedVia = Literal["exact", "case", "diacritic", "token_set", "prefix"]


def _strip_diacritics(s: str) -> str:
    """Strip combining diacritics + normalise cedilla→comma form.

    Romanian text in MO can surface either modern comma-below diacritics
    (`ț`, `ș`) or pre-2010 cedilla forms (`ţ`, `ş`). Both must collapse
    to the same key so a 2008 doc and a 2024 doc match the same alias.
    Mojibake replacement chars (`\\ufffd`) are also stripped — they show
    up in roughly 10% of pre-2018 image-only PDFs and would otherwise
    break alias matching.
    """
    cedilla_map = str.maketrans({"ţ": "t", "Ţ": "T", "ş": "s", "Ş": "S"})
    s = s.translate(cedilla_map)
    nfkd = unicodedata.normalize("NFKD", s)
    no_combining = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return no_combining.replace("�", "")


_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tokenise(s: str) -> frozenset[str]:
    """Token-set form: lowercased, diacritic-stripped, punctuation-split."""
    folded = _strip_diacritics(s).lower()
    return frozenset(t for t in _TOKEN_SPLIT_RE.split(folded) if t)


def _validate_entries(entries: list[dict], *, registry_name: str) -> None:
    """Sanity checks on a registry file's entries list."""
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{registry_name}: entries must be a non-empty list")
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{registry_name}: entry is not an object: {entry!r}")
        for key in ("id", "canonical_name"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ValueError(
                    f"{registry_name}: entry missing required string '{key}': {entry!r}"
                )
        if entry["id"] in seen_ids:
            raise ValueError(f"{registry_name}: duplicate id {entry['id']!r}")
        seen_ids.add(entry["id"])
        aliases = entry.get("aliases") or []
        if not isinstance(aliases, list) or not all(
            isinstance(a, str) and a for a in aliases
        ):
            raise ValueError(
                f"{registry_name}: entry {entry['id']!r} has malformed aliases"
            )


# -- institutional bodies ---------------------------------------------------


@lru_cache(maxsize=1)
def _load_institutional_bodies() -> dict:
    path = _REGISTRIES_DIR / "institutional_bodies.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _validate_entries(data.get("entries") or [], registry_name="institutional_bodies")
    return data


def load_institutional_bodies() -> dict:
    """Public accessor — returns the parsed JSON dict."""
    return _load_institutional_bodies()


INSTITUTIONAL_BODIES_REGISTRY_VERSION: str = _load_institutional_bodies()["version"]


@lru_cache(maxsize=1)
def _institutional_alias_index() -> tuple[
    dict[str, str],  # exact
    dict[str, str],  # casefold
    dict[str, str],  # diacritic-stripped + casefold
    list[tuple[frozenset[str], str]],  # token-set
]:
    data = _load_institutional_bodies()
    exact: dict[str, str] = {}
    case: dict[str, str] = {}
    diacritic: dict[str, str] = {}
    token: list[tuple[frozenset[str], str]] = []
    for entry in data["entries"]:
        eid = entry["id"]
        names: list[str] = [entry["canonical_name"], *(entry.get("aliases") or [])]
        for name in names:
            exact.setdefault(name, eid)
            case.setdefault(name.casefold(), eid)
            diacritic.setdefault(_strip_diacritics(name).casefold(), eid)
            token.append((_tokenise(name), eid))
    return exact, case, diacritic, token


def normalize_institutional_body(
    raw: str | None,
) -> tuple[str | None, MatchedVia | None]:
    """Map a free-text issuing-body string to a registry id.

    Returns `(id, matched_via)` on hit, `(None, None)` on miss. The
    matched-via value records which tier resolved the match for
    audit/telemetry. `raw=None` or empty input returns `(None, None)`
    cheaply.

    Trims surrounding whitespace and bracket noise (the extractor
    sometimes leaves a trailing footnote marker `**` or HTML break
    `<br>` in the raw value); aliases are matched against the cleaned
    string so registries don't have to enumerate every artefact.
    """
    if not isinstance(raw, str):
        return (None, None)
    cleaned = raw.strip().rstrip("*").replace("<br>", " ").strip()
    if not cleaned:
        return (None, None)

    exact_idx, case_idx, dia_idx, token_idx = _institutional_alias_index()

    if (eid := exact_idx.get(cleaned)) is not None:
        return (eid, "exact")
    if (eid := case_idx.get(cleaned.casefold())) is not None:
        return (eid, "case")
    diacritic_key = _strip_diacritics(cleaned).casefold()
    if (eid := dia_idx.get(diacritic_key)) is not None:
        return (eid, "diacritic")
    raw_tokens = _tokenise(cleaned)
    if not raw_tokens:
        return (None, None)
    for token_set, eid in token_idx:
        if token_set and token_set == raw_tokens:
            return (eid, "token_set")
    return (None, None)


# -- ministries (4.2) -------------------------------------------------------


@lru_cache(maxsize=1)
def _load_ministries() -> dict:
    path = _REGISTRIES_DIR / "ministries.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _validate_entries(data.get("entries") or [], registry_name="ministries")
    return data


def load_ministries() -> dict:
    """Public accessor — returns the parsed JSON dict."""
    return _load_ministries()


MINISTRIES_REGISTRY_VERSION: str = _load_ministries()["version"]


@lru_cache(maxsize=1)
def _ministry_alias_index() -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    list[tuple[frozenset[str], str]],
    list[tuple[str, str]],
]:
    data = _load_ministries()
    exact: dict[str, str] = {}
    case: dict[str, str] = {}
    diacritic: dict[str, str] = {}
    token: list[tuple[frozenset[str], str]] = []
    prefix: list[tuple[str, str]] = []  # (diacritic-folded name, id)
    for entry in data["entries"]:
        eid = entry["id"]
        names: list[str] = [entry["canonical_name"], *(entry.get("aliases") or [])]
        for name in names:
            exact.setdefault(name, eid)
            case.setdefault(name.casefold(), eid)
            diacritic.setdefault(_strip_diacritics(name).casefold(), eid)
            token.append((_tokenise(name), eid))
            prefix.append((_strip_diacritics(name).casefold(), eid))
    # Longest-prefix-wins for the prefix tier — sort by descending length
    # so successful match is the most specific alias (e.g. "Ministerul
    # Apărării Naționale" wins over "Ministerul Apărării" when the input
    # starts with the longer form).
    prefix.sort(key=lambda t: -len(t[0]))
    return exact, case, diacritic, token, prefix


def normalize_ministry(
    raw: str | None,
) -> tuple[str | None, MatchedVia | None]:
    """Map a free-text addressee/ministry string to a registry id.

    Strict cleanup: trims whitespace + a few extractor-leakage artefacts
    (`<br>`, trailing `**`, smart-quote tail). Then runs the four-tier
    match. Token-set tier is the last line of defense for word-order
    variants in long ministry names — but it is bounded to entries
    where the registered token set has at least 3 tokens, otherwise a
    single-word token-set match (e.g. `{ministerul}` alone) would
    spuriously hit every alias starting with `Ministerul`.

    Returns `(id, matched_via)` on hit, `(None, None)` on miss.
    """
    if not isinstance(raw, str):
        return (None, None)
    cleaned = raw.strip().rstrip("*").replace("<br>", " ").rstrip('”"').strip()
    if not cleaned:
        return (None, None)

    exact_idx, case_idx, dia_idx, token_idx, prefix_idx = _ministry_alias_index()

    if (eid := exact_idx.get(cleaned)) is not None:
        return (eid, "exact")
    if (eid := case_idx.get(cleaned.casefold())) is not None:
        return (eid, "case")
    diacritic_key = _strip_diacritics(cleaned).casefold()
    if (eid := dia_idx.get(diacritic_key)) is not None:
        return (eid, "diacritic")
    raw_tokens = _tokenise(cleaned)
    if len(raw_tokens) >= 3:
        for token_set, eid in token_idx:
            if len(token_set) >= 3 and token_set == raw_tokens:
                return (eid, "token_set")
    # Last-resort prefix match: handles the plenary extractor's habit of
    # bleeding sentence prose into `addressed_to` (e.g. `Ministerul
    # Justiției a fost să modifice legislația…`). Only fires when the
    # cleaned input STARTS WITH a registered alias AND the next char is
    # whitespace — so `Ministerul nostru` (no registered alias is just
    # "Ministerul") falls through to no-match. Longest-prefix-wins so
    # `Ministerul Apărării Naționale` beats the shorter `Ministerul
    # Apărării` alias when both are registered.
    folded = diacritic_key
    for name_folded, eid in prefix_idx:
        if not name_folded:
            continue
        # Require the prefix to end at a token boundary in the input —
        # i.e., either it equals the whole input or the char after it
        # is whitespace. This is what stops `Ministerul Educației` from
        # matching a hypothetical `Ministerul Educației și Cercetării`
        # (which would already have a more-specific alias hit higher up).
        if folded.startswith(name_folded):
            tail = folded[len(name_folded) :]
            if not tail or tail[0] == " ":
                return (eid, "prefix")
    return (None, None)


def normalize_addressee(
    raw: str | None,
) -> tuple[str | None, MatchedVia | None]:
    """Map a free-text addressee to ministry → institutional fallback.

    The schema's `addressed_to_normalized` / `ministry_normalized` slots
    accept any canonical id, including those from the institutional
    bodies registry (the schema field's docstring explicitly lists
    intelligence services / CNSAS / ombudsman / central bank as
    in-scope). This composite normaliser tries the ministry registry
    first (the dominant case) and falls back to the institutional
    bodies registry only on ministry-miss. Order matters: a ministry
    name that happens to share a token set with an institutional body
    must resolve as the ministry.
    """
    eid, via = normalize_ministry(raw)
    if eid is not None:
        return (eid, via)
    return normalize_institutional_body(raw)


__all__ = [
    "INSTITUTIONAL_BODIES_REGISTRY_VERSION",
    "MINISTRIES_REGISTRY_VERSION",
    "MatchedVia",
    "load_institutional_bodies",
    "load_ministries",
    "normalize_addressee",
    "normalize_institutional_body",
    "normalize_ministry",
]
